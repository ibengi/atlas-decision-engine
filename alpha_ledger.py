"""Append-only cost and calibration ledgers, and the metrics over them.

Alpha Gateway v1, sections 12, 13, 14, 15, 21. SHADOW ONLY.

WHY APPEND-ONLY, AND WHY IT MATTERS MORE HERE THAN ELSEWHERE
    Section 13 is blunt: historical predictions must never be retroactively
    overwritten after resolution. The failure mode is not malice, it is
    convenience -- a prediction row updated in place after the outcome is
    known cannot be distinguished from one that was always right, and every
    calibration number computed from that file becomes unfalsifiable.

    So a prediction is one immutable line, a resolution is a SECOND
    immutable line referring to it, and the resolved view is derived by
    replay. `resolve()` refuses a second resolution for the same prediction,
    and `PREDICTION` rows are never rewritten. This is the same discipline
    the equity ledger's continuity chain uses (`continuity.py`), for the
    same reason and after the same audit finding.

WHAT IS DELIBERATELY NOT COMPUTED
    A net-of-inference-cost figure is withheld while token prices are unset
    (`cost_priced=false` on every row). Reporting "net PnL after AI cost"
    from a cost of zero would answer this subsystem's central question with
    a placeholder. `metrics()` says `cost_priced: false` and omits the
    figure instead.
"""

import json
import hashlib
import logging
import math
import os
from datetime import datetime, timezone

from config import CFG, _p
from durable_append import (append_line, exclusive_lock,
                            serialized_append, tail_is_torn, sync_path,
                            file_generation)

log = logging.getLogger("ALPHA")

LEDGER_SCHEMA = "atlas-alpha-ledger-v1"
ROW_PREDICTION = "PREDICTION"
ROW_RESOLUTION = "RESOLUTION"
ROW_COST = "COST"
#: Phase 2, section 7. A probability estimated BEFORE a scheduled
#: information event may not stay actionable in shadow scoring after it. The
#: invalidation is a NEW row, like a resolution: the prediction itself is
#: never edited, so "what we thought at the time" and "when it stopped
#: counting" remain separately auditable.
ROW_INVALIDATION = "INVALIDATION"
#: Section 8. A follow-up price observation at a configured interval.
ROW_OBSERVATION = "OBSERVATION"
#: AA-13. Written BEFORE the prediction it announces. A PREPARE with no
#: matching PREDICTION is the durable trace of a crash between "we decided to
#: analyse this" and "the analysis is safely on disk", and it is what lets a
#: restart tell that apart from work that genuinely completed.
ROW_PREPARE = "PREPARE"
#: Identity records bind a PREPARE or PREDICTION to its exact canonical row.
#: Their readability is NEVER a durability certificate. Authority methods
#: validate both records under one lock and cross actual file and directory
#: synchronization barriers, including on retry and in a fresh process.
ROW_COMMIT = "COMMIT"

#: This historical row kind is retained for append-only compatibility. There
#: is no additional receipt chain: confirmation is a successful barrier, not
#: another readable marker. See _AppendOnlyLog.confirm for the state machine.
ROW_PREPARE_COMMIT = "PREPARE_COMMIT"
RECEIPT_SCHEMA = "atlas-alpha-receipt-v2"

ROW_KINDS = (ROW_PREPARE, ROW_PREPARE_COMMIT, ROW_PREDICTION, ROW_COMMIT,
             ROW_RESOLUTION, ROW_COST, ROW_INVALIDATION, ROW_OBSERVATION)


def analysis_identity(market_snapshot_id: str) -> str:
    """The STABLE identity of one analysis of one snapshot (AA-13).

    Derived from the snapshot id alone, so a retry after a crash computes the
    SAME value and the duplicate check can actually fire. `prediction_id`
    cannot serve this purpose: it mixes in the wall clock, so the same
    evidence retried a second later produces a different id and the row is
    appended twice.
    """
    return "an-" + str(market_snapshot_id)


def verify_source_evidence(prediction) -> dict:
    """Independently verify retained source bytes and their economic snapshot."""
    from alpha_settlement_validation import verify_source_evidence as verify
    return verify(prediction)


#: RA-13. Every binding field a settlement must carry for its outcome to be
#: admissible in LEARNING. The same list `alpha_resolution_ingest` enforces at
#: ingestion, re-checked here: a resolution row could have been appended by an
#: older build, by hand, or by a tool that did not go through that path, and
#: learning has to be able to tell.
QUALIFIED_BINDING_FIELDS = ("contract_id", "market_snapshot_id",
                            "source_record_sha256", "environment",
                            "contract_schema")


def settlement_qualification(prediction, resolution) -> tuple:
    """Shared ingress/replay semantic gate; stored flags are never proof."""
    from alpha_settlement_validation import settlement_qualification as qualify
    return qualify(prediction, resolution)


