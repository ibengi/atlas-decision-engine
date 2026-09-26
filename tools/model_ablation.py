"""
model_ablation.py — which part of the model loses to the market?

WHY THIS EXISTS
    `brier_oos` says the model forecasts worse than the ask. It does not
    say WHICH part of it does, and "the model is wrong, rebuild it" is
    weeks of work aimed at a component nobody has identified.

    It does not have to be guessed. The store records every input the
    model consumed — `spot`, `strike`, `sigma_1m`, `minutes_remaining`,
    `ret_5m` — beside the outcome. So the model can be re-run over the
    exact same settled observations with one component changed at a time,
    and each variant scored against the same market baseline. No new data,
    no waiting for a fresh window.

THE COMPONENT UNDER SUSPICION
    `probability_yes` is a driftless GBM terminal probability,

        d = ln(spot/strike) / (sigma_1m * sqrt(T))
        P = Phi(d + mu),  mu = clip((ret_5m/5) * T / denom, +/- MOMENTUM_CAP)

    The momentum term bets that five minutes of BTC return persists over
    the next T. At these horizons returns are close to a martingale, so
    the bet is doubtful on its face. What makes it worth isolating is its
    size: MOMENTUM_CAP is 0.5 in units of d, and Phi(0.5) - Phi(0) is about
    19 points of probability. The engine's own logs claim net edges of
    +19.2% and +22.3%. An edge that lands on the model's own clipping
    constant is a number the model is reporting about itself, not about
    the market.

    That is a hypothesis, and this tool is how it gets tested rather than
    argued.

THE DISCIPLINE THIS TOOL OWES
    Trying several variants and keeping the best is how overfitting
    happens; the winner of a search is biased upward by the search itself.
    So every verdict here is DIAGNOSTIC_ONLY. A variant that beats the
    baseline on this sample has earned one thing: the right to be fixed in
    advance and re-measured on a window it has never seen. Nothing here
    promotes anything, and a variant winning here is not a result.

    The control variant recomputes the model exactly as configured and
    must reproduce the probability the store recorded. If it does not,
    the deployed model is not the model in this tree, and no variant
    comparison means anything until that is resolved.
"""

import os
import sys

# `python tools/x.py` puts tools/ on sys.path, not the repository root, so
# the package imports below would fail exactly where this tool is meant to
# be used: from a shell, in the container. Prepending the root makes the
# CLI and the test-suite import paths the same one.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import hashlib
import json

from btc_probability_model import MOMENTUM_CAP, P_CEIL, P_FLOOR, norm_cdf
from tools.brier_oos import brier, split_chronological, usable_rows

#: Agreement tolerance between the recomputed control and the stored
#: probability. Wider than float noise, far tighter than any real change.
CONTROL_TOL = 1e-6

REQUIRED = ("spot", "strike", "sigma_1m", "minutes_remaining")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def recompute(r, *, use_momentum=True, cap=MOMENTUM_CAP, sigma_scale=1.0):
    """The model's own formula over one stored row, one knob moved.

    Returns None when an input the model requires is missing, exactly as
    `probability_yes` raises: a variant is never scored on a row it could
    not actually have decided.
    """
    vals = {}
    for f in REQUIRED:
        v = _num(r.get(f) if r.get(f) is not None
                 else (r.get("features") or {}).get(f))
        if v is None or v <= 0:
            return None
        vals[f] = v
    import math
    sigma = vals["sigma_1m"] * sigma_scale
    t = vals["minutes_remaining"]
    denom = sigma * math.sqrt(t)
    if denom <= 0:
        return None
    d = math.log(vals["spot"] / vals["strike"]) / denom
    mu = 0.0
    if use_momentum:
        ret5 = _num(r.get("ret_5m") if r.get("ret_5m") is not None
                    else (r.get("features") or {}).get("ret_5m"))
        if ret5 is not None:
            mu = max(-cap, min(cap, (ret5 / 5.0) * t / denom))
    return min(P_CEIL, max(P_FLOOR, norm_cdf(d + mu)))


