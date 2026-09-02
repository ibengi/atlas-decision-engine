"""
btc_daily_evidence.py — T7-I. Durable prediction/outcome evidence for the
BTC daily strategy, so it can eventually be calibrated.

WHY A SEPARATE STORE
    `ShadowPredictionStore` already records predictions AND settles them, but
    `_shadow_observer` admits only strategies named `btc15m*`, so
    `btc_daily_above_strike` has never produced a single settled prediction.

    The fix is NOT to widen that filter. `backtest_btc15m.py` consumes the
    same store, applies no strategy filter, and buckets by
    `minutes_remaining` with 15-minute edges (0-5, 5-10, 10-15+). Daily
    records dropped into it would land silently in the last bucket and
    contaminate a 15-minute analysis. So daily evidence gets its own
    append-only store, and the btc15m dataset is left byte-identical.

GUARANTEES
    * Append-only. A prediction row is never mutated; settlement is a
      SEPARATE row joined on `record_id`.
    * Deterministic `record_id` derived from record content, so replaying the
      same decision yields the same id and duplicates are detectable.
    * Never raises into a trading cycle. Every public method swallows its own
      errors and returns a falsy result; losing evidence must never change,
      create, or prevent an order.
    * Bounded settlement cost: a market is polled only once its close time has
      passed. Unmatured predictions cost zero API calls.
    * Reads nothing the engine writes elsewhere and writes nothing the engine
      reads. No gate, model, sizing, or order path is touched.

CALIBRATION
    `calibration_records()` emits `probability_yes` + `result` pairs, which is
    exactly the contract `calibration.py` consumes, filterable by model
    version, strategy and horizon bucket so versions and horizons are never
    silently pooled.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("EVIDENCE")

EVIDENCE_SCHEMA_VERSION = 1
PREDICTIONS_FILE = "btc_daily_predictions.jsonl"
SETTLEMENTS_FILE = "btc_daily_settlements.jsonl"

#: Market statuses under which the API's `result` is authoritative.
FINALIZED_STATUSES = ("settled", "finalized")

#: SETTLEMENT CONFIRMATION PROTOCOL — why it exists.
#:
#: Measured on 2026-09-02: 62 KXBTCD events that closed with BTC ABOVE the
#: strike (by up to $9,360) carried result="no" in this journal. Every one
#: had been polled within 0.6 h of close, when the demo API answers "no"
#: for a closed-but-undetermined market, and the first non-empty answer
#: was frozen forever. A label written that way is not evidence.
#:
#: A result is therefore accepted only when EITHER the market reports a
#: finalized status, OR the same value is observed twice at least
#: SETTLE_CONFIRM_MIN_S apart with the first observation at least
#: SETTLE_MIN_LAG_S after close. Anything else is recorded as an
#: observation and re-polled — never as a settlement.
SETTLE_MIN_LAG_S = 1800.0
SETTLE_CONFIRM_MIN_S = 1800.0

#: Strike provenances that may enter decisive evidence. `None` is the
#: router's "field" case (it only stamps non-field sources); "ticker" is a
#: real strike parsed from the ticker. "spot_proxy" sets strike := spot and
#: is excluded: it forces d=0 and the model degenerates to its momentum
#: term, so such a row measures nothing about the model.
DECISIVE_STRIKE_SOURCES = (None, "field", "ticker")

#: Horizon buckets required by T7-I Phase 5. Upper bound exclusive, minutes.
HORIZON_BUCKETS = (("<=24h", 0.0, 1440.0), ("24-48h", 1440.0, 2880.0),
                   ("2-3d", 2880.0, 4320.0), ("3-7d", 4320.0, 10080.0),
                   ("7-14d", 10080.0, 20160.0),
                   (">14d", 20160.0, float("inf")))


def horizon_bucket(minutes: Optional[float]) -> Optional[str]:
    """Bucket label, or None when the horizon is unknown. Never guesses."""
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return None
    if m < 0:
        return None
    for name, lo, hi in HORIZON_BUCKETS:
        if lo <= m < hi:
            return name
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _num(value):
    """float(value) or None. A missing input stays missing — never 0.0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _model_hash() -> Optional[str]:
    """Hash of the model source, or None if the model cannot be imported.
    Never raises: evidence capture must not be able to break a cycle."""
    try:
        from btc_probability_model import model_hash
        return model_hash()
    except Exception:                                   # noqa: BLE001
        return None


