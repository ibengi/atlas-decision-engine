"""Atlas Alpha Learning v1.

Read-only learning layer over the append-only AlphaLedger.  It never submits,
sizes or authorizes a trade.  Its job is to turn resolved shadow predictions
into auditable model scorecards, cost-aware incremental value, and a compact
memory of similar historical cases that can be fed into the next research
prompt.

The learning rule is deliberately statistical, not win/loss imitation:
Brier score, log loss, calibration gap and hypothetical PnL are derived from
predictions that were recorded BEFORE resolution.
"""

import math
from collections import defaultdict


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _clip(p, eps=1e-9):
    return min(1.0 - eps, max(eps, float(p)))


def _model_key_matches(key: str, selector: str) -> bool:
    return str(selector).lower() in str(key).lower()


def score_model(rows, selector: str, *, market_class=None) -> dict:
    """Score one provider/model over resolved immutable prediction rows."""
    samples = []
    for row in rows:
        if row.get("invalidated"):
            continue
        if market_class and row.get("market_class") != market_class:
            continue
        outcome = row.get("actual_outcome")
        if outcome not in (0, 1):
            continue
        for key, signal in (row.get("per_model") or {}).items():
            if not _model_key_matches(key, selector):
                continue
            p = (signal or {}).get("p_yes")
            if _finite(p):
                samples.append((float(p), int(outcome), row, key))

    if not samples:
        return {"selector": selector, "market_class": market_class,
                "samples": 0, "brier": None, "log_loss": None,
                "mean_prediction": None, "empirical_yes_rate": None,
                "calibration_gap": None, "accuracy_at_50": None,
                "models_seen": []}

    brier = sum((p - y) ** 2 for p, y, _, _ in samples) / len(samples)
    log_loss = -sum(y * math.log(_clip(p)) + (1-y) * math.log(1-_clip(p))
                    for p, y, _, _ in samples) / len(samples)
    mean_p = sum(p for p, _, _, _ in samples) / len(samples)
    yes_rate = sum(y for _, y, _, _ in samples) / len(samples)
    accuracy = sum((p >= .5) == bool(y) for p, y, _, _ in samples) / len(samples)
    return {"selector": selector, "market_class": market_class,
            "samples": len(samples), "brier": round(brier, 8),
            "log_loss": round(log_loss, 8),
            "mean_prediction": round(mean_p, 8),
            "empirical_yes_rate": round(yes_rate, 8),
            "calibration_gap": round(mean_p - yes_rate, 8),
            "accuracy_at_50": round(accuracy, 8),
            "models_seen": sorted({key for _, _, _, key in samples})}


def score_by_category(rows, selector: str) -> dict:
    cats = sorted({r.get("market_class") for r in rows if r.get("market_class")})
    return {c: score_model(rows, selector, market_class=c) for c in cats}


def _snapshot_price(row):
    snap = row.get("snapshot") or {}
    yes_ask = snap.get("yes_ask")
    no_ask = snap.get("no_ask")
    return yes_ask, no_ask


def hypothetical_model_pnl(rows, selector: str, *, notional_usd=1.0,
                           min_edge=0.0, fee_rate=0.0, slippage_rate=0.0) -> dict:
    """Simple counterfactual: trade only when model probability beats ask.

    This is deliberately conservative and provider-comparable.  It is not an
    execution recommendation and does not use future information.
    """
    pnl = 0.0
    trades = 0
    for row in rows:
        if row.get("invalidated") or row.get("actual_outcome") not in (0, 1):
            continue
        signal = None
        for key, value in (row.get("per_model") or {}).items():
            if _model_key_matches(key, selector):
                signal = value or {}
                break
        if not signal or not _finite(signal.get("p_yes")):
            continue
        p = float(signal["p_yes"])
        yes_ask, no_ask = _snapshot_price(row)
        if not (_finite(yes_ask) and _finite(no_ask)):
            continue
        y = int(row["actual_outcome"])
        yes_edge = p - float(yes_ask)
        no_edge = (1.0 - p) - float(no_ask)
        side = None
        price = None
        if yes_edge >= no_edge and yes_edge > min_edge:
            side, price = "yes", float(yes_ask)
        elif no_edge > min_edge:
            side, price = "no", float(no_ask)
        if side is None:
            continue
        cost = notional_usd * (fee_rate + slippage_rate)
        # $1 notional: win pays (1-price), loss loses price.
        win = (side == "yes" and y == 1) or (side == "no" and y == 0)
        pnl += notional_usd * ((1.0 - price) if win else -price) - cost
        trades += 1
    return {"selector": selector, "trades": trades,
            "hypothetical_net_pnl_usd": round(pnl, 8)}


