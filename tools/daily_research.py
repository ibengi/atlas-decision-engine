"""
daily_research.py — event-level walk-forward for the BTC DAILY evidence.

Bridges the T7-I evidence store (btc_daily_predictions.jsonl +
btc_daily_settlements.jsonl) to the walk-forward harness in
tools/model_research.py, with three rules that store cannot enforce on its
own:

  1. ONE ROW PER EVENT PER CHECKPOINT. The live pipeline writes a row for
     the same ticker every cycle, hundreds of times over a day. Those rows
     share one outcome. Each ticker contributes at most one observation per
     time-to-expiry checkpoint — the last observation before the checkpoint
     — so N counts decisions, not cycles.

  2. LABELS MUST BE CONFIRMED. A settlement row without `confirmed_by` was
     frozen from a first poll at close time; measured on 2026-09-02 those
     rows were wrong 62 times out of 62 verifiable cases. They are excluded
     unless `--allow-unconfirmed` is passed, and then the report says so.

  3. A DEGENERATE LABEL STOPS THE STUDY. If the minority class is under
     `MIN_MINORITY_FRAC` of settled events, or there are fewer than two
     settlement windows, no candidate is ranked: a model that always says
     "no" scores a perfect Brier against an all-"no" journal, and that is
     the journal talking, not the model.

Tradeable slices are reported separately (spread <=4, <=6, <=10, and
pipeline-accepted) so that edge is never claimed from books the engine
could not have traded.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import model_research as mr          # noqa: E402

MIN_MINORITY_FRAC = 0.05
CHECKPOINTS_MIN = (1440, 720, 360, 180, 60)


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def checkpoint_of(minutes):
    """Smallest checkpoint >= minutes, as the shadow observer defines it."""
    best = None
    for cp in CHECKPOINTS_MIN:
        if minutes <= cp and (best is None or cp < best):
            best = cp
    return best


def to_harness_row(p: dict, s: dict):
    """Translate an evidence prediction + its settlement into the row
    shape tools/model_research consumes. Missing fields stay missing."""
    ask, bid = p.get("market_yes_ask"), p.get("market_yes_bid")
    return {
        "ts": p.get("observed_at"), "ticker": p.get("ticker"),
        "result": s.get("result"), "settled_at": s.get("settled_at"),
        "probability_yes": p.get("predicted_probability"),
        "yes_ask": ask, "yes_bid": bid,
        "spot": p.get("underlying_price"), "strike": p.get("strike"),
        "sigma_1m": p.get("sigma_1m"),
        "minutes_remaining": p.get("minutes_remaining"),
        "ret_5m": p.get("ret_5m"),
        "spread": (ask - bid) if ask is not None and bid is not None else None,
        "strike_source": p.get("strike_source"),
        "decision_accepted": bool(p.get("decision_accepted")),
        "origin": p.get("origin"), "checkpoint": None,
    }


def load_events(predictions, settlements, allow_unconfirmed=False,
                allow_spot_proxy=False):
    """Event-level rows: one per (ticker, checkpoint), the last observation
    before the checkpoint. Returns (rows, exclusions)."""
    final = {}
    for r in settlements:
        if r.get("kind") == "observation" or not r.get("record_id"):
            continue
        final[r["record_id"]] = r
    excl = defaultdict(int)
    best = {}
    for p in predictions:
        s = final.get(p.get("record_id"))
        if not s or s.get("result") not in ("yes", "no"):
            excl["unsettled"] += 1
            continue
        if not allow_unconfirmed and not s.get("confirmed_by"):
            excl["unconfirmed_label"] += 1
            continue
        if not allow_spot_proxy and p.get("strike_source") == "spot_proxy":
            excl["spot_proxy_strike"] += 1
            continue
        row = to_harness_row(p, s)
        m = row.get("minutes_remaining")
        if not isinstance(m, (int, float)) or m <= 0:
            excl["no_horizon"] += 1
            continue
        cp = checkpoint_of(m)
        if cp is None:
            excl["beyond_checkpoints"] += 1
            continue
        row["checkpoint"] = cp
        key = (row["ticker"], cp)
        if key not in best or m < best[key]["minutes_remaining"]:
            best[key] = row
    rows = list(best.values())
    rows.sort(key=lambda r: _ts(r["ts"]))
    return rows, dict(excl)


def label_health(rows) -> dict:
    events = {}
    for r in rows:
        events[r["ticker"]] = r["result"]
    n = len(events)
    yes = sum(1 for v in events.values() if v == "yes")
    minority = min(yes, n - yes) / n if n else 0.0
    dates = {str(r.get("settled_at"))[:10] for r in rows if r.get("settled_at")}
    return {"events": n, "yes": yes, "no": n - yes,
            "minority_frac": round(minority, 4),
            "settlement_dates": sorted(dates),
            "degenerate": bool(n < 2 or minority < MIN_MINORITY_FRAC)}


def slices(rows) -> dict:
    def sp(r):
        return r.get("spread")
    return {
        "all": rows,
        "spread_le_4": [r for r in rows if sp(r) is not None and sp(r) <= 4],
        "spread_le_6": [r for r in rows if sp(r) is not None and sp(r) <= 6],
        "spread_le_10": [r for r in rows if sp(r) is not None and sp(r) <= 10],
        "pipeline_accepted": [r for r in rows if r.get("decision_accepted")],
    }


def study(predictions, settlements, n_folds=4, allow_unconfirmed=False,
          allow_spot_proxy=False) -> dict:
    rows, excl = load_events(predictions, settlements, allow_unconfirmed,
                             allow_spot_proxy)
    usable = mr.usable_rows(rows)
    health = label_health(usable)
    out = {"event_rows": len(rows), "usable_rows": len(usable),
           "exclusions": excl, "label_health": health,
           "allow_unconfirmed": allow_unconfirmed,
           "allow_spot_proxy": allow_spot_proxy,
           "slices": {k: len(v) for k, v in slices(usable).items()}}
    if health["degenerate"]:
        out["verdict"] = "INDETERMINATE"
        out["reason"] = ("degenerate label: a study on it would score the "
                         "journal, not the model")
        return out
    out["results"] = {}
    for name, rs in slices(usable).items():
        if len(rs) < 10:
            out["results"][name] = {"n": len(rs), "skipped": "too few rows"}
            continue
        out["results"][name] = mr.evaluate(rs, n_folds=n_folds)
    sig = [r["model"] for r in out["results"].get("all", {}).get("results", [])
           if r.get("beats_market_significantly")]
    out["verdict"] = "CANDIDATE_BEATS_MARKET" if sig else "NO_MODEL_BEATS_MARKET"
    out["significant_candidates"] = sig
    return out


def _jsonl(path):
    raw = open(path, "rb").read()
    return ([json.loads(l) for l in raw.decode("utf-8").splitlines()
             if l.strip()], hashlib.sha256(raw).hexdigest())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Event-level daily study")
    ap.add_argument("predictions")
    ap.add_argument("settlements")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--allow-unconfirmed", action="store_true")
    ap.add_argument("--allow-spot-proxy", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    preds, hp = _jsonl(a.predictions)
    setts, hs = _jsonl(a.settlements)
    rep = study(preds, setts, a.folds, a.allow_unconfirmed, a.allow_spot_proxy)
    rep = {"generated_at": datetime.now(timezone.utc)
                                   .isoformat(timespec="seconds")
                                   .replace("+00:00", "Z"),
           "predictions_sha256": hp, "settlements_sha256": hs, **rep}
    text = json.dumps(rep, indent=1, ensure_ascii=False, allow_nan=False)
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
    return 0 if rep["verdict"] == "CANDIDATE_BEATS_MARKET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
