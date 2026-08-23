"""Position sizing with legacy fixed sizing and optional fractional Kelly."""
import logging
import math

from config import CFG

log_rsk = logging.getLogger("RISK")


class PositionSizer:
    """Compute executable contract counts from capital and a decision signal.

    Kelly is deliberately opt-in.  With ``KELLY_ENABLED=0`` the legacy sizing
    path is byte-for-byte equivalent in its calculations and limits.
    """

    @staticmethod
    def full_kelly(probability: float, price_cents: int, side: str = "yes") -> float:
        """Return full Kelly as a bankroll fraction for the bought outcome.

        ``probability`` is the model's YES probability (0..1); for a NO order
        it is complemented. Invalid/degenerate prices and non-positive edge
        return zero rather than producing an unsafe wager.
        """
        try:
            p_yes = float(probability)
            price = float(price_cents) / 100.0
        except (TypeError, ValueError):
            return 0.0
        if p_yes > 1.0:  # convenient support for percentage probabilities
            p_yes /= 100.0
        if not math.isfinite(p_yes) or not 0.0 <= p_yes <= 1.0:
            return 0.0
        if side and str(side).lower() == "no":
            p = 1.0 - p_yes
        else:
            p = p_yes
        if not (0.0 < price < 1.0):
            return 0.0
        b = (1.0 - price) / price
        q = 1.0 - p
        return max(0.0, (p * b - q) / b)

    @staticmethod
    def _legacy(capital, price_cents, taille_str, confidence, drawdown, open_risk):
        base_pct = {"0.5%": 0.5, "1%": 1.0, "2%": 2.0}.get(taille_str)
        if base_pct is None or price_cents <= 0:
            return 0
        pct = min(base_pct, CFG.MAX_POS_PCT)
        if confidence <= 4:
            pct *= 0.5
        if capital > 0 and drawdown / capital * 100.0 >= CFG.DD_THROTTLE_PCT:
            pct *= 0.5
            log_rsk.info(f"Sizer: drawdown {drawdown:.2f}$ >= "
                         f"{CFG.DD_THROTTLE_PCT:g}% du capital -- taille reduite.")
        budget_left = capital * CFG.RISK_BUDGET_PCT / 100.0 - open_risk
        alloc = min(capital * pct / 100.0, max(0.0, budget_left))
        return max(0, int(alloc / (price_cents / 100.0)))

    @staticmethod
    def contracts(capital: float, price_cents: int, taille_str: str,
                  confidence: int, drawdown: float, open_risk: float,
                  probability=None, side: str = "yes") -> int:
        if not getattr(CFG, "KELLY_ENABLED", False) or probability is None:
            return PositionSizer._legacy(capital, price_cents, taille_str,
                                         confidence, drawdown, open_risk)
        if capital <= 0 or price_cents <= 0:
            return 0
        full = PositionSizer.full_kelly(probability, price_cents, side)
        fraction = max(0.0, float(getattr(CFG, "KELLY_FRACTION", 0.5)))
        max_pct = min(float(CFG.MAX_POS_PCT),
                      float(getattr(CFG, "KELLY_MAX_POS_PCT", 10.0)))
        pct = min(full * fraction * 100.0, max_pct)
        if pct <= 0:
            return 0
        if confidence <= 4:
            pct *= 0.5
        if capital > 0 and drawdown / capital * 100.0 >= CFG.DD_THROTTLE_PCT:
            pct *= 0.5
        budget_left = capital * CFG.RISK_BUDGET_PCT / 100.0 - open_risk
        alloc = min(capital * pct / 100.0, max(0.0, budget_left))
        # AIR-001 Wave 6 (DE-P0-008): a minimum bet must never RAISE an
        # allocation above what the percentage caps computed (the old
        # code lifted a capped allocation up to KELLY_MIN_BET, silently
        # overriding MAX_POS_PCT/KELLY_MAX_POS_PCT). Below the minimum
        # tradable size: NO TRADE.
        minimum = max(0.0, float(getattr(CFG, "KELLY_MIN_BET", 1.0)))
        if alloc < minimum:
            return 0
        return max(0, int(alloc / (price_cents / 100.0)))