def incremental_value(rows, *, target_selector: str, baseline_selector: str,
                      subscription_cost_usd=0.0, **pnl_kwargs) -> dict:
    target = hypothetical_model_pnl(rows, target_selector, **pnl_kwargs)
    baseline = hypothetical_model_pnl(rows, baseline_selector, **pnl_kwargs)
    inc = target["hypothetical_net_pnl_usd"] - baseline["hypothetical_net_pnl_usd"]
    net = inc - float(subscription_cost_usd)
    return {"target": target_selector, "baseline": baseline_selector,
            "target_pnl_usd": target["hypothetical_net_pnl_usd"],
            "baseline_pnl_usd": baseline["hypothetical_net_pnl_usd"],
            "incremental_alpha_usd": round(inc, 8),
            "subscription_cost_usd": round(float(subscription_cost_usd), 8),
            "net_value_after_subscription_usd": round(net, 8),
            "worth_subscription": bool(net > 0)}


def classify_error(p_yes: float, outcome: int) -> str:
    """Small, deterministic error taxonomy used for memory retrieval."""
    p = float(p_yes)
    y = int(outcome)
    confidence = abs(p - .5)
    correct = (p >= .5) == bool(y)
    if correct and confidence >= .25:
        return "strong_correct"
    if correct:
        return "weak_correct"
    if confidence >= .35:
        return "overconfident_wrong"
    if confidence >= .15:
        return "confident_wrong"
    return "near_coinflip_wrong"


def build_memory(rows, selector: str) -> list:
    """Compact historical cases safe to inject into a future research prompt."""
    out = []
    for row in rows:
        if row.get("actual_outcome") not in (0, 1):
            continue
        for key, signal in (row.get("per_model") or {}).items():
            if not _model_key_matches(key, selector):
                continue
            p = (signal or {}).get("p_yes")
            if not _finite(p):
                continue
            snap = row.get("snapshot") or {}
            out.append({
                "prediction_id": row.get("prediction_id"),
                "contract_id": row.get("contract_id") or snap.get("contract_id"),
                "market_class": row.get("market_class") or snap.get("market_class"),
                "p_yes": round(float(p), 6),
                "outcome": int(row["actual_outcome"]),
                "error_class": classify_error(float(p), int(row["actual_outcome"])),
                "invalidated": bool(row.get("invalidated")),
                "model": key,
            })
    return out


def similar_cases(memory: list, *, market_class=None, limit=5) -> list:
    """Return recent relevant cases without pretending to use embeddings.

    Category match is preferred; within a category, hard mistakes are shown
    first so the next prompt sees failure modes before easy victories.
    """
    rank = {"overconfident_wrong": 0, "confident_wrong": 1,
            "near_coinflip_wrong": 2, "strong_correct": 3,
            "weak_correct": 4}
    rows = [m for m in memory if not m.get("invalidated")]
    if market_class:
        matched = [m for m in rows if m.get("market_class") == market_class]
        if matched:
            rows = matched
    rows = sorted(rows, key=lambda m: rank.get(m.get("error_class"), 9))
    return rows[:max(0, int(limit))]


def learning_report(ledger, *, astra_selector="astra", baseline_selector="atlasquant",
                    subscription_cost_usd=100.0) -> dict:
    # RA-13: learning reads QUALIFIED settlements only. `resolved()` is the
    # audit history and includes rows nobody can tie to a market -- a
    # resolution appended with no binding, no trusted authority and no
    # evidence verification was previously scored, weighted and reported as
    # the model's calibration. The excluded count is reported so the
    # exclusion cannot be silent.
    rows = ledger.qualified_resolved()
    excluded = len(ledger.unqualified_resolved())
    return {
        "mode": "SHADOW_ONLY",
        "broker_authority": False,
        "settlements_excluded_unqualified": excluded,
        "astra": score_model(rows, astra_selector),
        "astra_by_category": score_by_category(rows, astra_selector),
        "memory": build_memory(rows, astra_selector),
        "astra_vs_baseline": incremental_value(
            rows, target_selector=astra_selector,
            baseline_selector=baseline_selector,
            subscription_cost_usd=subscription_cost_usd),
    }