#: name -> kwargs. `as_recorded` is the control and must reproduce the
#: store; `no_momentum` is the hypothesis; the rest bracket it so a win is
#: read as a trend rather than one lucky setting.
VARIANTS = (
    ("as_recorded", {}),
    ("no_momentum", {"use_momentum": False}),
    ("momentum_cap_0.10", {"cap": 0.10}),
    ("momentum_cap_0.25", {"cap": 0.25}),
    ("sigma_x1.5", {"sigma_scale": 1.5}),
    ("sigma_x0.67", {"sigma_scale": 0.67}),
)


def _score(rows, **kw):
    """Brier of a variant against the market, on identical rows only."""
    model, market = [], []
    skipped = 0
    for r in rows:
        p = recompute(r, **kw)
        if p is None:
            skipped += 1
            continue
        y = 1 if r["result"] == "yes" else 0
        model.append((p, y))
        market.append((float(r["yes_ask"]) / 100.0, y))
    bm, bb = brier(model), brier(market)
    return {
        "n_scored": len(model), "n_skipped_missing_inputs": skipped,
        "brier_model": round(bm, 6) if bm is not None else None,
        "brier_market_baseline": round(bb, 6) if bb is not None else None,
        "delta_model_minus_baseline": (round(bm - bb, 6)
                                       if bm is not None and bb is not None
                                       else None),
        "beats_market": (bm < bb) if bm is not None and bb is not None
                        else None,
    }


def control_agreement(rows):
    """Does recomputing reproduce what the store recorded?

    A disagreement means the probabilities in the store did not come from
    this tree's model, so no variant comparison below it is meaningful.
    """
    checked = mismatched = 0
    worst = 0.0
    for r in rows:
        p, stored = recompute(r), _num(r.get("probability_yes"))
        if p is None or stored is None:
            continue
        checked += 1
        gap = abs(p - stored)
        worst = max(worst, gap)
        if gap > CONTROL_TOL:
            mismatched += 1
    return {"rows_checked": checked, "rows_mismatched": mismatched,
            "max_abs_gap": round(worst, 9),
            "tolerance": CONTROL_TOL,
            "model_in_tree_reproduces_the_store": (checked > 0
                                                   and mismatched == 0)}


def analyse(records, dataset_sha256):
    rows = usable_rows(records)
    _, _, test = split_chronological(rows)
    control = control_agreement(test)

    results = []
    for name, kw in VARIANTS:
        results.append(dict(variant=name, knobs=kw or {"none": "control"},
                            **_score(test, **kw)))

    winners = [v["variant"] for v in results
               if v["beats_market"] and v["variant"] != "as_recorded"]
    baseline = next(v for v in results if v["variant"] == "as_recorded")

    if not control["model_in_tree_reproduces_the_store"]:
        verdict = "INVALID_MODEL_IN_TREE_DOES_NOT_REPRODUCE_THE_STORE"
    elif baseline["beats_market"]:
        verdict = "DIAGNOSTIC_ONLY_BASELINE_ALREADY_BEATS_MARKET"
    elif winners:
        verdict = "DIAGNOSTIC_ONLY_A_VARIANT_BEATS_MARKET_IN_SAMPLE"
    else:
        verdict = "DIAGNOSTIC_ONLY_NO_VARIANT_BEATS_MARKET"

    return {
        "dataset_sha256": dataset_sha256,
        "decisive_slice": "test",
        "n_test_rows": len(test),
        "control_recompute": control,
        "variants": results,
        "variants_beating_market_in_sample": winners,
        # Said in the report, not only in the docstring: the search that
        # produced a winner also biased it.
        "warning": ("variant selection on this sample is in-sample. A "
                    "winner must be fixed in advance and re-measured on a "
                    "window it has never seen before it means anything."),
        "verdict": verdict,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Which model component loses to the market?")
    ap.add_argument("path", help="shadow_predictions.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    raw = open(args.path, "rb").read()
    report = analyse(json.loads(raw.decode("utf-8")),
                     hashlib.sha256(raw).hexdigest())
    text = json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    # Diagnostic: exit 0 whenever it could run and the control held.
    return 0 if report["control_recompute"][
        "model_in_tree_reproduces_the_store"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