class LedgerError(RuntimeError):
    """A ledger write could not be made durable, or would rewrite history."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


class _AppendOnlyLog:
    """One JSON object per line, fsynced, never rewritten or truncated."""

    def __init__(self, path: str):
        self.path = path
        self._observed = False

    def append(self, row: dict) -> dict:
        """Append one row, completely and durably (AA-12).

        `durable_append.append_line` loops until every byte is written, closes
        a torn tail by SEPARATION rather than truncation, and fsyncs. The
        previous single `os.write` could write fewer bytes than it was given
        and lose a ledger row that then read as a clean crash.
        """
        line = json.dumps(row, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str) + "\n"
        try:
            # RA-05: `tail_is_torn` now RAISES `DurabilityUnknown` when it
            # cannot read the tail, so it belongs inside the guard. Outside
            # it, an unreadable tail escaped as a bare `OSError` and a caller
            # that only handles `LedgerError` would have seen a different
            # exception type for the same failure.
            if tail_is_torn(self.path):
                log.error(
                    f"[ALPHA_LEDGER] torn tail detected in {self.path}; the "
                    f"damaged fragment is PRESERVED and separated, not "
                    f"truncated -- it will be reported as an unparsable row")
            append_line(self.path, line)
        except OSError as e:
            raise LedgerError(f"alpha ledger row not durable: {e}")
        return row

    def lock(self, *, timeout: float = 10.0):
        """Serialize a check-then-append critical section (AA-14)."""
        return exclusive_lock(self.path, timeout=timeout)

    def rows(self, *, with_generation=False):
        """Every parseable row. A torn LAST line is a crash mid-append and
        is skipped; a bad line anywhere else is reported and skipped, never
        silently treated as the end of the file."""
        opened = False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                opened = True
                generation = file_generation(os.fstat(fh.fileno()))
                lines = fh.read().splitlines()
                if file_generation(os.fstat(fh.fileno())) != generation:
                    raise LedgerError("alpha ledger changed during read")
            if with_generation and file_generation(os.stat(self.path)) != generation:
                raise LedgerError("alpha ledger name changed during read")
            self._observed = True
        except FileNotFoundError as e:
            if opened or self._observed:
                raise LedgerError(f"previously observed alpha ledger disappeared: {e}")
            return ([], None) if with_generation else []
        except OSError as e:
            raise LedgerError(f"alpha ledger unreadable: {e}")
        out = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    log.warning("[ALPHA_LEDGER] torn last row ignored "
                                "(crash during append)")
                    break
                log.error(f"[ALPHA_LEDGER] unparsable row at line {i + 1} "
                          f"-- skipped, NOT treated as end of file")
                continue
            if isinstance(row, dict):
                out.append(row)
        return (out, generation) if with_generation else out

    def confirm(self, generation=None) -> None:
        """Cross real synchronization barriers; caller holds ``lock()``.

        State transitions are ABSENT -> READABLE_UNCONFIRMED on write,
        READABLE_UNCONFIRMED -> CONFIRMED only on successful file AND name
        synchronization. Every new reader starts unconfirmed. A failed barrier
        leaves the row nonterminal regardless of how many receipts are read.
        """
        try:
            if not sync_path(self.path, expected_generation=generation):
                raise LedgerError("ledger disappeared before its durability barrier")
        except OSError as exc:
            raise LedgerError(f"alpha ledger durability remains unconfirmed: {exc}") from exc


class AlphaLedger:
    """Calibration ledger (section 13) plus the cost ledger (section 12)."""

    def __init__(self, path: str = None, cost_path: str = None):
        self.log = _AppendOnlyLog(path or _p(CFG.ALPHA_LEDGER_FILE))
        self.cost_log = _AppendOnlyLog(cost_path or _p(CFG.ALPHA_COST_FILE))

    # ── writing ─────────────────────────────────────────────────────────
    def record_costs(self, snapshot, dispatch_result) -> list:
        """One row per provider invocation, successful or not.

        A failed call still consumed a deadline and often still consumed
        tokens; section 12 wants the economics of ASKING, not just of
        succeeding.
        """
        rows = []
        # AA-14 (re-audit): one lock for the whole batch. Each append reads
        # the file's last byte to decide whether a torn tail needs
        # separating, so even a pure append is a check-then-act here; and a
        # cost batch that interleaves with another writer's batch is a cost
        # ledger nobody can attribute to a cycle afterwards.
        with self.cost_log.lock():
            for signal in dispatch_result.signals:
                cost = dict(signal.cost or {})
                rows.append(self.cost_log.append({
                    "schema": LEDGER_SCHEMA, "kind": ROW_COST,
                    "at": _now_iso(),
                    "market_snapshot_id": snapshot.market_snapshot_id,
                    "contract_id": snapshot.contract_id,
                    "provider": signal.provider or cost.get("provider"),
                    "model": signal.model,
                    "input_tokens": int(cost.get("input_tokens") or 0),
                    "output_tokens": int(cost.get("output_tokens") or 0),
                    "api_cost_usd": float(cost.get("api_cost_usd") or 0.0),
                    "cost_priced": bool(cost.get("cost_priced", False)),
                    "latency_ms": int(signal.analysis_latency_ms or 0),
                    "outcome": "VALID" if signal.valid else "EXCLUDED",
                    "reason": signal.rejected_reason,
                }))
        return rows

    def cycle_cost_usd(self, dispatch_result) -> float:
        return round(sum(float((s.cost or {}).get("api_cost_usd") or 0.0)
                         for s in dispatch_result.signals), 8)

    @staticmethod
    def _row_digest(row: dict) -> str:
        try:
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise LedgerError("receipt target cannot be canonically represented") from exc
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _receipt_time(value) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return instant.tzinfo is not None and instant.utcoffset() is not None
        except (ValueError, TypeError, OverflowError):
            return False

    def _receipt_index(self, rows: list, kind: str) -> dict:
        """Validate receipts against exact *preceding* canonical target rows.

        Receipts are identity records, not durability certificates. A caller
        may authorize work only after ``log.confirm()`` also succeeds while
        holding the same lock. Unknown receipt versions are never inferred.
        """
        target_kind = ROW_PREPARE if kind == ROW_PREPARE_COMMIT else ROW_PREDICTION
        targets, out = {}, {}
        for row in rows:
            if row.get("kind") == target_kind:
                key = row.get("analysis_id") if target_kind == ROW_PREPARE else row.get("prediction_id")
                if not isinstance(key, str) or not key or key in targets:
                    raise LedgerError("missing or duplicate receipt target identity")
                sid = row.get("market_snapshot_id")
                aid = row.get("analysis_id")
                if (row.get("schema") != LEDGER_SCHEMA or not isinstance(sid, str)
                        or not isinstance(aid, str)
                        or aid != (analysis_identity(sid) if sid else "")):
                    raise LedgerError("receipt target schema or analysis identity mismatch")
                targets[key] = row
            elif row.get("kind") == kind:
                key = row.get("analysis_id") if kind == ROW_PREPARE_COMMIT else row.get("prediction_id")
                target = targets.get(key) if isinstance(key, str) else None
                # V4 rows remain immutable. The exact historical v1 shape is
                # supported as a row identity, with all of its declared
                # fields checked and a unique preceding target required.
                # It never confers durability: confirm() is still mandatory.
                # New v2 receipts additionally bind the full canonical row.
                legacy = "receipt_schema" not in row
                fields = {"schema", "kind", "at", "analysis_id", "market_snapshot_id"}
                if kind == ROW_COMMIT:
                    fields.add("prediction_id")
                if not legacy:
                    fields |= {"receipt_schema", "row_sha256"}
                    if kind == ROW_PREPARE_COMMIT:
                        fields |= {"contract_id", "source_record_sha256", "environment"}
                if (target is None or set(row) != fields
                        or row.get("schema") != LEDGER_SCHEMA
                        or (not legacy and row.get("receipt_schema") != RECEIPT_SCHEMA)
                        or not self._receipt_time(row.get("at"))
                        or row.get("analysis_id") != target.get("analysis_id")
                        or row.get("market_snapshot_id") != target.get("market_snapshot_id")
                        or (not legacy and (not isinstance(row.get("row_sha256"), str)
                            or row.get("row_sha256") != self._row_digest(target)))):
                    raise LedgerError("receipt schema or complete target identity mismatch")
                if kind == ROW_PREPARE_COMMIT and not legacy and any(
                        not isinstance(row.get(field), str)
                        or row.get(field) != target.get(field, "")
                        for field in ("contract_id", "source_record_sha256", "environment")):
                    raise LedgerError("PREPARE receipt source binding mismatch")
                out.setdefault(key, row)
        return out

    def _confirmed_receipts(self, rows: list, kind: str, generation=None) -> dict:
        receipts = self._receipt_index(rows, kind)
        if receipts:
            self.log.confirm(generation)
        return receipts

    def prepare(self, market_snapshot_id: str, *, contract_id: str = "",
                source_record_sha256: str = "", environment: str = "") -> dict:
        """Durably announce an analysis; retry crosses a barrier every time.

        A retry may finish a missing identity receipt, but it never treats an
        existing readable receipt as evidence that an earlier fsync worked.
        No extra receipt kinds are introduced by recovery.
        """
        values = (market_snapshot_id, contract_id, source_record_sha256, environment)
        if any(not isinstance(value, str) for value in values) or not market_snapshot_id:
            raise LedgerError("PREPARE identities must be strings and snapshot must be nonempty")
        aid = analysis_identity(market_snapshot_id)
        with self.log.lock():
            rows, generation = self.log.rows(with_generation=True)
            receipts = self._receipt_index(rows, ROW_PREPARE_COMMIT)
            existing = next((row for row in rows if row.get("kind") == ROW_PREPARE
                             and row.get("analysis_id") == aid), None)
            if existing is not None:
                # Empty optional caller bindings do not replace historical
                # identities; any asserted binding must agree exactly.
                if any(value and existing.get(field, "") != value for field, value in
                       (("contract_id", contract_id), ("source_record_sha256", source_record_sha256),
                        ("environment", environment))):
                    raise LedgerError("PREPARE retry changed its source binding")
                if aid not in receipts:
                    self._commit_prepare(aid, market_snapshot_id)
                else:
                    self.log.confirm(generation)
                return existing
            row = self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_PREPARE,
                "at": _now_iso(), "analysis_id": aid,
                "market_snapshot_id": market_snapshot_id,
                "contract_id": contract_id,
                "source_record_sha256": source_record_sha256,
                "environment": environment})
            self._commit_prepare(aid, market_snapshot_id)
            return row

    def _commit_prepare(self, analysis_id: str, snapshot_id: str) -> dict:
        """Record complete PREPARE identity; caller holds the ledger lock."""
        target = self.find_prepare(analysis_id)
        if target is None or target.get("market_snapshot_id") != snapshot_id:
            raise LedgerError("PREPARE receipt has no exact preceding target")
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_PREPARE_COMMIT,
            "receipt_schema": RECEIPT_SCHEMA, "row_sha256": self._row_digest(target),
            "at": _now_iso(), "analysis_id": analysis_id,
            "market_snapshot_id": snapshot_id,
            **{field: target.get(field, "") for field in
               ("contract_id", "source_record_sha256", "environment")}})

    def prepare_commits(self) -> dict:
        """Validated PREPARE identities, after an actual sync barrier."""
        with self.log.lock():
            rows, generation = self.log.rows(with_generation=True)
            return self._confirmed_receipts(rows, ROW_PREPARE_COMMIT, generation)

    def prepare_is_durable(self, analysis_id: str) -> bool:
        return isinstance(analysis_id, str) and bool(analysis_id) and analysis_id in self.prepare_commits()

    def find_prepare(self, analysis_id: str):
        """Readable PREPARE only; this diagnostic lookup confers no authority."""
        for row in self.rows():
            if row.get("kind") == ROW_PREPARE and row.get("analysis_id") == analysis_id:
                return row
        return None

    def record_prediction(self, opportunity: dict) -> dict:
        """Append a prediction and its bound identity receipt under one lock.

        Recovery retains the original prediction ID. Historical budget
        refusals may be superseded by a new append-only attempt; completed
        economic analyses cannot be superseded or written twice.
        """
        prediction_id = opportunity.get("prediction_id")
        snapshot_id = opportunity.get("market_snapshot_id", "")
        if (not isinstance(prediction_id, str) or not prediction_id
                or not isinstance(snapshot_id, str)):
            raise LedgerError("prediction and snapshot identities must be strings")
        aid = analysis_identity(snapshot_id) if snapshot_id else ""
        with self.log.lock():
            rows, generation = self.log.rows(with_generation=True)
            receipts = self._receipt_index(rows, ROW_COMMIT)
            predictions = [r for r in rows if r.get("kind") == ROW_PREDICTION]
            existing_id = next((r for r in predictions if r.get("prediction_id") == prediction_id), None)
            if existing_id is not None:
                raise LedgerError(f"prediction {prediction_id} already recorded; a prediction is written once")
            previous = [r for r in predictions if aid and r.get("analysis_id") == aid]
            existing = previous[-1] if previous else None
            supersedes = None
            if existing is not None:
                pid = existing.get("prediction_id")
                if pid not in receipts:
                    self._commit(aid, pid, snapshot_id)
                    return existing
                self.log.confirm(generation)
                if existing.get("state") != "BUDGET_EXHAUSTED":
                    raise LedgerError(f"analysis {aid} already has committed prediction {pid}; this snapshot is not analysed twice")
                supersedes = pid
            row = {**opportunity, "schema": LEDGER_SCHEMA, "kind": ROW_PREDICTION,
                   "at": opportunity.get("at", _now_iso()), "analysis_id": aid,
                   "market_snapshot_id": snapshot_id}
            if supersedes is not None:
                row["supersedes_prediction_id"] = supersedes
            elif "supersedes_prediction_id" in row:
                raise LedgerError("prediction names a nonexistent superseded attempt")
            result = self.log.append(row)
            self._commit(aid, prediction_id, snapshot_id)
            return result

    def _commit(self, analysis_id: str, prediction_id: str,
                snapshot_id: str) -> dict:
        """Append an exact-row identity receipt; caller holds the lock."""
        target = self.find_prediction(prediction_id)
        if (target is None or target.get("analysis_id") != analysis_id
                or target.get("market_snapshot_id", "") != snapshot_id):
            raise LedgerError("COMMIT receipt has no exact preceding target")
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_COMMIT,
            "receipt_schema": RECEIPT_SCHEMA, "row_sha256": self._row_digest(target),
            "at": _now_iso(), "analysis_id": analysis_id,
            "prediction_id": prediction_id, "market_snapshot_id": snapshot_id})

    def find_prediction_by_analysis(self, analysis_id: str):
        """Latest readable attempt only; no claim of confirmed durability."""
        candidates = [r for r in self.predictions() if analysis_id
                      and r.get("analysis_id") == analysis_id]
        return candidates[-1] if candidates else None

    def _effective_committed(self, rows: list, receipts: dict) -> dict:
        out = {}
        for row in rows:
            if row.get("kind") != ROW_PREDICTION or row.get("prediction_id") not in receipts:
                continue
            sid = row.get("market_snapshot_id")
            if not sid:
                continue
            previous = out.get(sid)
            if previous is not None:
                if (previous.get("state") != "BUDGET_EXHAUSTED"
                        or row.get("supersedes_prediction_id") != previous.get("prediction_id")):
                    raise LedgerError("conflicting committed predictions for one analysis")
            elif row.get("supersedes_prediction_id"):
                raise LedgerError("committed successor has no committed preceding attempt")
            out[sid] = row
        return out

    def commits(self) -> dict:
        """Effective analysis -> exact COMMIT receipt, after real barriers."""
        with self.log.lock():
            rows, generation = self.log.rows(with_generation=True)
            receipts = self._confirmed_receipts(rows, ROW_COMMIT, generation)
            return {row["analysis_id"]: receipts[row["prediction_id"]]
                    for row in self._effective_committed(rows, receipts).values()}

    def committed_predictions(self) -> dict:
        """One consistent, synchronized snapshot -> exact prediction view.

        This supplies restart reconciliation without unrelated reads of
        receipts and predictions, or per-object durable-state caches.
        """
        with self.log.lock():
            rows, generation = self.log.rows(with_generation=True)
            receipts = self._confirmed_receipts(rows, ROW_COMMIT, generation)
            return self._effective_committed(rows, receipts)

    def committed_prediction(self, market_snapshot_id: str):
        return self.committed_predictions().get(market_snapshot_id)

    def prediction_is_committed(self, market_snapshot_id: str) -> bool:
        return self.committed_prediction(market_snapshot_id) is not None

    def resolve(self, prediction_id: str, outcome, *, resolved_at=None,
                source: str = "", binding: dict = None) -> dict:
        """Record the outcome as a NEW row.

        `outcome` is 1 for YES, 0 for NO. The prediction row is not touched:
        the ledger is replayed to build the resolved view, so "what did we
        predict" and "what happened" can never be conflated into one
        editable record.
        """
        if outcome not in (0, 1, True, False):
            raise LedgerError(f"outcome {outcome!r} must be 0 or 1")
        # AA-14: check and append under one lock, so two ingesters cannot both
        # observe "unresolved" and both append a resolution.
        with self.log.lock():
            prediction = self.find_prediction(prediction_id)
            if prediction is None:
                raise LedgerError(f"unknown prediction {prediction_id}")
            if self.find_resolution(prediction_id) is not None:
                raise LedgerError(f"prediction {prediction_id} is already "
                                  f"resolved; outcomes are written once")
            return self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_RESOLUTION,
                "at": _now_iso(), "prediction_id": prediction_id,
                "actual_outcome": int(bool(outcome)),
                "resolved_at": resolved_at or _now_iso(),
                "resolution_source": source,
                **{k: v for k, v in (binding or {}).items()}})

    # ── section 7: catalyst invalidation ────────────────────────────────
    def invalidate(self, prediction_id: str, reason: str,
                   detail: str = "") -> dict:
        """Mark a prediction non-actionable from now on.

        Idempotent: invalidating twice is a no-op returning the first row,
        because the moment a signal stopped counting is a fact, not a
        counter.
        """
        # AA-14 (re-audit): "is it already invalidated? no -> append" is a
        # check-then-append like any other, and it was the only one of the
        # three left outside the lock. Two writers both read "not yet" and
        # both appended, and the moment a signal stopped counting became two
        # different moments.
        with self.log.lock():
            existing = self.find_invalidation(prediction_id)
            if existing is not None:
                return existing
            if self.find_prediction(prediction_id) is None:
                raise LedgerError(f"unknown prediction {prediction_id}")
            return self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_INVALIDATION,
                "at": _now_iso(), "prediction_id": prediction_id,
                "reason": str(reason), "detail": str(detail)[:300]})

    def invalidations(self) -> dict:
        out = {}
        for row in self.rows():
            if row.get("kind") == ROW_INVALIDATION:
                out.setdefault(row.get("prediction_id"), row)
        return out

    def find_invalidation(self, prediction_id: str):
        return self.invalidations().get(prediction_id)

    def sweep_catalysts(self, now=None) -> list:
        """Invalidate every open prediction whose catalyst has passed.

        Called on each service cycle. Only predictions that are still
        unresolved and not already invalidated are touched: a resolved
        prediction is history, and history is not rewritten.
        """
        now = now or datetime.now(timezone.utc)
        resolved = self.resolutions()
        invalid = self.invalidations()
        out = []
        for prediction in self.predictions():
            pid = prediction.get("prediction_id")
            if pid in resolved or pid in invalid:
                continue
            catalyst = ((prediction.get("snapshot") or {})
                        .get("next_known_catalyst") or {})
            when = catalyst.get("time_utc")
            if not when:
                continue
            try:
                from alpha_snapshot import parse_utc
                catalyst_at = parse_utc(when, field_name="catalyst time")
            except Exception:                                 # noqa: BLE001
                continue
            if now >= catalyst_at:
                out.append(self.invalidate(
                    pid, "catalyst_occurred",
                    f"{catalyst.get('name') or 'catalyst'} at {when} has "
                    f"passed; the estimate did not see it"))
        return out

    # ── section 8: follow-up price observations ─────────────────────────
    def record_observation(self, prediction_id: str, *, interval_s,
                           quote: dict, at=None) -> dict:
        """One post-analysis price sample. Append-only like everything else.

        Idempotent per (prediction, interval): a service restart must not
        record the same interval twice and skew the latency-decay series.
        """
        # AA-14 (re-audit): the idempotence below is a read followed by a
        # write that depends on it. Unserialized, two service threads both
        # saw the interval unsampled and both appended, and the
        # latency-decay series was computed from a duplicated sample.
        with self.log.lock():
            for row in self.observations(prediction_id):
                if row.get("interval_s") == interval_s:
                    return row
            return self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_OBSERVATION,
                "at": at or _now_iso(), "prediction_id": prediction_id,
                "interval_s": interval_s,
                "quote": dict(quote) if isinstance(quote, dict) else None})

    def observations(self, prediction_id: str = None) -> list:
        return [r for r in self.rows()
                if r.get("kind") == ROW_OBSERVATION
                and (prediction_id is None
                     or r.get("prediction_id") == prediction_id)]

    # ── reading ─────────────────────────────────────────────────────────
    def rows(self) -> list:
        return self.log.rows()

    def predictions(self) -> list:
        return [r for r in self.rows() if r.get("kind") == ROW_PREDICTION]

    def resolutions(self) -> dict:
        out = {}
        for row in self.rows():
            if row.get("kind") != ROW_RESOLUTION:
                continue
            # First resolution wins. A duplicate can only appear by editing
            # the file by hand, and the earlier one is the one the metrics
            # were computed from.
            out.setdefault(row.get("prediction_id"), row)
        return out

    def find_prediction(self, prediction_id: str):
        for row in self.predictions():
            if row.get("prediction_id") == prediction_id:
                return row
        return None

    def find_resolution(self, prediction_id: str):
        return self.resolutions().get(prediction_id)

    def resolved(self) -> list:
        """Predictions joined to their outcomes, with the scores derived at
        read time. Derived, never stored: a stored score is a score that can
        drift from the prediction it grades."""
        outcomes = self.resolutions()
        invalidations = self.invalidations()
        observations = {}
        for row in self.observations():
            observations.setdefault(row.get("prediction_id"), []).append(row)
        joined = []
        for prediction in self.predictions():
            resolution = outcomes.get(prediction.get("prediction_id"))
            if resolution is None:
                continue
            row = dict(prediction)
            row["actual_outcome"] = resolution.get("actual_outcome")
            row["resolved_at"] = resolution.get("resolved_at")
            # AA-15 (re-audit): the settlement's identity and trust decision
            # travel WITH the outcome into learning. A calibration number is
            # only as good as the authority that produced the outcomes it was
            # computed from, and a learning row that cannot name that
            # authority cannot be audited afterwards.
            row["resolution_source"] = resolution.get("resolution_source", "")
            row["settlement_binding"] = resolution.get("settlement_binding")
            row["settlement_evidence_id"] = resolution.get(
                "settlement_evidence_id")
            row["binding_verified"] = resolution.get("binding_verified")
            row["source_trusted"] = resolution.get("source_trusted")
            row["source_evidence_verified"] = resolution.get("source_evidence_verified")
            # RA-13: the one verdict that decides whether learning may read
            # this row. Derived at read time, like every other score here,
            # because a stored verdict is a verdict that can drift from the
            # row it grades.
            qualified, disqualification = settlement_qualification(prediction,
                                                                   resolution)
            row["settlement_qualified"] = qualified
            row["settlement_disqualification"] = disqualification
            invalidation = invalidations.get(prediction.get("prediction_id"))
            row["invalidated"] = bool(invalidation)
            row["invalidation_reason"] = (invalidation or {}).get("reason")
            row["observations"] = [
                {"interval_s": o.get("interval_s"), "quote": o.get("quote")}
                for o in observations.get(prediction.get("prediction_id"), [])]
            row.update(score_prediction(prediction, row["actual_outcome"]))
            joined.append(row)
        return joined

    def qualified_resolved(self) -> list:
        """Resolved predictions whose settlements are FULLY QUALIFIED (RA-13).

        The only series learning and calibration may read. Everything
        `resolved()` returns stays in the audit history; this is the subset
        whose outcomes can be defended -- verified binding, qualified
        authority, recorded evidence identity, real resolution instant, and a
        source digest that independently recomputes.
        """
        return [r for r in self.resolved() if r.get("settlement_qualified")]

    def unqualified_resolved(self) -> list:
        """The complement, so the exclusion is never silent."""
        return [r for r in self.resolved()
                if not r.get("settlement_qualified")]

    def actionable_resolved(self) -> list:
        """Resolved predictions that were still valid when they resolved.

        Section 7: an estimate made before a catalyst it never saw must not
        be scored as though it were actionable. It stays in the ledger and in
        `resolved()` -- it is evidence about the model -- but it is excluded
        from the actionable series, and `metrics()` reports both so the gap
        is visible rather than assumed away.
        """
        return [r for r in self.resolved() if not r.get("invalidated")]

    # ── calibration lookup used by the Meta engine (section 8) ──────────
    def calibration(self, model: str, category: str = None):
        """`{"samples": n, "brier": x}` for one model, optionally within one
        market category. None when there is nothing to say."""
        # RA-13: the Meta engine weights models with this number, so it
        # reads QUALIFIED settlements only. An unqualified outcome stays in
        # the audit history and out of the weights.
        samples, total = 0, 0.0
        for row in self.qualified_resolved():
            if category and row.get("market_class") != category:
                continue
            per_model = (row.get("per_model") or {}).get(model)
            if not per_model or not _finite(per_model.get("p_yes")):
                continue
            outcome = row["actual_outcome"]
            total += (float(per_model["p_yes"]) - outcome) ** 2
            samples += 1
        if samples == 0:
            return None
        return {"samples": samples, "brier": round(total / samples, 8)}

    # ── metrics (section 14) ────────────────────────────────────────────
    def metrics(self) -> dict:
        rows = self.resolved()
        cost_rows = self.cost_log.rows()
        priced = bool(cost_rows) and all(r.get("cost_priced")
                                         for r in cost_rows)
        report = {
            "schema": LEDGER_SCHEMA,
            "generated_at": _now_iso(),
            "predictions_recorded": len(self.predictions()),
            "predictions_resolved": len(rows),
            # RA-13: BOTH series, always. The gap between them is itself the
            # finding -- a large one means the settlement feed is not
            # producing outcomes learning can use -- so it is reported rather
            # than assumed away.
            "settlements_qualified": sum(
                1 for r in rows if r.get("settlement_qualified")),
            "settlements_unqualified": sum(
                1 for r in rows if not r.get("settlement_qualified")),
            "ensemble_qualified": _score_group(
                [r for r in rows if r.get("settlement_qualified")],
                lambda r: r.get("p_meta")),
            "cost_priced": priced,
            "ensemble": _score_group(rows, lambda r: r.get("p_meta")),
            # Section 7: the same series with catalyst-invalidated estimates
            # removed. Reported ALONGSIDE, never instead: a large gap between
            # the two is itself the finding about latency and event risk.
            "ensemble_actionable": _score_group(
                self.actionable_resolved(), lambda r: r.get("p_meta")),
            "invalidated_predictions": len(self.invalidations()),
            "observations_recorded": len(self.observations()),
            "by_model": {}, "by_category": {}, "by_horizon": {},
            "by_latency_class": {}, "by_confidence_bucket": {},
            "provider_failure_rate": _failure_rates(cost_rows),
            "signal_expiration_rate": _expiration_rate(self.predictions()),
            "latency_ms_mean": _mean([r.get("latency_ms") for r in cost_rows]),
            "inference_cost_usd_total": round(
                sum(float(r.get("api_cost_usd") or 0.0) for r in cost_rows), 8),
        }
        models = {m for r in rows for m in (r.get("per_model") or {})}
        for model in sorted(models):
            report["by_model"][model] = _score_group(
                rows, lambda r, m=model: ((r.get("per_model") or {})
                                          .get(m) or {}).get("p_yes"))
        for key, bucket in (("market_class", "by_category"),
                            ("market_class", "by_latency_class")):
            groups = {}
            for row in rows:
                groups.setdefault(row.get(key), []).append(row)
            report[bucket] = {str(k): _score_group(v, lambda r: r.get("p_meta"))
                              for k, v in groups.items()}
        horizons = {}
        for row in rows:
            horizons.setdefault(_horizon_bucket(row.get("time_to_resolution_s")),
                                []).append(row)
        report["by_horizon"] = {k: _score_group(v, lambda r: r.get("p_meta"))
                                for k, v in horizons.items()}
        buckets = {}
        for row in rows:
            buckets.setdefault(_confidence_bucket(row.get("confidence")),
                               []).append(row)
        report["by_confidence_bucket"] = {
            k: _score_group(v, lambda r: r.get("p_meta"))
            for k, v in buckets.items()}
        if not priced:
            report["net_pnl_after_inference_cost"] = None
            report["net_pnl_note"] = (
                "withheld: ALPHA_PRICE_IN_PER_MTOK / _OUT_PER_MTOK are unset, "
                "so inference cost is 0.0 by default rather than by "
                "measurement. Set the real per-token rates before reading any "
                "net-of-AI-cost figure.")
        else:
            gross = report["ensemble"].get("hypothetical_gross_pnl") or 0.0
            report["net_pnl_after_inference_cost"] = round(
                gross - report["inference_cost_usd_total"], 8)
        return report


def score_prediction(prediction: dict, outcome: int) -> dict:
    """Brier, log loss and the hypothetical trade this prediction implies.

    The hypothetical trade is what the shadow record is FOR: it is the
    counterfactual PnL of having taken the side the ensemble preferred, at
    the price on the book at the time, with no market impact assumed. It is
    an upper bound on what execution would have achieved, and it is labelled
    hypothetical everywhere it appears.
    """
    p_meta = prediction.get("p_meta")
    out = {"brier_score": None, "log_loss": None,
           "hypothetical_trade_price": None, "hypothetical_pnl": None}
    if type(outcome) is not int or outcome not in (0, 1):
        return out
    if not _finite(p_meta):
        return out
    p = float(p_meta)
    out["brier_score"] = round((p - outcome) ** 2, 8)
    eps = 1e-12
    q = min(max(p, eps), 1 - eps)
    out["log_loss"] = round(-(outcome * math.log(q)
                              + (1 - outcome) * math.log(1 - q)), 8)
    side = prediction.get("side")
    price = prediction.get("entry_price")
    if side in ("yes", "no") and _finite(price):
        price = float(price)
        won = (outcome == 1) if side == "yes" else (outcome == 0)
        out["hypothetical_trade_price"] = round(price, 6)
        out["hypothetical_pnl"] = round((1.0 - price) if won else -price, 6)
    return out


def _score_group(rows, probability_of) -> dict:
    rows = [row for row in rows if type(row.get("actual_outcome")) is int
            and row["actual_outcome"] in (0, 1)]
    briers, losses, pnls, count = [], [], [], 0
    for row in rows:
        p = probability_of(row)
        if not _finite(p):
            continue
        outcome = int(row["actual_outcome"])
        count += 1
        briers.append((float(p) - outcome) ** 2)
        eps = 1e-12
        q = min(max(float(p), eps), 1 - eps)
        losses.append(-(outcome * math.log(q) + (1 - outcome) * math.log(1 - q)))
        if _finite(row.get("hypothetical_pnl")):
            pnls.append(float(row["hypothetical_pnl"]))
    if count == 0:
        return {"samples": 0, "brier": None, "log_loss": None,
                "accuracy": None, "calibration_error": None,
                "hypothetical_gross_pnl": None,
                "hypothetical_pnl_mean": None}
    correct = 0
    for row in rows:
        p = probability_of(row)
        if not _finite(p):
            continue
        correct += int((float(p) >= 0.5) == (int(row["actual_outcome"]) == 1))
    return {
        "samples": count,
        "brier": round(sum(briers) / count, 8),
        "log_loss": round(sum(losses) / count, 8),
        "accuracy": round(correct / count, 6),
        "calibration_error": _calibration_error(rows, probability_of),
        "hypothetical_gross_pnl": round(sum(pnls), 8) if pnls else None,
        "hypothetical_pnl_mean": round(sum(pnls) / len(pnls), 8) if pnls else None,
    }


def _calibration_error(rows, probability_of, bins: int = 10):
    """Expected calibration error over equal-width probability bins."""
    buckets = {}
    total = 0
    for row in rows:
        p = probability_of(row)
        if not _finite(p):
            continue
        index = min(bins - 1, int(float(p) * bins))
        buckets.setdefault(index, []).append((float(p),
                                              int(row["actual_outcome"])))
        total += 1
    if total == 0:
        return None
    error = 0.0
    for pairs in buckets.values():
        mean_p = sum(p for p, _ in pairs) / len(pairs)
        observed = sum(o for _, o in pairs) / len(pairs)
        error += (len(pairs) / total) * abs(mean_p - observed)
    return round(error, 8)


def _failure_rates(cost_rows) -> dict:
    per = {}
    for row in cost_rows:
        provider = row.get("provider") or "unknown"
        stats = per.setdefault(provider, {"calls": 0, "excluded": 0,
                                          "reasons": {}})
        stats["calls"] += 1
        if row.get("outcome") != "VALID":
            stats["excluded"] += 1
            reason = row.get("reason") or "unknown"
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    for stats in per.values():
        stats["failure_rate"] = (round(stats["excluded"] / stats["calls"], 6)
                                 if stats["calls"] else None)
    return per


def _expiration_rate(predictions) -> float:
    if not predictions:
        return None
    stale = sum(1 for p in predictions
                if p.get("state") in ("STALE", "ANALYSIS_TIMEOUT"))
    return round(stale / len(predictions), 6)


def _mean(values):
    numbers = [float(v) for v in values if _finite(v)]
    return round(sum(numbers) / len(numbers), 4) if numbers else None


def _horizon_bucket(seconds):
    if not _finite(seconds):
        return "unknown"
    seconds = float(seconds)
    if seconds <= 900:
        return "<=15m"
    if seconds <= 21600:
        return "<=6h"
    if seconds <= 86400:
        return "<=24h"
    return ">24h"


def _confidence_bucket(confidence):
    if not _finite(confidence):
        return "unknown"
    return f"{int(float(confidence) * 5) / 5:.1f}"
