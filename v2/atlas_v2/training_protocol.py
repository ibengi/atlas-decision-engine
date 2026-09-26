"""Frozen, finite recalibration experiment. Pure calculations confer no authority."""
from collections import Counter
from datetime import timedelta
from itertools import product
import math

from .alpha_lab import HYPOTHESES, plan
from .domain import Refused, digest, utc

START = "2026-09-27T00:00:00Z"
TRAIN_END = "2026-10-04T00:00:00Z"
CAL_END = "2026-10-07T00:00:00Z"
VALID_END = "2026-10-14T00:00:00Z"
FIT_AT = "2026-10-14T01:00:00Z"
DEADLINE = "2026-10-25T00:00:00Z"


def protocol():
    return {
        "schema": "atlas-training-protocol/1", "version": "PHASE2-20260926-1",
        "eligible_not_before": START, "parent_plan_hash": digest(plan()),
        "scope": plan()["scope"], "families": HYPOTHESES,
        "dataset_schema": {
            "identity": ["decision_id", "decision_hash", "event_id", "ticker", "domain", "family", "candidate_hash"],
            "timing": ["decision_at", "observed_at", "close_at", "label_available_at"],
            "inputs": ["features_hash", "feature_snapshot", "probability", "market_probability", "ask_baseline", "protocol_hash"],
            "outcome": ["outcome", "settlement_hash", "settlement_receipt_hash"],
            "admission": "native V2 ledgers; reconstruct raw observations, features and authoritative labels; reject synthetic/imported rows"},
        "features": "fixed family probability only as logit input; original family formulas/features unchanged; no new feature search",
        "target": "authoritative finalized YES payout, integer 0 or 1",
        "windows": {"train": [START, TRAIN_END], "calibration": [TRAIN_END, CAL_END], "validation": [CAL_END, VALID_END]},
        "knowledge_cutoff": FIT_AT, "fit_at": FIT_AT,
        "minimum": {"distinct_events_per_complete_day_per_family": 30, "train_days": 7, "calibration_days": 3,
                    "validation_days": 7, "oos_days": 7, "train_events": 210, "calibration_events": 90,
                    "validation_events": 210, "oos_events": 210},
        "independence": "UTC day blocks, not five family copies; disjoint underlying events; independence assumption must pass external review before promotion",
        "missing": "no imputation; all qualified family decisions in each fixed window require labels by cutoff; missing family data => DATA_QUALIFICATION_FAILED",
        "baseline": "paired same-decision midpoint AND ask; fixed incumbent family probability also compared",
        "optimization": {"model": "clip(logistic(a*logit(p)+b+c),0.001,0.999)",
                         "a": [0.5, 0.75, 1, 1.25, 1.5], "b": [-0.25, 0, 0.25],
                         "objective": "minimum mean Brier on TRAIN only", "ties": "ascending (a,b)"},
        "calibration": {"method": "fixed intercept grid on separate CALIBRATION only", "c": [-0.2, -0.1, 0, 0.1, 0.2],
                        "objective": "minimum mean Brier", "ties": "ascending c", "report": "10 equal-width ECE bins; per-row residual is not calibration quality"},
        "selection": "one challenger per family; validation and OOS Brier/logloss strictly beat midpoint and ask; Brier beats fixed family; paired day sign-flip one-sided p<=0.01 for each baseline and family; report all five families",
        "oos": "7 full UTC days beginning next midnight strictly after actual immutable challenger lock; predictions must be appended before close; evaluate once at end+1h; no reconstructed post-outcome forecasts",
        "reward": {"unit_dollars": 1, "hypothetical_starting_equity_dollars": 100,
                   "formula": "mean(net_pnl/U)+mean(market_brier-model_brier)+positive_day_fraction-ECE10-max_drawdown_fraction-sample_variance(net_pnl/U)-mean(slippage/U)-stale_rate-liquidity_failure_rate",
                   "weights": "all 1, frozen", "reward_total": "settled accepted count * normalized score",
                   "slippage": "deduct once in net PnL; additional explicit risk surcharge in reward",
                   "consistency": "positive net-PnL day fraction; descriptive until independent-period review; at least 7 complete days for economic admission",
                   "eligibility": "authoritatively settled native accepted qualified shadow cohort only; any open accepted position blocks aggregate; rejected or synthetic rows earn no reward; n<2 leaves variance/reward unavailable",
                   "control_violation": "permanent disqualification, never compensated by profits"},
        "economic_gate": "positive net PnL AND reward after evidenced costs; qualified fees, slippage, reconciliation, approval, allocation and risk controls; no self-attestation",
        "rejection": ["insufficient samples or periods", "unqualified or missing evidence", "overlap or late predictions",
                      "baseline or reference not beaten", "nonpositive economics", "failed independent review", "any control bypass"],
        "stopping": {"registered_batches": 1, "maximum_challengers": 5, "deadline": DEADLINE,
                     "sufficient_evaluated_losers": "NO_LEARNABLE_EDGE_DEMONSTRATED",
                     "insufficient_or_unqualified": "DATA_QUALIFICATION_FAILED", "retraining_after_terminal": False},
        "champion": "retain existing active research champion; if none, report null; fixed family references are not promoted champions",
        "promotion": "manual external independent review after validation, future OOS, baseline and positive economics; no auto-promotion or self-approval",
        "financial_state": {"capital": "OFF", "broker_writes": 0, "real_orders_submitted": 0}}


