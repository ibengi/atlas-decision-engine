"""
shadow_census.py — where do the shadow predictions go?

WHY THIS EXISTS
    `model_validation.json` records `shadow_predictions_settled: 108` while
    the engine logs report a running total in the thousands
    (`[SHADOW] ... total regle: 13444`). Both numbers can be true at once:
    `brier_oos.usable_rows` keeps only rows that are settled AND carry both
    a model probability and a market ask, and it is right to do so — a row
    missing either side cannot score the model and the baseline on the same
    observation. But `brier_oos` reports only the survivors. It never says
    how many rows were dropped, or why.

    That gap matters more than the Brier number itself. If the sample
    collapses because the market quote was not journalled, the fix is
    instrumentation and the claim "the model has an out-of-sample edge" is
    not testable yet. If it collapses because almost every settled row
    belongs to a series under quarantine, then the measurable sample is not
    the sample any live scope would trade. A verdict that does not
    distinguish those two cases is not a verdict.

WHAT IT DOES
    Reads the same `shadow_predictions.json` and reports, without deciding
    anything:
      * the attrition ladder from every record down to a usable row, with
        one counted reason per dropped row (first reason wins, in the same
        order `brier_oos` applies its filters);
      * the same ladder per market series, so a sample that survives only
        inside one series is visible as such;
      * settlement-date concentration, because rows sharing a settlement
        date resolve from one underlying path and are not independent;
      * per series, the realised base rate against the mean market-implied
        probability. A wide gap on a large sample is a label-integrity
        smell, not a proof: `daily_label_audit.py` is the tool that decides
        a label, and only for KXBTCD;
      * the model versions present, so a sample can be bound to a lineage;
      * the spread of `data_quality`, the only recorded witness to how good
        the model's inputs were. The engine's primary klines provider is
        Binance, which answers 451 to the US region this service runs in,
        so every prediction in the store was made on a fallback venue or on
        the bounded stale cache. Which one is not recorded per row, but
        `data_quality` moves with staleness, so scoring the strata
        separately says whether a failure is the model's or its inputs'.

WHAT IT DOES NOT DO
    It emits no verdict, no threshold and no PASS. It never writes to the
    store. Nothing here promotes anything: `brier_oos.py` remains the tool
    that answers the guide's rule, and this one only says what its sample
    was made of.
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
from collections import Counter, defaultdict

#: Applied in this order; the first one that matches is the reason the row
#: is counted under. The order mirrors `brier_oos.usable_rows` exactly, so
#: the survivor count here equals `n_usable` there on the same bytes.
DROP_REASONS = (
    "not_a_dict",
    "unsettled",
    "no_model_probability",
    "no_market_ask",
    "unparseable_number",
    "probability_out_of_range",
    "ask_out_of_range",
)


def drop_reason(r):
    """None when the row is usable, else the first reason it is not."""
    if not isinstance(r, dict):
        return "not_a_dict"
    if r.get("result") not in ("yes", "no"):
        return "unsettled"
    p, ask = r.get("probability_yes"), r.get("yes_ask")
    if p is None:
        return "no_model_probability"
    if ask is None:
        return "no_market_ask"
    try:
        p, ask = float(p), float(ask)
    except (TypeError, ValueError):
        return "unparseable_number"
    if not 0.0 <= p <= 1.0:
        return "probability_out_of_range"
    if not 0.0 <= ask <= 100.0:
        return "ask_out_of_range"
    return None


def series_of(r):
    """The market series a ticker belongs to.

    Kalshi tickers are SERIES-EVENT-OUTCOME. The series is what a live
    scope or a quarantine is expressed in, so it is the unit that decides
    whether a measurable sample is a tradable one.
    """
    if not isinstance(r, dict):
        return "<not_a_dict>"
    t = r.get("ticker")
    if not isinstance(t, str) or not t.strip():
        return "<no_ticker>"
    return t.strip().split("-")[0]


#: `confidence_from_quality`: below 60 the router refuses the market
#: outright, so rows at or above it are the ones that reached a decision.
QUALITY_BANDS = ((0, 60, "refused_below_router_floor"), (60, 75, "60-75"),
                 (75, 90, "75-90"), (90, 101, "90-100"))


def quality_band(r):
    """The data_quality band a row's inputs fell in, or None if unrecorded."""
    v = (r.get("features") or {}).get("data_quality")
    try:
        q = float(v)
    except (TypeError, ValueError):
        return None
    for lo, hi, name in QUALITY_BANDS:
        if lo <= q < hi:
            return name
    return "out_of_range"


