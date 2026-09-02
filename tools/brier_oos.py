"""
brier_oos.py — reproducible out-of-sample Brier check for the BTC 15m model.

WHY THIS EXISTS
    `MODEL_VALIDATION_GUIDE.md` states the decisive rule for promotion:

        "Brier du modele DOIT battre le baseline marche (yes_ask/100) hors
         echantillon ; sinon le marche predit mieux que le modele et il n'y
         a aucun edge a exploiter."

    The snippet the guide gives to compute it is NOT executable: it calls
    `ShadowPredictionStore().as_calibration_obs()` and `mc.save(...)`, and
    neither exists in this repository. So the rule was stated but could not
    be checked. This module makes it checkable, deterministically, from the
    shadow prediction store on disk.

WHAT IT DOES
    * Reads a `shadow_predictions.json` written by `ShadowPredictionStore`.
    * Keeps only rows that are SETTLED (`result` in yes/no) and that carry
      BOTH a model probability and a market ask, so the model and the
      baseline are scored on the EXACT same rows. A row missing either side
      is dropped from both, never defaulted.
    * Sorts by decision time `ts` and splits chronologically 60/20/20, the
      same split `backtest_btc15m` uses. Only the TEST slice decides.
    * Scores the model on `probability_yes` AS RECORDED AT DECISION TIME.
      Nothing is recomputed from the outcome, and no future row informs an
      earlier one, so there is no leakage by construction.
    * Scores the market baseline on `yes_ask / 100`, per the guide.
    * Emits a JSON report carrying the dataset SHA-256, so a reader can
      confirm which bytes produced which numbers.

VERDICT DISCIPLINE
    PASS is returned only when every gate is defined AND satisfied. A gate
    whose threshold is not defined in the repository (see
    `T7I_BTC_DAILY_EVIDENCE_CALIBRATION_FOUNDATION.md` sect. 9: the minimum
    number of INDEPENDENT settlement dates "REQUIRES OPERATOR APPROVAL")
    makes the verdict INDETERMINATE, never PASS. This module does not invent
    a threshold to clear itself, and computing a favourable number on the
    full sample is not a substitute for the out-of-sample one.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone

#: `MODEL_VALIDATION_GUIDE.md`, "Donnees historiques qui MANQUENT encore".
GATE_MIN_SETTLED = 300

#: Chronological split shared with `backtest_btc15m.DEFAULT_SPLIT`.
DEFAULT_SPLIT = (0.6, 0.2, 0.2)


def _ts(row) -> float:
    v = row["ts"]
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def usable_rows(records) -> list:
    """Settled rows carrying both sides of the comparison.

    A row is kept only when it has a settled outcome, a model probability,
    and a market ask. Dropping it from both series is the only honest
    handling: substituting a default would score one side on data the other
    never saw.
    """
    kept = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        if r.get("result") not in ("yes", "no"):
            continue
        p, ask = r.get("probability_yes"), r.get("yes_ask")
        if p is None or ask is None:
            continue
        try:
            p, ask = float(p), float(ask)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= p <= 1.0) or not (0.0 <= ask <= 100.0):
            continue
        kept.append(r)
    return kept


def split_chronological(rows, split=DEFAULT_SPLIT):
    ordered = sorted(rows, key=_ts)
    n = len(ordered)
    a = int(n * split[0])
    b = a + int(n * split[1])
    return ordered[:a], ordered[a:b], ordered[b:]


def brier(pairs) -> float:
    """Mean squared error of probabilistic forecasts. None on an empty set —
    an empty sample has no score, and 0.0 would read as a perfect one."""
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _pairs(rows, source: str):
    if source == "model":
        return [(float(r["probability_yes"]), 1 if r["result"] == "yes" else 0)
                for r in rows]
    return [(float(r["yes_ask"]) / 100.0, 1 if r["result"] == "yes" else 0)
            for r in rows]


def settlement_dates(rows) -> list:
    """Distinct UTC settlement dates. Rows sharing one date resolve from one
    underlying price path, so they are not independent observations."""
    dates = {str(r.get("settled_at"))[:10] for r in rows
             if r.get("settled_at")}
    return sorted(d for d in dates if len(d) == 10)


def score(rows, label: str) -> dict:
    model, market = _pairs(rows, "model"), _pairs(rows, "market")
    bm, bb = brier(model), brier(market)
    return {
        "label": label,
        "n": len(rows),
        "brier_model": round(bm, 6) if bm is not None else None,
        "brier_market_baseline": round(bb, 6) if bb is not None else None,
        "delta_model_minus_baseline": (round(bm - bb, 6)
                                       if bm is not None and bb is not None
                                       else None),
        "model_beats_market": (bm < bb) if bm is not None and bb is not None
                              else None,
    }


def analyse(records, dataset_sha256: str, split=DEFAULT_SPLIT,
            min_settled: int = GATE_MIN_SETTLED) -> dict:
    rows = usable_rows(records)
    train, val, test = split_chronological(rows, split)
    test_score = score(test, "test")
    dates = settlement_dates(rows)

    gates = [
        {"name": "settled_predictions",
         "required": f">= {min_settled}",
         "observed": len(rows),
         "passed": len(rows) >= min_settled},
        {"name": "test_slice_non_empty",
         "required": ">= 1",
         "observed": len(test),
         "passed": len(test) >= 1},
        {"name": "model_brier_beats_market_baseline_out_of_sample",
         "required": "brier_model < brier_market_baseline on the TEST slice",
         "observed": test_score["delta_model_minus_baseline"],
         "passed": test_score["model_beats_market"]},
        # Threshold deliberately absent: T7-I sect. 9 records it as
        # "REQUIRES OPERATOR APPROVAL". Reporting the count without a
        # sanctioned threshold is honest; inventing one would not be.
        {"name": "independent_settlement_dates",
         "required": "OPERATOR THRESHOLD NOT DEFINED",
         "observed": len(dates),
         "passed": None},
    ]
    undefined = [g["name"] for g in gates if g["passed"] is None]
    failed = [g["name"] for g in gates if g["passed"] is False]
    if failed:
        verdict = "FAIL"
    elif undefined:
        verdict = "INDETERMINATE"
    else:
        verdict = "PASS"

    return {
        "generated_at": datetime.now(timezone.utc)
                                .isoformat(timespec="seconds")
                                .replace("+00:00", "Z"),
        "dataset_sha256": dataset_sha256,
        "n_records_total": len(records or []),
        "n_usable": len(rows),
        "split": {"chronological": True, "ratios": list(split),
                  "train": len(train), "validation": len(val),
                  "test": len(test)},
        "decisive_slice": "test",
        "test": test_score,
        # Reported for context only. The guide's rule is out-of-sample; a
        # favourable full-sample number is exactly what it exists to reject.
        "context_only_not_decisive": {
            "train": score(train, "train"),
            "validation": score(val, "validation"),
            "full_sample": score(rows, "full_sample"),
        },
        "settlement_dates": dates,
        "n_settlement_dates": len(dates),
        "model_versions": sorted({str(r.get("features", {})
                                       .get("model_version"))
                                  for r in rows}),
        "gates": gates,
        "undefined_gates": undefined,
        "failed_gates": failed,
        "verdict": verdict,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Out-of-sample Brier: model vs market baseline")
    ap.add_argument("path", help="shadow_predictions.json")
    ap.add_argument("--min-settled", type=int, default=GATE_MIN_SETTLED)
    ap.add_argument("--out", default=None, help="write the report here too")
    args = ap.parse_args(argv)

    raw = open(args.path, "rb").read()
    report = analyse(json.loads(raw.decode("utf-8")),
                     hashlib.sha256(raw).hexdigest(),
                     min_settled=args.min_settled)
    text = json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
