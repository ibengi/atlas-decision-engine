"""
SignalValidator — Validation des signaux d'achat (ACHETER YES/NO) avant execution.
Extrait de kalshi_alpha_bot.py (P3.13).
"""
import logging
from config import CFG
from position_manager import PositionManager
from trade_logger import TradeLogger
# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log = logging.getLogger("BOT")
class SignalValidator:
    @staticmethod
    def check(verdict: str, entry_price: int, ticker: str,
              tlog: TradeLogger, posmgr: PositionManager) -> (bool, str):
        if verdict not in ("ACHETER YES", "ACHETER NO"):
            return False, "aucun signal"
        if entry_price > CFG.MAX_ENTRY_CENTS:
            return False, (f"prix d'entree {entry_price}c > plafond "
                           f"{CFG.MAX_ENTRY_CENTS}c (ratio risque/gain)")
        if entry_price < 1 or entry_price > 99:
            return False, f"prix d'entree invalide: {entry_price}c"
        if CFG.ONE_TRADE_PER_MKT and (ticker in posmgr.tickers_open()
                                      or tlog.has_open_on(ticker)):
            return False, "position deja prise sur ce marche (1 trade/marche)"
        return True, ""
