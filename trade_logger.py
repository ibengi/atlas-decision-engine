"""
TradeLogger — Journal des trades reels Kalshi.
Extrait de kalshi_alpha_bot.py (PR #16, P3.5+).
"""

import logging
import uuid
from datetime import datetime, timezone

from config import CFG, _p
from persistence import JsonStore

# Module-level logger (meme format que dans kalshi_alpha_bot.py)
log_trd = logging.getLogger("TRADE")


def now_iso() -> str:
    """Horodatage UTC ISO 8601 a la seconde pres."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeLogger:
    """Journal des trades. REGLE ABSOLUE : on n'enregistre un trade que si
    filled_count > 0 (execution verifiee). Les anciens enregistrements
    'dry_run' sont archives a part, jamais melanges aux vrais."""

    SCHEMA = "v11"

    def __init__(self):
        self.path = _p(CFG.TRADES_FILE)
        raw = JsonStore.load(self.path, [])
        legacy = [t for t in raw if t.get("schema") != self.SCHEMA]
        self.trades = [t for t in raw if t.get("schema") == self.SCHEMA]
        if legacy:
            legacy_path = _p("kalshi_trades_legacy.json")
            old = JsonStore.load(legacy_path, [])
            JsonStore.save(legacy_path, old + legacy)
            JsonStore.save(self.path, self.trades)
            log_trd.warning(f"{len(legacy)} enregistrement(s) heritee(s) "
                            f"(dry-run/ancien schema) archives dans "
                            f"kalshi_trades_legacy.json -- exclus des statistiques.")

    def open_trade(self, *, ticker, market_title, side, req_price, avg_price,
                   req_count, filled_count, spread, fees, edge, ev, confidence,
                   grade, reason, analysis, order_id, order_status,
                   decision_id=None) -> dict:
        # decision_id: the originating Decision's identifier, attached at the
        # moment the trade row is born and never rewritten afterwards
        # (settle_trade updates only settlement fields). It is the canonical
        # Decision -> Trade -> Settlement lifecycle key; None means the trade
        # did not originate from a traceable decision (e.g. crash recovery)
        # and stays honestly unjoinable.
        rec = {
            "schema": self.SCHEMA, "trade_id": uuid.uuid4().hex[:12],
            "decision_id": decision_id,
            "timestamp": now_iso(), "ticker": ticker, "market": market_title,
            "side": side, "requested_price": req_price, "avg_fill_price": avg_price,
            "requested_count": req_count, "filled_count": filled_count,
            "spread": spread, "fees": round(fees, 2), "edge": edge, "ev": ev,
            "confidence": confidence, "grade": grade, "reason": reason,
            "analysis": analysis, "order_id": order_id, "order_status": order_status,
            "state": "open",       # open -> settled | expired
            "result": None, "won": None,
            "gross_pnl": None, "net_pnl": None, "roi": None,
            "holding_seconds": None, "settled_at": None,
        }
        self.trades.append(rec)
        self.flush()
        log_trd.info(f"OUVERT {ticker} {side.upper()} {filled_count}/{req_count} "
                     f"@ {avg_price}c (frais {fees:.2f}$) ordre={order_id}")
        return rec

    def settle_trade(self, trade_id: str, result: str, won: bool,
                     gross_pnl: float, net_pnl: float):
        for t in self.trades:
            if t["trade_id"] == trade_id:
                opened = datetime.fromisoformat(t["timestamp"])
                t.update({
                    "state": "settled", "result": result, "won": won,
                    "gross_pnl": round(gross_pnl, 2), "net_pnl": round(net_pnl, 2),
                    "roi": round(net_pnl / max(0.01, t["avg_fill_price"] / 100.0
                                               * t["filled_count"]), 4),
                    "settled_at": now_iso(),
                    "holding_seconds": int((datetime.now(timezone.utc) - opened)
                                           .total_seconds()),
                })
                self.flush()
                log_trd.info(f"REGLE  {t['ticker']} -> {result.upper()} | "
                             f"{'GAGNE' if won else 'PERDU'} | net {net_pnl:+.2f}$")
                return t
        log_trd.error(f"settle_trade: trade_id {trade_id} introuvable.")
        return None

    def has_open_on(self, ticker: str) -> bool:
        return any(t["ticker"] == ticker and t["state"] == "open" for t in self.trades)

    def open_trades(self) -> list:
        return [t for t in self.trades if t["state"] == "open"]

    def settled_trades(self) -> list:
        return [t for t in self.trades if t["state"] == "settled"]

    def flush(self):
        JsonStore.save(self.path, self.trades)