def protocol_hash():
    return digest(protocol())


def predict(parameters, probability):
    p = float(probability)
    if not math.isfinite(p) or not 0 < p < 1:
        raise Refused("finite interior probability required")
    a, b, c = (float(parameters[k]) for k in ("a", "b", "c"))
    cfg = protocol()
    if a not in cfg["optimization"]["a"] or b not in cfg["optimization"]["b"] or c not in cfg["calibration"]["c"]:
        raise Refused("unregistered parameters")
    return min(0.999, max(0.001, 1 / (1 + math.exp(-(a * math.log(p / (1-p)) + b + c)))))


def scores(rows, probability_key="probability"):
    if not rows:
        return {"n": 0, "brier": None, "logloss": None, "ece10": None, "bins": []}
    ps, ys = [float(r[probability_key]) for r in rows], [r["outcome"] for r in rows]
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in ps) or any(type(y) is not int or y not in (0, 1) for y in ys):
        raise Refused("invalid prediction/label")
    bins, ece = [], 0
    for i in range(10):
        values = [(p,y) for p,y in zip(ps,ys) if min(9, int(10*p)) == i]
        if values:
            mean = sum(p for p,y in values)/len(values)
            rate = sum(y for p,y in values)/len(values)
            ece += len(values)/len(rows)*abs(mean-rate)
            bins.append({"bin": i, "count": len(values), "mean_probability": mean, "observed_rate": rate})
    return {"n": len(rows), "brier": sum((p-y)**2 for p,y in zip(ps,ys))/len(rows),
            "logloss": -sum(math.log(max(1e-12, min(1-1e-12, p if y else 1-p))) for p,y in zip(ps,ys))/len(rows),
            "ece10": ece, "bins": bins}


def _validate_rows(rows, start, end, cutoff):
    selected = [r for r in rows if utc(start) <= utc(r["observed_at"]) < utc(end)]
    seen = set()
    for r in selected:
        if (r["protocol_hash"] != protocol_hash() or r.get("source_kind") != "NATIVE"
                or not r.get("provenance_verified") or r["event_id"] in seen
                or not utc(r["observed_at"]) <= utc(r["decision_at"]) < utc(r["close_at"])
                or not utc(r["close_at"]) <= utc(r["label_available_at"]) <= utc(cutoff)):
            raise Refused("unqualified, duplicate or unavailable training row")
        seen.add(r["event_id"])
    days = (utc(end)-utc(start)).days
    counts = Counter(utc(r["observed_at"]).date().isoformat() for r in selected)
    if any(counts[(utc(start)+timedelta(days=i)).date().isoformat()] < 30 for i in range(days)):
        raise Refused("minimum 30 distinct events each complete UTC day")
    scores(selected)
    return selected


