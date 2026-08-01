"""
MarketValidator — Validation et normalisation du carnet de marche (book YES/NO coherent).
Extrait de kalshi_alpha_bot.py (P3.12).
"""
import logging
from typing import Optional
# Module-level logger (meme canal que dans kalshi_alpha_bot.py)
log = logging.getLogger("BOT")
class MarketValidator:
    @staticmethod
    def normalize_book(m: dict) -> Optional[dict]:
        """Carnet coherent ou None. Derive le cote NO du cote YES si absent."""
        def cents(*names):
            # Parseur tolerant partage avec le scanner : gere cents entiers,
            # dollars decimaux (0.48 -> 48c) et variantes *_dollars.
            # Corrige le bug "int(float('0.48'))=0 => carnet vide" qui
            # rejetait 100% des marches si l'API renvoie des dollars.
            from market_scanner import read_price
            for n in names:
                c = read_price(m, n)
                if c is not None:
                    return c
            return None
        yb, ya = cents("yes_bid"), cents("yes_ask")
        nb, na = cents("no_bid"),  cents("no_ask")
        if nb is None and ya is not None: nb = 100 - ya
        if na is None and yb is not None: na = 100 - yb
        if yb is None and na is not None: yb = 100 - na
        if ya is None and nb is not None: ya = 100 - nb
        if yb is None or ya is None:
            return None
        clamp = lambda x: max(1, min(99, int(x)))
        yb, ya, nb, na = clamp(yb), clamp(ya), clamp(nb or 50), clamp(na or 50)
        if ya < yb or na < nb:
            return None
        mid = round((yb + ya) / 2)
        # CORRECTIF AUDIT : l'ancien "if abs((mid+(100-mid))-100) > 0" est
        # une tautologie (mid+(100-mid) vaut TOUJOURS 100, quel que soit
        # mid) -- ce test ne pouvait jamais echouer et ne validait rien.
        # yes_mid/no_mid sont deja garantis dans [1,99] par clamp() plus
        # haut ; supprime pour ne pas laisser croire a une verification.
        return {"yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
                "yes_mid": mid, "no_mid": 100 - mid,
                "spread": ya - yb}