def _mean(xs):
    return round(sum(xs) / len(xs), 6) if xs else None


def census(records, dataset_sha256: str) -> dict:
    records = records or []
    reasons = Counter()
    per_series = defaultdict(lambda: {"records": 0, "settled": 0,
                                      "usable": 0, "drops": Counter()})
    usable = []

    for r in records:
        s = series_of(r)
        bucket = per_series[s]
        bucket["records"] += 1
        if isinstance(r, dict) and r.get("result") in ("yes", "no"):
            bucket["settled"] += 1
        why = drop_reason(r)
        if why is None:
            bucket["usable"] += 1
            usable.append(r)
        else:
            reasons[why] += 1
            bucket["drops"][why] += 1

    dates = Counter(str(r.get("settled_at"))[:10] for r in usable
                    if r.get("settled_at"))
    dates.pop("None", None)

    series_report = []
    for s in sorted(per_series, key=lambda k: -per_series[k]["usable"]):
        b = per_series[s]
        rows = [r for r in usable if series_of(r) == s]
        outcomes = [1 if r["result"] == "yes" else 0 for r in rows]
        implied = [float(r["yes_ask"]) / 100.0 for r in rows]
        base, mkt = _mean(outcomes), _mean(implied)
        series_report.append({
            "series": s,
            "records": b["records"],
            "settled": b["settled"],
            "usable": b["usable"],
            "drops": dict(sorted(b["drops"].items())),
            "realised_yes_rate": base,
            "mean_market_implied": mkt,
            # Context only. A gap is a reason to audit the labels of that
            # series, never a conclusion about them on its own.
            "base_rate_minus_market_implied": (round(base - mkt, 6)
                                               if base is not None
                                               and mkt is not None else None),
        })

    # Per quality band: realised outcome against what the market implied.
    # A band whose realised rate tracks the market is one the model had
    # sound inputs for; a band that diverges is one it did not.
    bands, unrecorded = {}, 0
    for r in usable:
        b = quality_band(r)
        if b is None:
            unrecorded += 1
            continue
        bands.setdefault(b, []).append(r)
    quality_report = []
    for name in [b[2] for b in QUALITY_BANDS] + ["out_of_range"]:
        rows = bands.get(name)
        if not rows:
            continue
        base = _mean([1 if r["result"] == "yes" else 0 for r in rows])
        mkt = _mean([float(r["yes_ask"]) / 100.0 for r in rows])
        quality_report.append({
            "band": name, "n": len(rows),
            "realised_yes_rate": base, "mean_market_implied": mkt,
            "base_rate_minus_market_implied": (round(base - mkt, 6)
                                               if base is not None
                                               and mkt is not None else None),
        })

    ts = sorted(str(r.get("ts")) for r in usable if r.get("ts"))
    return {
        "dataset_sha256": dataset_sha256,
        "n_records_total": len(records),
        "n_settled": sum(1 for r in records if isinstance(r, dict)
                         and r.get("result") in ("yes", "no")),
        "n_usable": len(usable),
        # Reconciles the engine's running settled total with the sample
        # `brier_oos` actually scores. This is the number the validation
        # artifact reports, and the difference is the finding.
        "attrition": {
            "usable": len(usable),
            "dropped": len(records) - len(usable),
            "by_reason": {k: reasons[k] for k in DROP_REASONS if reasons[k]},
        },
        "by_series": series_report,
        "usable_time_span": {"first_ts": ts[0] if ts else None,
                             "last_ts": ts[-1] if ts else None},
        "settlement_dates": {
            "n_distinct": len(dates),
            "max_rows_on_one_date": max(dates.values()) if dates else 0,
            "top": dict(dates.most_common(10)),
        },
        "by_data_quality": quality_report,
        "n_without_recorded_data_quality": unrecorded,
        "model_versions": dict(Counter(
            str((r.get("features") or {}).get("model_version"))
            for r in usable).most_common()),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Attrition and coverage census of the shadow store")
    ap.add_argument("path", help="shadow_predictions.json")
    ap.add_argument("--out", default=None, help="write the report here too")
    args = ap.parse_args(argv)

    raw = open(args.path, "rb").read()
    report = census(json.loads(raw.decode("utf-8")),
                    hashlib.sha256(raw).hexdigest())
    text = json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    # A census has no verdict to fail on. Exit 0 whenever it could read.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