def paired_gate(rows, parameters):
    evaluated = [dict(r, challenger_probability=predict(parameters, r["probability"])) for r in rows]
    candidate = scores(evaluated, "challenger_probability")
    baselines = {k: scores(rows,k) for k in ("market_probability", "ask_baseline", "probability")}
    comparisons = {}
    for key, baseline in baselines.items():
        blocks = {}
        for r in evaluated:
            day = utc(r["observed_at"]).date().isoformat()
            blocks.setdefault(day, []).append((float(r[key])-r["outcome"])**2-(r["challenger_probability"]-r["outcome"])**2)
        differences = [sum(v)/len(v) for k,v in sorted(blocks.items())]
        observed = sum(differences)
        pvalue = sum(sum(s*d for s,d in zip(signs,differences)) >= observed-1e-14
                     for signs in product((-1,1), repeat=len(differences))) / (2**len(differences))
        comparisons[key] = {"brier_advantage": baseline["brier"]-candidate["brier"], "day_sign_flip_p": pvalue,
                            "days": len(differences), "all_days_positive": all(x>0 for x in differences)}
    passed = all(v["brier_advantage"] > 0 and v["day_sign_flip_p"] <= 0.01 for v in comparisons.values())
    passed = passed and all(candidate["logloss"] < baselines[k]["logloss"] for k in ("market_probability", "ask_baseline"))
    return {"predictive_pass": passed, "candidate": candidate, "baselines": baselines, "comparisons": comparisons,
            "independence_established": False, "qualification": "DIAGNOSTIC_UNADMITTED", "promotion": False}


def train_family(family, rows, at):
    if family not in HYPOTHESES or utc(at) < utc(FIT_AT) or utc(at) >= utc(DEADLINE):
        raise Refused("unregistered family or training time")
    if any(r["family"] != family for r in rows):
        raise Refused("mixed families")
    train = _validate_rows(rows, START, TRAIN_END, FIT_AT)
    calibration = _validate_rows(rows, TRAIN_END, CAL_END, FIT_AT)
    validation = _validate_rows(rows, CAL_END, VALID_END, FIT_AT)
    ids = [r["event_id"] for r in train+calibration+validation]
    if len(set(ids)) != len(ids):
        raise Refused("event overlap across splits")
    cfg = protocol()
    loss = lambda rs, p: sum((predict(p,r["probability"])-r["outcome"])**2 for r in rs)/len(rs)
    a,b = min(product(cfg["optimization"]["a"], cfg["optimization"]["b"]), key=lambda v:(loss(train,dict(a=v[0],b=v[1],c=0)),v))
    c = min(cfg["calibration"]["c"], key=lambda c:(loss(calibration,dict(a=a,b=b,c=c)),c))
    parameters = dict(a=a,b=b,c=c)
    return {"family": family, "parameters": parameters, "protocol_hash": protocol_hash(),
            "dataset_hash": digest(train+calibration+validation), "counts": [len(train),len(calibration),len(validation)],
            "validation": paired_gate(validation,parameters), "qualification": "DIAGNOSTIC_UNADMITTED", "promotion": False}


def oos_window(locked_at):
    start = (utc(locked_at)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    end = start+timedelta(days=7)
    return start.isoformat(),end.isoformat(),(end+timedelta(hours=1)).isoformat()


def evaluate_oos(challenger, rows, at):
    start,end,cutoff = oos_window(challenger["locked_at"])
    if utc(at) < utc(cutoff):
        raise Refused("OOS period incomplete")
    selected = _validate_rows(rows,start,end,cutoff)
    for r in selected:
        if (r.get("challenger_hash") != challenger["candidate_hash"]
                or not utc(challenger["locked_at"]) < utc(start) <= utc(r["prediction_recorded_at"]) < utc(r["close_at"])
                or abs(float(r["prospective_probability"])-predict(challenger["parameters"],r["probability"])) > 1e-12):
            raise Refused("missing immutable prospective OOS prediction")
    return paired_gate(selected,challenger["parameters"])
