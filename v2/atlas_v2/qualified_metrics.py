"""Protocol metrics. Diagnostic math is never a substitute for native admission."""
from .domain import Refused, decimal, utc
from .learning_reward import reward_summary
from .training_protocol import scores


def predictive_metrics(rows):
    return {"model": scores(rows), "market": scores(rows,"market_probability"),
            "ask": scores(rows,"ask_baseline"), "distinct_events": len({r["event_id"] for r in rows}),
            "distinct_utc_days": len({utc(r["observed_at"]).date() for r in rows}),
            "independence_established": False}


def economic_reward(pairs, *, as_of):
    """Pure diagnostics only; production must authenticate all fills and controls.

    Supplied native/source flags are not evidence. Synthetic marked input is refused
    even for diagnostics. Live Phase 2 has no accepted fill writer, so it cannot
    admit this diagnostic output as a qualified economic reward.
    """
    pairs = list(pairs)
    if any(d.get("source_kind") != "NATIVE" for d,s in pairs):
        raise Refused("synthetic/unknown rows cannot earn learning reward")
    summary = reward_summary(pairs,"100",as_of=as_of)
    result = {**summary, "normalized_score": None, "reward_total": None,
              "unit_dollars": 1, "runtime_admission": False, "components": None}
    if summary["net_pnl"] is None or summary["sample_variance_dollars_squared"] is None:
        return result
    accepted = [d for d,s in pairs if d["status"] == "ACCEPTED"]
    n = len(accepted)
    ece = sum(b["count"]/n*abs(float(b["mean_probability"])-float(b["observed_rate"])) for b in summary["calibration"])
    slip = sum(float(decimal(d["slippage_total"])) for d in accepted)/n
    # Unknown risk measurements cannot silently turn into zero penalties.
    if any(type(d.get(k)) is not bool for d in accepted for k in ("stale_quote_exposure", "liquidity_failure")):
        return dict(result, reason="RISK_MEASUREMENTS_MISSING")
    components = {"mean_net_pnl_units": float(summary["net_pnl"])/n,
                  "mean_brier_advantage": float(summary["mean_brier_improvement"]),
                  "positive_day_fraction": summary["period_consistency"]["positive_days"]/summary["period_consistency"]["days"],
                  "ece10": ece, "drawdown_fraction": float(summary["drawdown_fraction"]),
                  "variance_units_squared": float(summary["sample_variance_dollars_squared"]),
                  "slippage_risk_surcharge": slip,
                  "stale_rate": sum(d["stale_quote_exposure"] for d in accepted)/n,
                  "liquidity_failure_rate": sum(d["liquidity_failure"] for d in accepted)/n}
    positives={"mean_net_pnl_units","mean_brier_advantage","positive_day_fraction"}
    score = sum(v if k in positives else -v for k,v in components.items())
    return dict(result, normalized_score=score,reward_total=n*score,components=components)


def daily_report(decisions, rows, day, *, at, challengers, disqualified):
    """Counts use decision time and actual label availability, not settlement backdating."""
    new = [r for r in decisions if utc(r["decision_at"]).date().isoformat() == day]
    settled = [r for r in rows if utc(r["label_available_at"]).date().isoformat() == day]
    families = sorted({r["family"] for r in decisions})
    return {"schema": "atlas-daily-learning/1", "day_utc": day, "knowledge_cutoff": at,
            "new_qualified_decisions": len(new), "new_settlements": len(settled),
            "new_distinct_events": len({r["event_id"] for r in new}),
            "reward_total": None, "net_hypothetical_pnl": None, "drawdown": None,
            "economic_reason": "NO_QUALIFIED_ACCEPTED_SHADOW_FILLS",
            "by_family": {f: {"new_settlements": len([r for r in settled if r["family"]==f]),
                              "daily": predictive_metrics([r for r in settled if r["family"]==f]),
                              "cumulative": predictive_metrics([r for r in rows if r["family"]==f])} for f in families},
            "calibration_method": "10 fixed equal-width bins, reported separately per family",
            "rejected_candidates": disqualified, "challenger_status": challengers,
            "capital": "OFF", "broker_writes": 0, "real_orders_submitted": 0, "promotion": False}
