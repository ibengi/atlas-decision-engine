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
import logging
import math
import os
from datetime import datetime, timezone

from config import CFG, _p
from durable_append import (append_line, exclusive_lock,
                            serialized_append, tail_is_torn)

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
#: AA-13 (re-audit). The RECEIPT for a prediction, appended and fsynced in
#: its own right AFTER the prediction row.
#:
#: `prediction_is_committed()` used to answer by reading the ledger back and
#: finding the PREDICTION row. That conflates two different facts: `write()`
#: returning means the bytes are in the page cache, where they read back
#: perfectly while still being one power cut from never having existed, and
#: only a successful `fsync` means they reached the device. When the fsync
#: failed, `record_prediction` raised -- and the same bytes still read back,
#: so the commit check said "safe" about a prediction the caller had just
#: been told was lost, and the service published a TERMINAL acknowledgement
#: for it.
#:
#: A receipt cannot be produced by bytes existing. It is written only after
#: the prediction's own append has completed AND been fsynced, so its
#: presence is evidence of a completed durable sequence rather than of a
#: readable page.
ROW_COMMIT = "COMMIT"

#: RA-08 -- THE SAME RECEIPT, FOR THE SAME REASON, ONE STEP EARLIER.
#:
#: `prepare()` was idempotent by LOOKUP: `find_prepare(analysis_id)` and, if a
#: row came back, return it. That row is READ from the file, which is exactly
#: what the AA-13 re-audit established is not proof of durability -- and the
#: reason `ROW_COMMIT` exists for predictions.
#:
#: The failure is the ordinary one. An append whose `write` landed and whose
#: `fsync` failed leaves bytes that read back perfectly while still being one
#: power cut from never having existed. `prepare()` raised on that attempt, so
#: the service deferred and spent nothing -- correct. On the NEXT poll
#: `find_prepare` returned those same readable bytes, `prepare()` returned
#: without touching the device, the dispatch precondition was declared
#: satisfied, and every provider was paid against a ledger that was still not
#: writable, so the prediction that followed could not be committed either.
#:
#: A PREPARE is durable when its receipt is, and appending the receipt is what
#: makes the row before it durable: an fsync flushes the whole file. So a
#: retry that finds a receipt-less PREPARE FINISHES it rather than trusting
#: the read, and dispatch is gated on the receipt.
ROW_PREPARE_COMMIT = "PREPARE_COMMIT"

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
    """Recompute a prediction's source digest from the evidence it carries.

    AA-15 (re-audit). The spool is bounded and pruned, so by the time a
    settlement arrives the bytes the digest describes are usually gone. A
    prediction row therefore carries the canonical source content itself, and
    this recomputes the digest from it.

    Returns a verdict rather than raising: "this prediction's evidence cannot
    be re-verified" is a fact an auditor needs reported, not an exception to
    be caught somewhere far from the row it concerns.
    """
    from candidate_contract import compute_checksum
    binding = ((prediction or {}).get("source_binding") or {}) \
        if isinstance(prediction, dict) else {}
    claimed = str(binding.get("record_sha256") or "")
    evidence = binding.get("source_evidence")
    if not isinstance(evidence, dict) or not evidence:
        return {"verified": False, "claimed": claimed, "recomputed": None,
                "reason": "the prediction carries no source evidence, so its "
                          "digest cannot be re-verified once the spool has "
                          "been pruned"}
    if not claimed:
        return {"verified": False, "claimed": "", "recomputed": None,
                "reason": "the prediction carries evidence but no verified "
                          "digest to check it against"}
    try:
        recomputed = compute_checksum({**evidence, "record_sha256": claimed})
    except Exception as exc:                                  # noqa: BLE001
        return {"verified": False, "claimed": claimed, "recomputed": None,
                "reason": f"evidence cannot be canonicalized: "
                          f"{type(exc).__name__}: {exc}"}
    if recomputed != claimed:
        return {"verified": False, "claimed": claimed,
                "recomputed": recomputed,
                "reason": f"source evidence digest mismatch: the persisted "
                          f"evidence hashes to {recomputed}, the prediction "
                          f"claims {claimed}"}
    return {"verified": True, "claimed": claimed, "recomputed": recomputed,
            "reason": ""}


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

    def rows(self) -> list:
        """Every parseable row. A torn LAST line is a crash mid-append and
        is skipped; a bad line anywhere else is reported and skipped, never
        silently treated as the end of the file."""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
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
        return out


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

    def prepare(self, market_snapshot_id: str, *, contract_id: str = "",
                source_record_sha256: str = "", environment: str = "") -> dict:
        """Announce an analysis BEFORE it is attempted (AA-13, step 1).

        Idempotent per snapshot: a retry after a crash re-announces the SAME
        `analysis_id`, because that identity is derived from the snapshot's
        content and not from the clock.

        RA-08: idempotent, and DURABLE ON EVERY PATH. A receipt-less PREPARE
        found by a retry is COMPLETED here rather than believed -- see
        `ROW_PREPARE_COMMIT`. Either this returns a row whose receipt is on
        disk, or it raises; there is no outcome in which a caller may treat
        readable bytes as an announced analysis.
        """
        analysis_id = analysis_identity(market_snapshot_id)
        with self.log.lock():
            existing = self.find_prepare(analysis_id)
            if existing is not None:
                if analysis_id in self.prepare_commits():
                    return existing
                # Readable bytes with no receipt: the previous attempt's
                # append landed and its fsync did not. Refusing here would
                # strand the snapshot forever, and returning would repeat the
                # defect. Finish the durability instead: this append fsyncs
                # the whole file, so the row above becomes durable at the same
                # moment its receipt does. If that fails too, it raises and
                # nothing is dispatched.
                log.warning(
                    f"[ALPHA_LEDGER] {analysis_id} has a PREPARE row with no "
                    f"receipt; completing its durability instead of trusting "
                    f"the bytes")
                self._commit_prepare(analysis_id, market_snapshot_id)
                return existing
            row = self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_PREPARE,
                "at": _now_iso(), "analysis_id": analysis_id,
                "market_snapshot_id": market_snapshot_id,
                "contract_id": contract_id,
                "source_record_sha256": source_record_sha256,
                "environment": environment})
            self._commit_prepare(analysis_id, market_snapshot_id)
            return row

    def _commit_prepare(self, analysis_id: str, snapshot_id: str) -> dict:
        """Append the receipt that makes a PREPARE durably announced (RA-08).

        Caller holds the writer lock.
        """
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_PREPARE_COMMIT,
            "at": _now_iso(), "analysis_id": analysis_id,
            "market_snapshot_id": snapshot_id})

    def prepare_commits(self) -> dict:
        """analysis_id -> the first PREPARE receipt for it (RA-08)."""
        out = {}
        for row in self.rows():
            if row.get("kind") == ROW_PREPARE_COMMIT \
                    and row.get("analysis_id"):
                out.setdefault(row["analysis_id"], row)
        return out

    def prepare_is_durable(self, analysis_id: str) -> bool:
        """RA-08: is this analysis DURABLY announced, receipt and all?

        The dispatch precondition. Answered by the receipt, never by the
        readability of the PREPARE row.
        """
        return bool(analysis_id) and analysis_id in self.prepare_commits()

    def find_prepare(self, analysis_id: str):
        """The PREPARE ROW for an analysis identity, if any.

        Says nothing about durability -- see `prepare_is_durable`.
        """
        for row in self.rows():
            if row.get("kind") == ROW_PREPARE \
                    and row.get("analysis_id") == analysis_id:
                return row
        return None

    def record_prediction(self, opportunity: dict) -> dict:
        """Persist one prediction BEFORE resolution (section 13; AA-13 step 2).

        The row carries the full snapshot so the prediction can be audited
        against the exact market state it was made on, without trusting a
        later lookup.

        AA-14: the uniqueness check and the append happen under ONE exclusive
        lock. Previously they were two separate operations, so two Alpha
        writers could both read "not recorded" and both append, producing two
        predictions for one snapshot and double-counting it in every
        calibration number afterwards.
        """
        prediction_id = opportunity["prediction_id"]
        snapshot_id = str(opportunity.get("market_snapshot_id") or "")
        # A prediction with no snapshot id has no ANALYSIS identity to be
        # unique on. Deriving one from the empty string would make every such
        # row collide with every other, so the analysis-level check below is
        # skipped and only the prediction_id uniqueness rule applies.
        analysis_id = analysis_identity(snapshot_id) if snapshot_id else ""
        with self.log.lock():
            if self.find_prediction(prediction_id) is not None:
                raise LedgerError(f"prediction {prediction_id} already "
                                  f"recorded; a prediction is written once")
            # AA-13: one snapshot yields at most ONE committed prediction.
            # Without this, a crash between the prediction append and the
            # processed acknowledgement produced a SECOND prediction for the
            # same evidence on the next poll.
            existing = (self.find_prediction_by_analysis(analysis_id)
                        if analysis_id else None)
            if existing is not None:
                if analysis_id in self.commits():
                    raise LedgerError(
                        f"analysis {analysis_id} already has committed "
                        f"prediction {existing.get('prediction_id')}; this "
                        f"snapshot is not analysed twice")
                # A row with no receipt: the previous attempt's append landed
                # but its fsync failed, so the caller was told it was lost.
                # Refusing here would poison the snapshot permanently -- it
                # could never be committed and never be retried.
                #
                # The right recovery is to FINISH the commit rather than
                # start another. Appending the receipt fsyncs the file, and an
                # fsync flushes the whole file, so the earlier row becomes
                # durable at the same moment its receipt does. One prediction,
                # one identity, and the original id is the one that survives.
                log.warning(
                    f"[ALPHA_LEDGER] {analysis_id} has an uncommitted "
                    f"prediction row ({existing.get('prediction_id')}); "
                    f"completing its commit instead of writing a second one")
                self._commit(analysis_id, existing.get("prediction_id"),
                             snapshot_id)
                return existing
            row = self.log.append({
                "schema": LEDGER_SCHEMA, "kind": ROW_PREDICTION,
                "at": _now_iso(), "analysis_id": analysis_id, **opportunity})
            # AA-13 (re-audit): the RECEIPT. Written only because the append
            # above returned, which happens only after its own fsync
            # succeeded. If that fsync failed we never reach this line, the
            # caller gets a LedgerError, and `prediction_is_committed` reads
            # no receipt -- so the readable bytes of a failed write can no
            # longer be mistaken for a durable commit.
            self._commit(analysis_id, prediction_id, snapshot_id)
            return row

    def _commit(self, analysis_id: str, prediction_id: str,
                snapshot_id: str) -> dict:
        """Append the receipt that makes a prediction durably committed.

        Caller holds the writer lock. Appending fsyncs the file, so the
        prediction row this receipt names is durable by the time the receipt
        itself is.
        """
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_COMMIT,
            "at": _now_iso(), "analysis_id": analysis_id,
            "prediction_id": prediction_id,
            "market_snapshot_id": snapshot_id})

    def find_prediction_by_analysis(self, analysis_id: str):
        """The prediction ROW for a stable analysis identity, if any.

        Says nothing about durability -- see `committed_prediction`.
        """
        if not analysis_id:
            return None
        for row in self.predictions():
            if row.get("analysis_id") == analysis_id:
                return row
        return None

    def commits(self) -> dict:
        """analysis_id -> the first COMMIT receipt for it."""
        out = {}
        for row in self.rows():
            if row.get("kind") == ROW_COMMIT and row.get("analysis_id"):
                out.setdefault(row["analysis_id"], row)
        return out

    def committed_prediction(self, market_snapshot_id: str):
        """The DURABLY COMMITTED prediction row for a snapshot, or None.

        Both halves are required (AA-13 re-audit): the prediction row, and a
        receipt proving the sequence that wrote it completed. A row without a
        receipt is a write we are not entitled to call durable, and a receipt
        without a row is a ledger somebody has edited.

        This is also the recovery entry point (AA-13, correction 7): a
        restart asks it BEFORE dispatching anything, so an analysis that was
        already committed is recovered by identity instead of being paid for
        a second time.
        """
        analysis_id = analysis_identity(market_snapshot_id)
        if analysis_id not in self.commits():
            return None
        return self.find_prediction_by_analysis(analysis_id)

    def prediction_is_committed(self, market_snapshot_id: str) -> bool:
        """AA-13: has this snapshot's prediction been DURABLY committed?

        The terminal processed acknowledgement must be published only after
        this returns True, and a restart re-asks it rather than trusting the
        processed file. Since the re-audit it is answered by the RECEIPT, not
        by the readability of the prediction row.
        """
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
            row["actual_outcome"] = int(resolution["actual_outcome"])
            row["resolved_at"] = resolution.get("resolved_at")
            # AA-15 (re-audit): the settlement's identity and trust decision
            # travel WITH the outcome into learning. A calibration number is
            # only as good as the authority that produced the outcomes it was
            # computed from, and a learning row that cannot name that
            # authority cannot be audited afterwards.
            row["resolution_source"] = resolution.get("resolution_source", "")
            row["settlement_binding"] = dict(
                resolution.get("settlement_binding") or {})
            row["settlement_evidence_id"] = resolution.get(
                "settlement_evidence_id")
            row["binding_verified"] = bool(resolution.get("binding_verified"))
            row["source_trusted"] = bool(resolution.get("source_trusted"))
            invalidation = invalidations.get(prediction.get("prediction_id"))
            row["invalidated"] = bool(invalidation)
            row["invalidation_reason"] = (invalidation or {}).get("reason")
            row["observations"] = [
                {"interval_s": o.get("interval_s"), "quote": o.get("quote")}
                for o in observations.get(prediction.get("prediction_id"), [])]
            row.update(score_prediction(prediction, row["actual_outcome"]))
            joined.append(row)
        return joined

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
        samples, total = 0, 0.0
        for row in self.resolved():
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
