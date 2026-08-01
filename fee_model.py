"""
FeeModel — Frais de trading Kalshi.
Extrait de kalshi_alpha_bot.py (PR #16, P3.5+).
"""

import math
from typing import Optional

from config import CFG


class FeeModel:
    """Frais de trading Kalshi. PRIORITE : frais REELS de l'API (ordre puis
    fills), formule locale 0.07 x C x P x (1-P) (arrondi cent sup.) en
    SECOURS uniquement — taux reglable via KALSHI_FEE_RATE_TRADING."""

    API_FIELDS = ("taker_fees", "maker_fees", "fees", "fee",
                  "average_fee_paid", "taker_fees_dollars",
                  "maker_fees_dollars", "fees_dollars", "fee_dollars")

    @staticmethod
    def trading_fee(count: int, price_cents: int) -> float:
        p = max(1, min(99, price_cents)) / 100.0
        return math.ceil(CFG.FEE_RATE * count * p * (1 - p) * 100) / 100.0

    @classmethod
    def _amount(cls, d: dict) -> Optional[float]:
        for k in cls.API_FIELDS:
            v = d.get(k)
            if v in (None, ""):
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x < 0:
                continue
            # heuristique unites : *_dollars => $, sinon si entier >= 1 et
            # sans point decimal dans la source, probablement des cents.
            if k.endswith("_dollars"):
                return round(x, 4)
            if isinstance(v, str) and "." in v:
                return round(x, 4)                # "0.07" => dollars
            return round(x / 100.0, 4) if x >= 1 and float(x).is_integer() \
                else round(x, 4)
        return None

    @classmethod
    def from_api(cls, order_resp: dict, fills: list,
                 count: int, price_cents: int) -> (float, str):
        """Retourne (frais_$, fee_source). Ordre de priorite :
        1. champs de frais de la reponse d'ordre ;
        2. somme des frais des fills ;
        3. formule locale (fee_source='estimated')."""
        amt = cls._amount(order_resp or {})
        if amt is not None:
            return amt, "api"
        if fills:
            parts = [cls._amount(f) for f in fills]
            parts = [p for p in parts if p is not None]
            if parts:
                return round(sum(parts), 4), "api"
        return cls.trading_fee(count, price_cents), "estimated"
