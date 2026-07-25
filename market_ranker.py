"""
market_ranker.py — v2-ref (2026-07-24)
Classement de tradabilite. REGLE : un market_type 'unknown' ne recoit
JAMAIS un score eleve (plafond 10/100). Le score n'entre pas dans les
portes edge/EV ; il ne sert qu'a prioriser l'evaluation couteuse.
"""

import json
import logging

from market_scanner import MarketScanner
from market_classifier import classify

log = logging.getLogger("RANKER")


def tradability_score(m: dict) -> float:
    c = m.get("_classification") or classify(m).to_dict()
    if c["market_type"] == "unknown":
        return min(10.0, 5.0)          # plafonne : jamais "eligible"
    score = 30.0                       # type connu et route-able
    spread = None
    try:
        ya, yb = int(float(m.get("yes_ask") or 0)), int(float(m.get("yes_bid") or 0))
        if 1 <= yb <= ya <= 99:
            spread = ya - yb
    except (TypeError, ValueError):
        pass
    if spread is not None:
        score += max(0.0, 30.0 - 5.0 * spread)
    vol = 0.0
    for k in ("volume_24h", "volume", "open_interest"):
        try:
            vol = max(vol, float(m.get(k) or 0))
        except (TypeError, ValueError):
            pass
    score += min(30.0, vol / 100.0)
    mins = m.get("_minutes_remaining")
    if mins is not None and mins >= 15:
        score += 10.0
    return round(min(100.0, score), 1)


def run_ranking(client, router=None) -> dict:
    sc = MarketScanner(client, router=router)
    res = sc.scan_cycle()
    scored = []
    for m in res["markets"]:
        s = tradability_score(m)
        scored.append({"ticker": m.get("ticker"), "score": s,
                       "classification": m.get("_classification")})
    scored.sort(key=lambda x: x["score"], reverse=True)
    eligible = [s for s in scored if s["score"] >= 50.0]
    report = {"markets_scored": len(scored), "eligible": len(eligible),
              "excluded_by_reason": res["report"]["excluded_by_reason"]}
    try:
        with open("market_rankings.json", "w", encoding="utf-8") as f:
            json.dump(scored, f, indent=1)
        with open("market_ranker_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    except OSError:
        pass
    return {"report": report, "rankings": scored}
