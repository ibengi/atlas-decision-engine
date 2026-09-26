"""
shadow_pnl.py — what would the engine's own decisions have earned?

WHY THIS EXISTS
    `brier_oos.py` answers the guide's rule: does the model forecast better
    than the market, across every qualified observation? On the current
    store the answer is no.

    That is not the same question as profitability, and conflating the two
    would be a mistake in either direction. Brier scores every row. A
    trading engine scores only the rows it chose to trade. A model can
    forecast worse than the market on average and still hold edge on a
    narrow selected tail; it can also forecast well and lose money once the
    spread and fees are charged. The engine already records which rows it
    would have traded, on which side, at which ask, with which fee and
    slippage. So the P&L those decisions would have produced is not an
    executable fill proof — it is a hypothetical recorded-ask replay.

WHAT IT DOES
    For every settled row whose `shadow_decision` is a side, fills at the
    RECORDED ASK for that side, never at mid: the engine pays the spread,
    as it would live. Settlement pays 1.0 for the winning side and 0.0 for
    the losing one, so per contract, in dollars:

        net = (1 - entry) - fee - slip   if the side won
        net = (  - entry) - fee - slip   otherwise

    which is the realised form of the engine's own
    `net_ev = p*(1-mkt_p) - (1-p)*mkt_p - fee - slip`.

    Rows are split chronologically 60/20/20 as in `brier_oos`, and only the
    TEST slice decides. Two controls run beside it: the P&L of trading
    EVERY row at the ask, which should be about minus the spread and fees
    and shows what selection is worth, and the per-settlement-date
    breakdown, because a total carried by one or two days is not a result.

THE TRAP THIS TOOL EXISTS TO NAME
    A positive test-slice P&L under a failing out-of-sample Brier is the
    classic signature of selection without forecasting skill: the model
    does not predict better than the market, yet the rows it picked
    happened to win. On a sample this size that is far more likely to be
    the selection fitting noise than an edge. The tool reports that
    combination as SELECTION_WITHOUT_FORECAST_EDGE and refuses to call it
    profitable, because a number that only looks good when the model is
    known to be wrong is the one number you must not act on.

WHAT IT DOES NOT DO
    It approves nothing and authorises nothing. A favourable replay is a
    reason to keep measuring, never a reason to send capital: no replay
    charges queue position, partial fills, adverse selection against a
    resting order, or the market moving between decision and fill.
"""

import argparse
import hashlib
import json
import math
from collections import defaultdict

#: Shared with `brier_oos.DEFAULT_SPLIT`; only the last slice decides.
DEFAULT_SPLIT = (0.6, 0.2, 0.2)

#: Below this many traded TEST rows the replay states no outcome. A handful
#: of contracts cannot separate an edge from a run of luck, and inventing a
#: threshold that the sample happens to clear is how a tool flatters itself.
MIN_TRADED_TEST = 100


def _ts(row):
    from tools.brier_oos import _ts as brier_ts
    return brier_ts(row)


def traded_rows(records):
    """Settled rows the engine said it would have traded, priced.

    A row is kept only when it names a side, that side carries an ask, and
    the market settled. Anything else cannot be replayed, and guessing a
    missing ask would invent the very number the replay is measuring.
    """
    kept = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        side = r.get("shadow_decision")
        if side not in ("yes", "no"):
            continue
        if r.get("result") not in ("yes", "no"):
            continue
        ask = r.get("yes_ask") if side == "yes" else r.get("no_ask")
        if ask is None:
            continue
        try:
            ask = float(ask)
        except (TypeError, ValueError):
            continue
        if not 0.0 < ask <= 100.0:
            continue
        kept.append(r)
    return kept


def _costs(r):
    """Missing, malformed or negative costs remain unavailable, never zero."""
    out=[]
    for field in ("estimated_fee","estimated_slippage"):
        value=r.get(field)
        if value is None or isinstance(value,bool): return None
        try: value=float(value)
        except (TypeError,ValueError): return None
        if not math.isfinite(value) or value<0: return None
        out.append(value)
    return tuple(out)


def replay(r, side=None):
    """Hypothetical dollars at recorded ask; None when either cost is unknown."""
    side = side or r["shadow_decision"]
    ask = float(r["yes_ask"] if side == "yes" else r["no_ask"]) / 100.0
    costs = _costs(r)
    if costs is None: return None
    fee, slip = costs
    won = (r["result"] == side)
    return (1.0 - ask if won else -ask) - fee - slip


