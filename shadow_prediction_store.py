"""
shadow_prediction_store.py — v2-ref (2026-07-24)
Journal des predictions shadow (base de la calibration et du backtest).

NOTE d'honnetete : le fichier original n'a pas ete fourni ; cette version
implemente le contrat consomme par kalshi_alpha_bot.ExecutionEngine :
  record(...), settle_pending(get_market_fn) -> int, settled() -> list.
Les enregistrements sont compatibles avec le format d'observation attendu
par backtest_btc15m.py (ts, ticker, spot, strike, sigma_1m,
minutes_remaining, ret_5m, yes_bid/ask, no_bid/ask, result).
"""

import os
import json
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShadowPredictionStore:
    def __init__(self, path: str):
        self.path = path
        self.records = []
        try:
            with open(path, encoding="utf-8") as f:
                self.records = json.load(f) or []
        except (OSError, ValueError):
            self.records = []

    def _flush(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def record(self, *, ticker, cycle_ts_iso=None, market=None, strike=None,
               spot=None, minutes_remaining=None, yes_bid=None, yes_ask=None,
               no_bid=None, no_ask=None, spread=None, ranker_score=None,
               features=None, probability_yes=None, probability_no=None,
               confidence=None, estimated_fee=None, estimated_slippage=None,
               gross_edge=None, net_edge=None, net_ev=None,
               shadow_decision=None, decision_reason=None):
        feats = features or {}
        self.records.append({
            "ts": cycle_ts_iso or _now_iso(), "ticker": ticker,
            "spot": spot if spot is not None else feats.get("spot"),
            "strike": strike if strike is not None else feats.get("strike"),
            "sigma_1m": feats.get("sigma_1m"),
            "minutes_remaining": minutes_remaining
            if minutes_remaining is not None
            else feats.get("minutes_remaining"),
            "ret_5m": feats.get("ret_5m"),
            "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": no_bid, "no_ask": no_ask, "spread": spread,
            "ranker_score": ranker_score, "features": feats,
            "probability_yes": probability_yes,
            "probability_no": probability_no, "confidence": confidence,
            "estimated_fee": estimated_fee,
            "estimated_slippage": estimated_slippage,
            "gross_edge": gross_edge, "net_edge": net_edge, "net_ev": net_ev,
            "shadow_decision": shadow_decision,
            "decision_reason": decision_reason,
            "result": None, "settled_at": None,
        })
        self._flush()

    def pending(self):
        return [r for r in self.records if r.get("result") is None]

    def settled(self):
        return [r for r in self.records if r.get("result") in ("yes", "no")]

    def settle_pending(self, get_market_fn) -> int:
        """Regle les predictions dont le marche a un 'result' API yes/no.
        AUCUN reglement suppose sans confirmation API."""
        n = 0
        seen = {}
        for r in self.pending():
            tk = r["ticker"]
            if tk not in seen:
                m = get_market_fn(tk) or {}
                seen[tk] = str(m.get("result") or "").lower()
            res = seen[tk]
            if res in ("yes", "no"):
                r["result"] = res
                r["settled_at"] = _now_iso()
                n += 1
        if n:
            self._flush()
        return n
