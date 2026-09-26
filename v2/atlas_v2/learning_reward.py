"""Pure offline shadow diagnostics; hashes bind inputs, never authenticate sources.

Costs are total dollars, charged once at frozen entry. Probability is selected-side.
There is deliberately no mixed dollar/probability scalar or approval capability.
"""
from decimal import Decimal

from .domain import Refused, decimal, digest, hash_id, utc

FILL_FIELDS = ("decision_id", "candidate_hash", "ticker", "side", "decision_at",
               "close_at", "status", "size", "entry_price", "fee_total", "slippage_total", "probability",
               "market_probability", "fill_receipt_hash", "cost_receipt_hash")


def frozen_fill_hash(decision):
    """Bind ex-ante inputs; persistence/authentication remains the caller's duty."""
    return digest({k: decision[k] for k in FILL_FIELDS})


def reward_trade(decision, settlement, *, as_of):
    """No settlement, rejected decision, or missing cost evidence means null PnL.

    Bypass/control violations disqualify regardless of hypothetical profitability.
    Ordinary guard rejection belongs in status=REJECTED, not control_violations.
    """
    at = utc(as_of)
    status = decision.get("status")
    if status not in {"ACCEPTED", "REJECTED"}:
        raise Refused("explicit shadow acceptance status required")
    disqualified = bool(decision.get("bypass_attempts") or decision.get("control_violations"))
    result = {"decision_id": decision.get("decision_id"), "status": status,
              "qualification": "DIAGNOSTIC_ONLY_UNAUTHENTICATED_INPUTS",
              "financial_authority": False, "promotion_allowed": False,
              "candidate_disqualified": disqualified, "reward": None,
              "net_pnl": None, "brier_improvement": None, "log_loss_improvement": None}
    if disqualified:
        return dict(result, reason="CANDIDATE_DISQUALIFIED")
    if status == "REJECTED":
        return dict(result, reason="REJECTED_NO_POSITION")
    if any(decision.get(k) is None for k in FILL_FIELDS) or not decision.get("frozen_fill_hash"):
        return dict(result, reason="MISSING_FROZEN_ECONOMIC_EVIDENCE")
    if frozen_fill_hash(decision) != decision["frozen_fill_hash"]:
        raise Refused("frozen economic inputs changed")
    for key in ("candidate_hash", "fill_receipt_hash", "cost_receipt_hash"):
        hash_id(decision[key])
    if not decision["decision_id"] or not decision["ticker"] or decision["side"] not in {"yes", "no"}:
        raise Refused("decision identity/side")
    decided = utc(decision["decision_at"])
    closed = utc(decision["close_at"])
    if decided > at or decided >= closed:
        raise Refused("future or after-close decision")
    size, price, fee, slip, p, b = map(decimal, (decision[k] for k in
        ("size", "entry_price", "fee_total", "slippage_total", "probability", "market_probability")))
    if not (size > 0 and 0 < price < 1 and fee >= 0 and slip >= 0 and 0 < p < 1 and 0 < b < 1):
        raise Refused("invalid frozen economic values")
    if settlement is None or any(settlement.get(k) is None for k in
            ("ticker", "outcome", "settled_at", "published_at", "receipt_hash")):
        return dict(result, reason="UNSETTLED_OR_MISSING_LABEL_EVIDENCE")
    hash_id(settlement["receipt_hash"])
    if settlement["ticker"] != decision["ticker"] or type(settlement["outcome"]) is not int or settlement["outcome"] not in (0, 1):
        raise Refused("settlement identity/outcome")
    settled, published = utc(settlement["settled_at"]), utc(settlement["published_at"])
    if not decided < closed <= settled <= published:
        raise Refused("settlement chronology")
    if published > at:
        return dict(result, reason="LABEL_NOT_YET_AVAILABLE")
    y = Decimal(settlement["outcome"] if decision["side"] == "yes" else 1-settlement["outcome"])
    cost = size*price + fee + slip
    pnl = size*y-cost
    model_brier, market_brier = (p-y)**2, (b-y)**2
    model_log = -(p.ln() if y else (1-p).ln())
    market_log = -(b.ln() if y else (1-b).ln())
    vector = {"net_pnl_dollars": str(pnl), "brier_improvement": str(market_brier-model_brier),
              "log_loss_improvement": str(market_log-model_log)}
    return dict(result, reason="SETTLED_DIAGNOSTIC_ONLY", reward=vector, net_pnl=str(pnl),
                brier_improvement=vector["brier_improvement"], log_loss_improvement=vector["log_loss_improvement"],
                cost_total=str(cost), payout=str(size*y), model_probability=str(p),
                market_probability=str(b), selected_side_outcome=int(y),
                period=decided.date().isoformat(), available_at=settlement["published_at"])