def _summarise(rows, label, side_of=None):
    if not rows:
        return {"label": label, "n": 0, "total_net": None,
                "mean_net_per_contract": None, "win_rate": None}
    pnl = [replay(r, side_of(r) if side_of else None) for r in rows]
    if any(value is None for value in pnl):
        return {"label":label,"n":len(rows),"total_net":None,"mean_net_per_contract":None,"win_rate":None,"cost_coverage":sum(value is not None for value in pnl)/len(rows),"reason":"MISSING_OR_INVALID_COSTS"}
    wins = sum(1 for r in rows
               if r["result"] == (side_of(r) if side_of
                                  else r["shadow_decision"]))
    return {
        "label": label,
        "n": len(rows),
        "total_net": round(sum(pnl), 6),
        "mean_net_per_contract": round(sum(pnl) / len(pnl), 6),
        "win_rate": round(wins / len(rows), 6),
    }


def _by_date(rows):
    """Per settlement date, so a total carried by one day is visible."""
    acc = defaultdict(float)
    for r in rows:
        d = str(r.get("settled_at"))[:10]
        if len(d) == 10:
            value=replay(r)
            if value is None: acc[d]=None
            elif acc[d] is not None: acc[d]+=value
    return dict(sorted(acc.items()))


def analyse(records, dataset_sha256, brier_delta=None, split=DEFAULT_SPLIT,
            min_traded=MIN_TRADED_TEST):
    from tools.brier_oos import split_chronological, usable_rows

    all_rows = usable_rows(records)
    all_tr, all_va, all_te = split_chronological(all_rows, split)
    tr, va, te = (traded_rows(rows) for rows in (all_tr,all_va,all_te))
    traded = tr+va+te
    test = _summarise(te, "test")

    # Control: trade EVERY scorable row on the yes side at the yes ask.
    # Its P&L is roughly minus the spread and fees, so the gap between it
    # and the selected result is what the engine's selection is worth.
    # Same full-row boundaries as forecasts; no independent trade-row split.
    control = _summarise(all_te, "test_trade_everything_yes",
                         side_of=lambda r: "yes")

    dates = _by_date(te)
    best = max(dates.items(), key=lambda kv: kv[1]) if dates and all(v is not None for v in dates.values()) else None

    profitable = (test["total_net"] is not None and test["total_net"] > 0)
    enough = test["n"] >= min_traded

    if test["total_net"] is None and test["n"]:
        verdict = "PROFITABILITY_UNAVAILABLE_MISSING_COSTS"
    elif not enough:
        verdict = "INDETERMINATE_SAMPLE_TOO_SMALL"
    elif not profitable:
        verdict = "UNPROFITABLE"
    elif brier_delta is not None and brier_delta >= 0:
        # Profit while the model forecasts WORSE than the market. See the
        # module docstring: this is not a pass.
        verdict = "SELECTION_WITHOUT_FORECAST_EDGE"
    else:
        verdict = "POSITIVE_ESTIMATED_PNL_NOT_QUALIFIED"

    return {
        "dataset_sha256": dataset_sha256,
        "profitability_qualified": False,
        "prospective_evidence": False,
        "cost_authority": "LEGACY_ESTIMATES_NOT_AUTHENTICATED_RECEIPTS",
        "n_records_total": len(records or []),
        "n_traded_decisions": len(traded),
        "split": {"chronological": True, "ratios": list(split),
                  "train": len(tr), "validation": len(va), "test": len(te)},
        "decisive_slice": "test",
        "fill_assumption": "crosses the spread: filled at the recorded ask",
        "test": test,
        "control": control,
        "selection_value_per_contract": (
            round(test["mean_net_per_contract"]
                  - control["mean_net_per_contract"], 6)
            if test["mean_net_per_contract"] is not None
            and control["mean_net_per_contract"] is not None else None),
        "test_pnl_by_settlement_date": dates,
        "n_settlement_dates_in_test": len(dates),
        "largest_single_date": ({"date": best[0], "net": round(best[1], 6),
                                 "share_of_total": (
                                     round(best[1] / test["total_net"], 4)
                                     if test["total_net"] else None)}
                                if best else None),
        "brier_delta_model_minus_baseline": brier_delta,
        "context_only_not_decisive": {"train": _summarise(tr, "train"),
                                      "validation": _summarise(va,
                                                               "validation")},
        "verdict": verdict,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Replay the engine's own shadow decisions as P&L")
    ap.add_argument("path", help="shadow_predictions.json")
    ap.add_argument("--brier-delta", type=float, default=None,
                    help="brier_model - brier_market from brier_oos, so the "
                         "replay can name a profit that contradicts it")
    ap.add_argument("--min-traded", type=int, default=MIN_TRADED_TEST)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    raw = open(args.path, "rb").read()
    report = analyse(json.loads(raw.decode("utf-8")),
                     hashlib.sha256(raw).hexdigest(),
                     brier_delta=args.brier_delta,
                     min_traded=args.min_traded)
    text = json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1  # legacy estimated replay cannot qualify profitability


if __name__ == "__main__":
    raise SystemExit(main())