def make_record_id(*, cycle_id, ticker, observed_at, model_version) -> str:
    """Deterministic id from record content: same decision -> same id."""
    raw = "|".join(str(p or "") for p in
                   (cycle_id, ticker, observed_at, model_version))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_prediction_record(*, decision, model_output, market, book,
                            minutes_remaining, cycle_id, observed_at=None,
                            strategy_version=None, record_id=None,
                            extra=None) -> dict:
    """The T7-I evidence contract. Absent values are recorded as explicit
    null — nothing is invented, and a missing field never becomes a zero.

    T7-K additions, both optional and both defaulting to prior behaviour:
      record_id — supply a caller-derived id instead of the content hash.
        Needed by the pre-liquidity shadow path, whose dedup key must be
        stable across cycles and restarts and therefore must NOT include
        observed_at.
      extra — additional fields merged into the record. Reserved keys are
        never overwritten, so a caller cannot silently redefine the
        contract.
    """
    dec = decision or {}
    mo = model_output or {}
    m = market or {}
    b = book or {}
    feats = mo.get("features") or {}
    observed_at = observed_at or _now_iso()
    model_version = feats.get("model_version")
    ticker = dec.get("ticker") or m.get("ticker")

    yes_bid, yes_ask = b.get("yes_bid"), b.get("yes_ask")
    yes_mid = b.get("yes_mid")
    implied = (_num(yes_mid) / 100.0) if _num(yes_mid) is not None else None

    mins = _num(minutes_remaining)
    if mins is None:
        mins = _num(feats.get("minutes_remaining"))

    row = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "record_id": record_id or make_record_id(
            cycle_id=cycle_id, ticker=ticker, observed_at=observed_at,
            model_version=model_version),
        "decision_id": dec.get("decision_id"),
        "cycle_id": cycle_id,
        "ticker": ticker,
        "series": str(ticker or "").split("-", 1)[0].upper() or None,
        "market_type": dec.get("market_type"),
        "strategy_name": dec.get("strategy"),
        # No strategy carries a version attribute today; recorded as an
        # explicit null rather than silently reusing the model version.
        "strategy_version": strategy_version,
        "model_version": model_version,
        "model_hash": _model_hash() if model_version else None,
        "observed_at": observed_at,
        "market_close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "minutes_remaining": mins,
        "horizon_bucket": horizon_bucket(mins),
        "underlying_price": _num(feats.get("spot")),
        "strike": _num(feats.get("strike")),
        "strike_source": feats.get("strike_source"),
        "market_yes_bid": yes_bid,
        "market_yes_ask": yes_ask,
        "market_implied_probability": implied,
        "predicted_probability": _num(mo.get("probability_yes")),
        "confidence": mo.get("confidence"),
        "edge": _num(dec.get("net_edge")),
        "gross_edge": _num(dec.get("gross_edge")),
        "expected_value": _num(dec.get("net_ev")),
        "data_quality": _num(feats.get("data_quality")),
        "sigma_1m": _num(feats.get("sigma_1m")),
        "sigma_effective": _num(feats.get("sigma_effective")),
        "horizon_mode": feats.get("horizon_mode", "normal"),
        "ret_5m": _num(feats.get("ret_5m")),
        "model_valid": bool(mo.get("valid")),
        "model_reason": mo.get("reason"),
        "decision_accepted": bool(dec.get("accepted")),
        "rejection_reason": dec.get("rejection_reason"),
    }
    for k, v in (extra or {}).items():
        if k not in row:            # the contract's own keys always win
            row[k] = v
    return row


