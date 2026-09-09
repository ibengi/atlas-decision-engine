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

log = logging.getLogger("ALPHA")

LEDGER_SCHEMA = "atlas-alpha-ledger-v1"
ROW_PREDICTION = "PREDICTION"
ROW_RESOLUTION = "RESOLUTION"
ROW_COST = "COST"
ROW_KINDS = (ROW_PREDICTION, ROW_RESOLUTION, ROW_COST)


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
        line = json.dumps(row, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str) + "\n"
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o644)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            raise LedgerError(f"alpha ledger row not durable: {e}")
        return row

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

    def record_prediction(self, opportunity: dict) -> dict:
        """Persist one prediction BEFORE resolution (section 13).

        The row carries the full snapshot so the prediction can be audited
        against the exact market state it was made on, without trusting a
        later lookup.
        """
        prediction_id = opportunity["prediction_id"]
        if self.find_prediction(prediction_id) is not None:
            raise LedgerError(f"prediction {prediction_id} already recorded; "
                              f"a prediction is written once")
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_PREDICTION,
            "at": _now_iso(), **opportunity})

    def resolve(self, prediction_id: str, outcome, *, resolved_at=None,
                source: str = "") -> dict:
        """Record the outcome as a NEW row.

        `outcome` is 1 for YES, 0 for NO. The prediction row is not touched:
        the ledger is replayed to build the resolved view, so "what did we
        predict" and "what happened" can never be conflated into one
        editable record.
        """
        prediction = self.find_prediction(prediction_id)
        if prediction is None:
            raise LedgerError(f"unknown prediction {prediction_id}")
        if self.find_resolution(prediction_id) is not None:
            raise LedgerError(f"prediction {prediction_id} is already "
                              f"resolved; outcomes are written once")
        if outcome not in (0, 1, True, False):
            raise LedgerError(f"outcome {outcome!r} must be 0 or 1")
        return self.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_RESOLUTION,
            "at": _now_iso(), "prediction_id": prediction_id,
            "actual_outcome": int(bool(outcome)),
            "resolved_at": resolved_at or _now_iso(),
            "resolution_source": source})

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
        joined = []
        for prediction in self.predictions():
            resolution = outcomes.get(prediction.get("prediction_id"))
            if resolution is None:
                continue
            row = dict(prediction)
            row["actual_outcome"] = int(resolution["actual_outcome"])
            row["resolved_at"] = resolution.get("resolved_at")
            row.update(score_prediction(prediction, row["actual_outcome"]))
            joined.append(row)
        return joined

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