def reward_summary(decision_settlement_pairs, hypothetical_starting_equity, *, as_of):
    """Recompute from bound inputs, never accept caller PnL or independence labels.

    Drawdown uses realized settlement cash PnL only; it is not intratrade drawdown.
    Any unresolved accepted trade blocks aggregate economics, avoiding winner-only
    scoring. Calendar-day consistency is descriptive, not independence evidence.
    """
    equity = decimal(hypothetical_starting_equity)
    if equity <= 0:
        raise Refused("positive explicit hypothetical starting equity required")
    pairs = list(decision_settlement_pairs)
    identities = [d.get("decision_id") for d, _ in pairs]
    if any(not isinstance(i, str) or not i for i in identities) or len(set(identities)) != len(identities):
        raise Refused("unique complete decision cohort required")
    if len({d.get("candidate_hash") for d, _ in pairs}) > 1:
        raise Refused("one candidate per reward cohort")
    rewards = [reward_trade(d, s, as_of=as_of) for d, s in pairs]
    disqualified = any(r["candidate_disqualified"] for r in rewards)
    pending = sum(r["status"] == "ACCEPTED" and r["reward"] is None for r in rewards)
    output = {"qualification": "DIAGNOSTIC_ONLY_UNAUTHENTICATED_INPUTS", "financial_authority": False,
              "promotion_allowed": False, "independence_established": False,
              "candidate_disqualified": disqualified, "decisions": len(rewards), "unscored_accepted": pending,
              "net_pnl": None, "drawdown_dollars": None, "drawdown_fraction": None,
              "sample_variance_dollars_squared": None, "period_consistency": None,
              "calibration": None, "calibration_scope": "ACCEPTED_SETTLED_COHORT_ONLY",
              "hypothetical_starting_equity": str(equity), "rewards": rewards}
    if disqualified or pending:
        return output
    scored = sorted((r for r in rewards if r["reward"] is not None),
                    key=lambda r: (utc(r["available_at"]), r["decision_id"]))
    if not scored:
        return output
    pnls = [decimal(r["net_pnl"]) for r in scored]
    peak, dd, fraction = equity, Decimal(0), Decimal(0)
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd, fraction = max(dd, peak-equity), max(fraction, (peak-equity)/peak)
    mean = sum(pnls)/len(pnls)
    variance = sum((x-mean)**2 for x in pnls)/(len(pnls)-1) if len(pnls)>1 else None
    periods, calibration = {}, []
    for r in scored:
        periods.setdefault(r["period"], []).append(decimal(r["net_pnl"]))
    for bucket in range(10):
        rows = [r for r in scored if int(decimal(r["model_probability"])*10) == bucket]
        if rows:
            calibration.append({"bin": bucket, "count": len(rows),
                "mean_probability": str(sum(decimal(r["model_probability"]) for r in rows)/len(rows)),
                "observed_rate": str(Decimal(sum(r["selected_side_outcome"] for r in rows))/len(rows))})
    return dict(output, net_pnl=str(sum(pnls)), drawdown_dollars=str(dd), drawdown_fraction=str(fraction),
                drawdown_kind="REALIZED_SETTLEMENT_PNL_ONLY", sample_variance_dollars_squared=str(variance) if variance is not None else None,
                mean_brier_improvement=str(sum(decimal(r["brier_improvement"]) for r in scored)/len(scored)),
                mean_log_loss_improvement=str(sum(decimal(r["log_loss_improvement"]) for r in scored)/len(scored)),
                calibration=calibration, period_consistency={"kind": "DESCRIPTIVE_UTC_CALENDAR_DAYS",
                    "days": len(periods), "positive_days": sum(sum(v)>0 for v in periods.values()),
                    "net_pnl_by_day": {k: str(sum(v)) for k,v in sorted(periods.items())}})