class BtcDailyEvidenceStore:
    """Append-only prediction store with a separate settlement journal."""

    def __init__(self, data_dir: str = "."):
        self.predictions_path = os.path.join(data_dir, PREDICTIONS_FILE)
        self.settlements_path = os.path.join(data_dir, SETTLEMENTS_FILE)

    # ── writing ────────────────────────────────────────────────────────────
    def _append(self, path: str, row: dict) -> bool:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return True
        except (OSError, TypeError, ValueError) as e:
            log.debug(f"[BTC_DAILY_EVIDENCE] write failed: {e}")
            return False

    def record(self, **kwargs) -> Optional[str]:
        """Append one prediction. Returns record_id, or None on any failure.
        Never raises: evidence capture must not be able to break a cycle."""
        try:
            row = build_prediction_record(**kwargs)
        except Exception as e:                            # noqa: BLE001
            log.debug(f"[BTC_DAILY_EVIDENCE] build failed: {e}")
            return None
        if not row.get("ticker"):
            return None
        return row["record_id"] if self._append(self.predictions_path,
                                                row) else None

    # ── reading ────────────────────────────────────────────────────────────
    def _read(self, path: str) -> list:
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue          # a torn line never kills the read
        except OSError:
            return []
        return rows

    def predictions(self) -> list:
        return self._read(self.predictions_path)

    def settlements(self) -> dict:
        """record_id -> FINAL settlement row. Last write wins on replay.

        Observation rows (kind="observation") are the confirmation
        protocol's working state and never count as settled. Rows without a
        `kind` predate the protocol: they were frozen from a first poll and
        are returned here for inventory, but `calibration_records` excludes
        them unless explicitly asked otherwise.
        """
        return {r["record_id"]: r for r in self._read(self.settlements_path)
                if r.get("record_id") and r.get("kind") != "observation"}

    def _observations(self) -> dict:
        """record_id -> list of observation rows, in write order."""
        out = {}
        for r in self._read(self.settlements_path):
            if r.get("kind") == "observation" and r.get("record_id"):
                out.setdefault(r["record_id"], []).append(r)
        return out

    # ── settlement ─────────────────────────────────────────────────────────
    def mature_unsettled(self, now: datetime = None) -> list:
        """Predictions whose market close time has passed and which have no
        settlement row yet. A prediction with no close time is never polled —
        it would cost an unbounded API call with no maturity guarantee."""
        now = now or datetime.now(timezone.utc)
        settled = self.settlements()
        out = []
        for r in self.predictions():
            rid = r.get("record_id")
            if not rid or rid in settled:
                continue
            close = _parse_iso(r.get("market_close_time"))
            if close is None or close > now:
                continue
            out.append(r)
        return out

    def settle(self, get_market_fn, now: datetime = None) -> int:
        """Resolve matured predictions from the API's own `result` field,
        under the confirmation protocol documented at SETTLE_MIN_LAG_S.

        One API call per distinct matured ticker per pass, and none at all
        for a record whose last observation is younger than
        SETTLE_CONFIRM_MIN_S. No settlement is ever assumed without an
        explicit yes/no from the API, and no first-poll answer is ever
        final on its own. Returns the number of FINAL settlement rows
        written; never raises.
        """
        try:
            now = now or datetime.now(timezone.utc)
            pending = self.mature_unsettled(now)
            obs = self._observations()
        except Exception as e:                            # noqa: BLE001
            log.debug(f"[BTC_DAILY_EVIDENCE] settle scan failed: {e}")
            return 0
        if not pending:
            return 0
        seen, written = {}, 0
        for r in pending:
            rid, tk = r["record_id"], r.get("ticker")
            prev = obs.get(rid) or []
            last = prev[-1] if prev else None
            last_at = _parse_iso(last.get("observed_at")) if last else None
            if last_at is not None and \
                    (now - last_at).total_seconds() < SETTLE_CONFIRM_MIN_S:
                continue              # confirmation window still open: no call
            if tk not in seen:
                try:
                    seen[tk] = get_market_fn(tk) or {}
                except Exception as e:                    # noqa: BLE001
                    log.debug(f"[BTC_DAILY_EVIDENCE] get_market({tk}): {e}")
                    seen[tk] = {}
            m = seen[tk]
            result = str(m.get("result") or "").strip().lower()
            status = str(m.get("status") or "").strip().lower()
            if result not in ("yes", "no"):
                continue                  # unresolved stays unresolved
            close = _parse_iso(r.get("market_close_time"))
            lag = (now - close).total_seconds() if close else None
            confirmed_by = None
            if status in FINALIZED_STATUSES:
                confirmed_by = "status"
            elif (close is not None
                  and last is not None and last.get("result") == result
                  and last_at is not None
                  and (last_at - close).total_seconds() >= SETTLE_MIN_LAG_S
                  and (now - last_at).total_seconds() >= SETTLE_CONFIRM_MIN_S):
                # The current poll is necessarily >= MIN_LAG + CONFIRM after
                # close, so no separate check on it is needed.
                confirmed_by = "repeat"
            base = {"schema_version": EVIDENCE_SCHEMA_VERSION,
                    "record_id": rid, "ticker": tk, "result": result,
                    "market_status": status or None,
                    "observed_at": now.isoformat(timespec="seconds"),
                    "settle_lag_s": round(lag, 1) if lag is not None else None}
            if confirmed_by is None:
                self._append(self.settlements_path,
                             {**base, "kind": "observation"})
                continue
            if self._append(self.settlements_path, {
                    **base, "kind": "settlement",
                    "confirmed_by": confirmed_by,
                    "settled_at": now.isoformat(timespec="seconds"),
                    "prior_observations": len(prev),
                    "model_version": r.get("model_version"),
                    "model_hash": r.get("model_hash"),
                    "strategy_name": r.get("strategy_name"),
                    "horizon_bucket": r.get("horizon_bucket")}):
                written += 1
        return written

    # ── calibration surface ────────────────────────────────────────────────
    def calibration_records(self, *, model_version=None, strategy=None,
                            bucket=None, require_confirmed=True,
                            decisive_strike_only=True) -> list:
        """Settled predictions only, in the shape `calibration.py` consumes.

        Unsettled predictions are structurally excluded: a record without a
        settlement row cannot appear here at all. Filters exist so model
        versions and horizons are never pooled without saying so.

        Two integrity filters are ON by default and must be switched off
        explicitly, in code, by whoever wants contaminated rows:
          require_confirmed    — drop settlements not confirmed under the
                                 protocol (legacy first-poll rows included);
          decisive_strike_only — drop rows whose strike was not the market's
                                 own (see DECISIVE_STRIKE_SOURCES).
        """
        settled = self.settlements()
        out = []
        for r in self.predictions():
            s = settled.get(r.get("record_id"))
            if not s or s.get("result") not in ("yes", "no"):
                continue
            if require_confirmed and not s.get("confirmed_by"):
                continue          # frozen first-poll label: not evidence
            if decisive_strike_only and \
                    r.get("strike_source") not in DECISIVE_STRIKE_SOURCES:
                continue          # strike := spot measures nothing
            p = r.get("predicted_probability")
            if p is None:
                continue          # no prediction -> not a calibration sample
            if model_version is not None and r.get("model_version") != model_version:
                continue
            if strategy is not None and r.get("strategy_name") != strategy:
                continue
            if bucket is not None and r.get("horizon_bucket") != bucket:
                continue
            out.append({**r, "probability_yes": p,
                        "result": s["result"],
                        "settled_at": s.get("settled_at"),
                        "confirmed_by": s.get("confirmed_by")})
        return out

    def calibration_report(self, *, bins: int = 10, model_version=None,
                           strategy=None, bucket=None) -> dict:
        """Per-segment calibration. `insufficient_sample` is advisory only —
        acceptance thresholds are the operator's call (T7-I Phase 9)."""
        from calibration import analyze
        rows = self.calibration_records(model_version=model_version,
                                        strategy=strategy, bucket=bucket)
        label = "|".join(str(x) for x in
                         (strategy or "*", model_version or "*", bucket or "*"))
        rep = analyze(rows, bins=bins, label=label)
        rep.update({
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "segment": {"strategy": strategy, "model_version": model_version,
                        "horizon_bucket": bucket},
            "confidence_distribution": _hist(rows, "confidence"),
            "predicted_probability_mean": _mean(rows, "probability_yes"),
            "realized_outcome_rate": _mean(
                [{"y": 1 if r["result"] == "yes" else 0} for r in rows], "y"),
            "unsettled_excluded": True,
        })
        return rep

    def confirmed_events(self) -> list:
        """One consolidated record per CONFIRMED settlement, carrying the
        fields the operator's evidence contract requires. This is the only
        surface that calibration, training or evaluation should read: a
        record appears here only when `confirmed_result` exists.

        `first_result_seen` is the earliest observation the protocol
        journaled for the record (None when the settlement was confirmed
        on its first poll by a finalized status)."""
        final = self.settlements()
        obs = self._observations()
        out = []
        for r in self.predictions():
            s = final.get(r.get("record_id"))
            if not s or not s.get("confirmed_by"):
                continue
            first = (obs.get(r["record_id"]) or [None])[0]
            ask, bid = r.get("market_yes_ask"), r.get("market_yes_bid")
            out.append({
                "record_id": r.get("record_id"),
                "ticker": r.get("ticker"),
                "decision_ts": r.get("observed_at"),
                "close_time": r.get("market_close_time"),
                "strike": r.get("strike"),
                "strike_source": r.get("strike_source") or "field",
                "spot": r.get("underlying_price"),
                "market_probability": r.get("market_implied_probability"),
                "market_yes_bid": bid, "market_yes_ask": ask,
                "spread": ((ask - bid) if isinstance(ask, (int, float))
                           and isinstance(bid, (int, float)) else None),
                "model_probability": r.get("predicted_probability"),
                "model_version": r.get("model_version"),
                "model_hash": r.get("model_hash"),
                "first_result_seen": first.get("result") if first else None,
                "first_result_seen_ts": first.get("observed_at") if first else None,
                "confirmed_result": s.get("result"),
                "confirmed_result_ts": s.get("settled_at"),
                "confirmation_method": s.get("confirmed_by"),
                "settlement_status": s.get("market_status"),
                "settle_lag_s": s.get("settle_lag_s"),
            })
        return out

    def observation_keys(self) -> set:
        """Every `observation_key` already on disk (T7-K dedup index).

        Read once at construction by the shadow evaluator, so a process
        restart cannot re-record an observation the store already holds.
        """
        return {r["observation_key"] for r in self.predictions()
                if r.get("observation_key")}

    def statistics(self) -> dict:
        """T7-K Phase 7 counting. Raw rows and independent outcomes are
        reported separately and never conflated.

        `independent_outcomes` counts DISTINCT settled expiries, not rows:
        every KXBTCD strike sharing a close time resolves from one
        underlying price at one instant, so N strikes on one expiry are one
        realisation of the underlying, not N experiments. Different strikes
        do probe different regions of the predicted distribution, so their
        information is not zero — but it is not independent either, and this
        is the conservative count. No effective-N formula is invented here.
        """
        preds = self.predictions()
        settled = self.settlements()
        s_rows = [r for r in preds if r.get("record_id") in settled]
        return {
            "prediction_rows": len(preds),
            "settled_prediction_rows": len(s_rows),
            "unique_tickers": len({r.get("ticker") for r in preds} - {None}),
            "unique_expiries": len({r.get("market_close_time")
                                    for r in preds} - {None}),
            "settled_unique_tickers": len({r.get("ticker")
                                           for r in s_rows} - {None}),
            "independent_outcomes": len({r.get("market_close_time")
                                         for r in s_rows} - {None}),
            "independent_outcome_basis": "distinct settled market_close_time",
            "by_checkpoint": _hist(preds, "observation_checkpoint"),
            "settled_by_checkpoint": _hist(s_rows, "observation_checkpoint"),
        }

    def coverage(self) -> dict:
        """Evidence inventory. Cheap enough to log every cycle."""
        preds = self.predictions()
        settled = self.settlements()
        by_bucket = {}
        for r in preds:
            b = r.get("horizon_bucket") or "(unknown)"
            e = by_bucket.setdefault(b, {"predictions": 0, "settled": 0})
            e["predictions"] += 1
            if r.get("record_id") in settled:
                e["settled"] += 1
        return {"schema_version": EVIDENCE_SCHEMA_VERSION,
                "predictions": len(preds), "settled": len(settled),
                "settled_confirmed": sum(1 for v in settled.values()
                                         if v.get("confirmed_by")),
                "settled_unconfirmed_legacy": sum(
                    1 for v in settled.values() if not v.get("confirmed_by")),
                "by_horizon_bucket": by_bucket,
                "model_versions": sorted({r.get("model_version")
                                          for r in preds} - {None})}


def _mean(rows, key):
    vals = [_num(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def _hist(rows, key):
    out = {}
    for r in rows:
        v = r.get(key)
        if v is not None:
            out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))
