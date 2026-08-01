"""
BtcStrategy — Strategie BTC et selection du marche ATM.
Extrait de kalshi_alpha_bot.py (P3.14).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from config import CFG
from kalshi_client import KalshiClient, pick
from market_validator import MarketValidator

log = logging.getLogger("BOT")

try:
    from btc_context import get_btc_context, get_btc_price  # v2 (contexte)
    try:
        from btc_context import evaluate_btc_trade   # legacy v1, optionnel
    except ImportError:
        evaluate_btc_trade = None
    try:
        from btc_context import VERSION as BTC_CTX_VERSION
    except ImportError:
        BTC_CTX_VERSION = "inconnue"
    BTC_AVAILABLE = True
except ImportError:
    BTC_AVAILABLE, BTC_CTX_VERSION = False, "absente"
    log.warning("btc_context absent -- strategie crypto DESACTIVEE explicitement: aucun modele de probabilite => aucun trade (rejets 'no_model_probability'). Le pipeline, --scan-only, --rank-only et --shadow restent operationnels.")
class BtcStrategy:
    """Selection du marche ATM + decision par btc_context.
    Probabilite modele = probabilite marche (strategie de suivi) donc
    edge = 0.0, affiche et enregistre comme tel. Aucune valeur fictive."""

    def __init__(self, client: KalshiClient):
        self.client = client

    def _select_market(self):
        markets = self.client.get_markets(CFG.SERIES, status="open", limit=50)
        if not markets:
            log.warning(f"Aucun marche '{CFG.SERIES}' renvoye par "
                        f"l'environnement {self.client.env} -- si cela persiste, "
                        f"cet environnement ne liste pas cette serie.")
            return None, None, None
        now = datetime.now(timezone.utc)
        spot = get_btc_price() if BTC_AVAILABLE else None
        best, best_key = None, None
        diag = {"cand": len(markets), "no_ct": 0, "bad_ct": 0, "soon": 0, "dmax": None}
        for m in markets:
            ct = m.get("close_time")
            if not ct: diag["no_ct"] += 1; continue
            try:
                close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            except ValueError:
                diag["bad_ct"] += 1; continue
            mins = (close_dt - now).total_seconds() / 60.0
            diag["dmax"] = mins if diag["dmax"] is None else max(diag["dmax"], mins)
            if mins < CFG.MIN_MINUTES:
                diag["soon"] += 1; continue
            strike = pick(m, "floor_strike", "cap_strike", "strike_price", default=None)
            try: strike = float(strike)
            except (TypeError, ValueError): continue
            key = abs((spot or strike) - strike)
            if best is None or key < best_key or (key == best_key and mins < best[1]):
                best, best_key = (m, mins, strike), key
        if best is None:
            log.warning(f"Aucun marche exploitable. [DIAG] {diag}")
            return None, None, None
        m, mins, strike = best
        log.info(f"Marche ATM: {m.get('ticker')} | strike={strike:,.2f} | "
                 f"t={mins:.1f}min | spot={f'{spot:,.2f}' if spot else 'N/A'}")
        return m, mins, strike

    def signal(self):
        """Retourne (market, book, decision) ou (None, None, None)."""
        if evaluate_btc_trade is None:
            return None, None, None      # legacy indisponible (btc_context v2)
        m, mins, strike = self._select_market()
        if not m: return None, None, None
        book = MarketValidator.normalize_book(m)
        if not book:
            raw = {k: m.get(k) for k in ("ticker", "yes_bid", "yes_ask",
                                         "no_bid", "no_ask", "status")}
            self.client._log_raw_once("market_book", raw)
            sides = [m.get(k) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")]
            vide = all(not s for s in sides)   # tout 0/None = aucune liquidite
            log.warning(f"Carnet {'VIDE (aucune liquidite sur cet environnement)' if vide else 'incoherent/incomplet'} "
                        f"sur {m.get('ticker')} -- aucun trade. "
                        f"[book] {raw}")
            return None, None, None
        log.info(f"Carnet: yes {book['yes_bid']}/{book['yes_ask']}c "
                 f"(mid={book['yes_mid']}c) | no mid={book['no_mid']}c | "
                 f"spread={book['spread']}c")
        res = evaluate_btc_trade(
            strike_price=strike,
            market_yes_price_cents=book["yes_mid"],
            market_no_price_cents=book["no_mid"],
            minutes_remaining=mins,
        )
        verdict = res.get("verdict", "AUCUN TRADE")
        side = "yes" if verdict == "ACHETER YES" else "no"
        if verdict == "ACHETER YES":
            entry = min(book["yes_ask"], book["yes_bid"] + CFG.MAX_SPREAD_PAY)
        elif verdict == "ACHETER NO":
            entry = min(book["no_ask"], book["no_bid"] + CFG.MAX_SPREAD_PAY)
        else:
            entry = book["yes_mid"]
        decision = {
            "verdict": verdict, "side": side, "entry_price": entry,
            "market_prob": (book["yes_mid"] if side == "yes" else book["no_mid"]) / 100.0,
            "model_prob":  (book["yes_mid"] if side == "yes" else book["no_mid"]) / 100.0,
            "edge": 0.0,                       # suivi de marche : edge reel nul
            "ev": 0.0,
            "confidence": res.get("confiance", 0), "grade": res.get("grade", "C"),
            "taille": res.get("taille_position", "0%"),
            "reason": res.get("raison_principale", ""),
        }
        return m, book, decision

    def decide(self, market: dict, book: dict) -> Optional[dict]:
        """Evaluation sur un marche/carnet fournis (utilisee par le routeur).
        Retourne l'analyse brute ; model_prob n'est renseignee QUE si
        btc_context fournit une probabilite independante (prob_reelle),
        sinon None -> le routeur rejette no_model_probability. RIEN n'est
        invente ici."""
        if not BTC_AVAILABLE or evaluate_btc_trade is None \
                or not market or not book:
            return None
        strike = pick(market, "floor_strike", "cap_strike", "strike_price",
                      default=None)
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            return None
        ct = market.get("close_time")
        try:
            close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            mins = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
        except (TypeError, ValueError):
            return None
        res = evaluate_btc_trade(
            strike_price=strike,
            market_yes_price_cents=book.get("yes_mid",
                                            (book["yes_bid"] + book["yes_ask"]) // 2),
            market_no_price_cents=book.get("no_mid",
                                           (book["no_bid"] + book["no_ask"]) // 2),
            minutes_remaining=mins) or {}
        verdict = res.get("verdict", "AUCUN TRADE")
        side = "yes" if verdict == "ACHETER YES" else \
               "no" if verdict == "ACHETER NO" else None
        if side is None:
            return None
        market_p = (book.get("yes_mid", 50) if side == "yes"
                    else book.get("no_mid", 50)) / 100.0
        return {"verdict": verdict, "side": side,
                "model_prob": res.get("prob_reelle"),   # None si non fournie
                "market_prob": market_p,
                "confidence": res.get("confiance", 0),
                "taille": res.get("taille_position", "0.5%"),
                "reason": res.get("raison_principale", "")}

