"""check_replay_fidelity.py — P-Sat-1 reproduction-fidelity gate (QA3b).

Pre-registered prediction #4 (PRE-REGISTRATION-btc-replay.md section 9) is a
HARD STOP for the whole audit: on rows where the momentum clip binds,

    p_yes == clamp(Phi(d + 0.5 * sign(r_5)))      to within 1e-12

Any violation means the replay does NOT reproduce production and the analysis
must halt. This script re-verifies the identity INDEPENDENTLY from the dataset
file on disk, using production's own norm_cdf and clamp constants (imported
from btc_probability_model — fidelity by import, not reimplementation).

Usage (from the repo root):

    .venv/bin/python research/check_replay_fidelity.py [--csv research/data/replay_btc_15m_2026-02.csv.gz]

Exit code 0 = PASS (max abs deviation <= 1e-12), 1 = FAIL (analysis must stop).
Also writes the result JSON next to the CSV when --json is given.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from btc_probability_model import P_CEIL, P_FLOOR, norm_cdf  # noqa: E402

THRESHOLD = 1e-12


def clamp_p(x: float) -> float:
    return min(P_CEIL, max(P_FLOOR, x))


def check_file(csv_path: str) -> dict:
    n_rows = 0
    n_capped = 0
    max_dev_capped = 0.0
    max_dev_all = 0.0
    worst = None
    with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            n_rows += 1
            d = float(r["d"])
            p_yes = float(r["p_yes"])
            if int(r["clip_binding"]) == 1:
                n_capped += 1
                sign = int(r["sign_5m_return"])
                expected = clamp_p(norm_cdf(d + 0.5 * sign))
                dev = abs(p_yes - expected)
                if dev > max_dev_capped:
                    max_dev_capped = dev
                    worst = (r["event_id"], r["decision_ts"], r["minutes_remaining"])
                if dev > max_dev_all:
                    max_dev_all = dev
            else:
                mu = float(r["momentum_term_postclip"])
                dev = abs(p_yes - clamp_p(norm_cdf(d + mu)))
                if dev > max_dev_all:
                    max_dev_all = dev
    return {
        "csv": os.path.basename(csv_path),
        "n_rows": n_rows,
        "n_capped_rows": n_capped,
        "max_abs_dev_capped_rows": max_dev_capped,
        "max_abs_dev_all_rows": max_dev_all,
        "threshold": THRESHOLD,
        "pass": max_dev_capped <= THRESHOLD,
        "worst_row": worst,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv",
                    default="research/data/replay_btc_15m_2026-02.csv.gz")
    ap.add_argument("--json", default=None,
                    help="write the result JSON next to the CSV if not given")
    args = ap.parse_args(argv)
    res = check_file(args.csv)
    print(f"fidelity check on {res['csv']}")
    print(f"  rows read            : {res['n_rows']}")
    print(f"  momentum-capped rows : {res['n_capped_rows']}")
    print(f"  max |p_yes - clamp(Phi(d + 0.5*sign(r5)))| on capped rows: "
          f"{res['max_abs_dev_capped_rows']:.3e}")
    print(f"  max |p_yes - clamp(Phi(d + mu))| on ALL rows (diagnostic): "
          f"{res['max_abs_dev_all_rows']:.3e}")
    print(f"  threshold            : {res['threshold']:.0e}")
    verdict = "PASS" if res["pass"] else "FAIL — REPLAY DOES NOT REPRODUCE PRODUCTION; ANALYSIS MUST STOP"
    print(f"  VERDICT              : {verdict}")
    if res["worst_row"] is not None:
        print(f"  worst capped row     : event_id={res['worst_row'][0]} "
              f"decision_ts={res['worst_row'][1]} "
              f"minutes_remaining={res['worst_row'][2]}")
    out = args.json or (os.path.splitext(args.csv)[0] + ".fidelity.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, sort_keys=True)
    print(f"  result written to    : {out}")
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
