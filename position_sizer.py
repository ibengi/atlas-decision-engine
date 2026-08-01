"""PositionSizer — Dimensionnement des positions (plafond dur 1% du capital, ajuste au contexte). Extrait de kalshi_alpha_bot.py (P3.11)."""
import logging
from config import CFG
# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log_rsk = logging.getLogger("RISK")
class PositionSizer:
    @staticmethod
    def contracts(capital: float, price_cents: int, taille_str: str,
                  confidence: int, drawdown: float, open_risk: float) -> int:
        base_pct = {"0.5%": 0.5, "1%": 1.0, "2%": 2.0}.get(taille_str)
        if base_pct is None or price_cents <= 0:
            return 0
        pct = min(base_pct, CFG.MAX_POS_PCT)              # plafond dur 1%
        if confidence <= 4:
            pct *= 0.5                                     # signal faible
        if capital > 0 and drawdown / capital * 100.0 >= CFG.DD_THROTTLE_PCT:
            pct *= 0.5                                     # drawdown eleve
            log_rsk.info(f"Sizer: drawdown {drawdown:.2f}$ >= "
                         f"{CFG.DD_THROTTLE_PCT:g}% du capital -- taille reduite.")
        budget_left = capital * CFG.RISK_BUDGET_PCT / 100.0 - open_risk
        alloc = min(capital * pct / 100.0, max(0.0, budget_left))
        return max(0, int(alloc / (price_cents / 100.0)))
