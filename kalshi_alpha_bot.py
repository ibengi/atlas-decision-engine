#!/usr/bin/env python3
"""
KALSHI ALPHA ENGINE  --  v11.0-pro (2026-07-10)
================================================
Reconstruction professionnelle du moteur de trading.

PRINCIPES :
  - AUCUN dry-run, AUCUNE simulation : tous les ordres sont reellement
    envoyes a l'API Kalshi de l'environnement selectionne (demo ou prod).
  - Un trade n'est enregistre QUE s'il a reellement ete execute
    (fill verifie aupres de l'API), jamais sur simple envoi d'ordre.
  - Le stop-loss quotidien repose UNIQUEMENT sur le PnL REALISE.
  - Aucun edge fictif : pour la strategie de suivi de marche, l'edge
    est honnetement 0 (prob. modele = prob. marche) et affiche comme tel.
  - Persistance atomique + sauvegardes + checksums : aucun JSON corrompu.
  - Recuperation apres crash : ordres et positions reconcilies au demarrage.

ARCHITECTURE :
  Config, JsonStore, KalshiClient, FeeModel, TradeLogger, PositionManager,
  OrderManager, RiskManager, PositionSizer, MarketValidator, SignalValidator,
  StatsEngine, BtcStrategy, ExecutionEngine.

AVERTISSEMENTS D'INTEGRITE (a verifier lors des premiers ordres reels) :
  - Les noms exacts des champs de la reponse "ordre" de l'API Kalshi
    (compteur de fills, quantite restante, prix moyen) sont extraits de
    facon TOLERANTE (plusieurs noms candidats) car la documentation peut
    differer de la realite. La reponse brute est loggee en DEBUG.
  - La formule de frais (0.07 x C x P x (1-P), arrondi au cent sup.)
    doit etre verifiee contre le bareme officiel Kalshi.
  - L'environnement demo (demo-api.kalshi.co) peut ne pas lister les
    marches courants (constate le 2026-07-04) et requiert normalement
    des cles API DEMO distinctes (KALSHI_DEMO_KEY_ID / KALSHI_DEMO_PRIVATE_KEY).
"""

import os, sys, json, time, math, uuid, base64, hashlib, logging, argparse, shutil
from datetime import datetime, timezone, date
from urllib.parse import urlparse
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): return False
import requests

load_dotenv()

ENGINE_VERSION = "v11.4-audit-fixed-2026-07-28"

# ══════════════════════════════════════════════════════════════════════════
# S1. CONFIGURATION (centralisee, surchargeables par variables d'env)
# ══════════════════════════════════════════════════════════════════════════

from config import Config, CFG, _env_b, _env_i, _env_f, _p

# ══════════════════════════════════════════════════════════════════════════
# S2. LOGGING (canaux BOT / API / TRADE / RISK / POSITION / STATS)
# ══════════════════════════════════════════════════════════════════════════

_FMT = "%(asctime)s  %(levelname)-7s [%(name)s] %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format=_FMT, datefmt="%Y-%m-%d %H:%M:%S",
                    stream=sys.stdout)   # stdout: Railway ne marque plus tout en 'error'
log      = logging.getLogger("BOT")
log_api  = logging.getLogger("API")
log_trd  = logging.getLogger("TRADE")
log_rsk  = logging.getLogger("RISK")
log_pos  = logging.getLogger("POSITION")
log_sts  = logging.getLogger("STATS")

from persistence import JsonStore

# ══════════════════════════════════════════════════════════════════════════
# S4. ERREURS API + RETRY/BACKOFF
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# S4-S5. KALSHI API CLIENT
# ══════════════════════════════════════════════════════════════════════════
from kalshi_client import KalshiClient, KalshiAPIError, pick, pick_int
from fee_model import FeeModel
from trade_logger import TradeLogger, now_iso

# ══════════════════════════════════════════════════════════════════════════
# S8. POSITION MANAGER (positions ouvertes, reglement, PnL realise)
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# S8. POSITION MANAGER (extrait dans position_manager.py)
# ══════════════════════════════════════════════════════════════════════════
from position_manager import PositionManager

# ══════════════════════════════════════════════════════════════════════════
# S9. ORDER MANAGER (placement, surveillance, TTL, fills partiels, recovery)
# ══════════════════════════════════════════════════════════════════════════
from execution_result import ExecutionResult

def _client_is_genuine(client) -> bool:
    """Un client authentique est une instance de KalshiClient de CE module,
    avec une base_url Kalshi reelle. Tout double de test (Fake/Mock/patch)
    echoue ce test."""
    return (type(client) is KalshiClient
            and str(getattr(client, "base_url", ""))
            .startswith("https://") and "kalshi.co" in
            str(getattr(client, "base_url", "")))


def assert_real_demo_integrity(client, shadow_mode: bool):
    """Exigence 1 : en EXECUTION_MODE=real_demo, aucun mock, aucun dry-run,
    aucune simulation ne peut remplacer l'appel API reel. Arret FATAL."""
    if CFG.EXECUTION_MODE != "real_demo":
        return
    problems = []
    if not _client_is_genuine(client):
        problems.append(f"client non authentique: {type(client).__name__} "
                        f"({type(client).__module__})")
    if CFG.DRY_RUN:
        problems.append("DRY_RUN=true")
    if shadow_mode:
        problems.append("SHADOW_MODE actif (= simulation)")
    if not CFG.ALLOW_ORDER_SUBMISSION:
        problems.append("ALLOW_ORDER_SUBMISSION=false")
    if problems:
        log.critical("[FATAL] Mock or simulation detected in REAL_DEMO mode: "
                     + "; ".join(problems))
        raise SystemExit(3)


def log_execution_banner(client):
    """Exigence 2 : etat d'execution explicite au demarrage. Les fonds sont
    des fonds DEMO — jamais annonce comme argent reel."""
    genuine = _client_is_genuine(client)
    log.info("[EXECUTION]")
    log.info(f"environment={'DEMO' if client.env == 'demo' else 'PROD'}")
    log.info(f"api_base_url={getattr(client, 'base_url', '?')}")
    log.info(f"execution_mode={CFG.EXECUTION_MODE.upper()}")
    if client.env == "demo" and CFG.EXECUTION_MODE != "real_demo":
        log.info("conseil: definir EXECUTION_MODE=real_demo (Railway) pour "
                 "activer le garde anti-mock ; les chemins d'execution sont "
                 "IDENTIQUES (verifie par test), seule la protection change.")
    log.info(f"dry_run={str(CFG.DRY_RUN or CFG.SHADOW_MODE).lower()}")
    log.info(f"mock_enabled={str(not genuine).lower()}")
    log.info(f"order_submission_enabled="
             f"{str(CFG.ALLOW_ORDER_SUBMISSION and not CFG.SHADOW_MODE).lower()}")
    if client.env == "demo":
        log.info("NOTE: ordres reels sur l'API DEMO — fonds DEMO uniquement, "
                 "aucun argent reel.")


# ══════════════════════════════════════════════════════════════════════════
# S9. ORDER MANAGER (placement, surveillance, TTL, fills partiels, recovery)
# ══════════════════════════════════════════════════════════════════════════
from order_manager import OrderManager
# ══════════════════════════════════════════════════════════════════════════
# S10. RISK MANAGER (base sur le PnL REALISE, jamais le capital investi)
# ══════════════════════════════════════════════════════════════════════════

from risk_manager import RiskManager

# ══════════════════════════════════════════════════════════════════════════
# S11. STATS ENGINE (statistiques completes + rapports periodiques)
# ══════════════════════════════════════════════════════════════════════════

from stats_engine import StatsEngine
# ══════════════════════════════════════════════════════════════════════════
# S12. POSITION SIZER (plafond dur 1% du capital, ajuste au contexte)
# ══════════════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════════════
# S13. VALIDATEURS (marche + signal)
# ══════════════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════════════
# S14. STRATEGIE BTC (btc_context inchange -- edge HONNETE = 0)
# ══════════════════════════════════════════════════════════════════════════

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
    # DESACTIVATION EXPLICITE (pas un masquage) : sans btc_context, la
    # strategie crypto n'a AUCUN fournisseur de probabilite. Le moteur
    # demarre (scan/rank/shadow fonctionnent) mais tout candidat crypto est
    # rejete no_model_probability et AUCUN ordre n'est possible. Comportement
    # verifie par test_repo_integrity (tests 6 et 7).
    BTC_AVAILABLE, BTC_CTX_VERSION = False, "absente"
    log.warning("btc_context absent -- strategie crypto DESACTIVEE "
                "explicitement: aucun modele de probabilite => aucun trade "
                "(rejets 'no_model_probability'). Le pipeline, --scan-only, "
                "--rank-only et --shadow restent operationnels.")

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

# ══════════════════════════════════════════════════════════════════════════
# S15. MOTEUR D'EXECUTION
# ══════════════════════════════════════════════════════════════════════════

class ExecutionEngine:
    """Cycle normal = PIPELINE INTEGRE :
    scanner -> ranker -> routeur -> portes edge/EV -> risque -> execution
    -> verification fills -> reconciliation. Plus de dependance exclusive
    a KXBTC15M ; parcours multi-candidats ; carnet relu juste avant l'ordre."""

    def __init__(self, client: KalshiClient, capital: float):
        from strategy_router import (GateConfig, build_default_registry,
                                     RegistryValidationError)
        from opportunity_pipeline import MarketOpportunityPipeline
        from market_scanner import MarketScanner
        from shadow_prediction_store import ShadowPredictionStore
        self.client   = client
        self.configured_capital = capital           # PLAFOND, pas la verite
        self.capital  = capital                     # effectif (maj par solde)
        self.tlog     = TradeLogger()
        self.posmgr   = PositionManager(client, self.tlog)
        self.orders   = OrderManager(client)
        self.risk     = RiskManager(self.tlog, self.posmgr, capital)
        self.stats    = StatsEngine(self.tlog)
        self.strategy = BtcStrategy(client)          # analyse crypto existante
        # REGISTRE CANONIQUE indexe par market_type (correctif cause racine :
        # la v1 n'enregistrait que la serie KXBTC15M, absente de l'univers,
        # d'ou strategy_supported=0 et no_compatible_strategy partout).
        # ECHEC DE DEMARRAGE si le registre est vide ou incomplet.
        btc_enabled = _env_b("BTC_STRATEGY_ENABLED", default=True)
        try:
            self.router = build_default_registry(
                btc_context_provider=(get_btc_context if BTC_AVAILABLE
                                      else None),
                btc_enabled=btc_enabled)
        except RegistryValidationError as e:
            log.error(f"REGISTRE DE STRATEGIES INVALIDE: {e} -- ARRET.")
            raise SystemExit(2)
        self.scanner = MarketScanner(client, router=self.router,
                                     data_dir=CFG.DATA_DIR)
        self.shadow_store = ShadowPredictionStore(_p("shadow_predictions.json"))
        self.gates = GateConfig(
            MIN_MODEL_CONFIDENCE=CFG.MIN_MODEL_CONFIDENCE,
            MIN_GROSS_EDGE=CFG.MIN_GROSS_EDGE, MIN_NET_EDGE=CFG.MIN_NET_EDGE,
            MIN_NET_EV=CFG.MIN_NET_EV,
            MAX_ACCEPTABLE_SPREAD=CFG.MAX_ACCEPTABLE_SPREAD,
            MIN_MARKET_SCORE=CFG.MIN_MARKET_SCORE,
            MIN_FILL_PROXY=CFG.MIN_FILL_PROXY,
            SLIPPAGE_BUFFER_CENTS=CFG.SLIPPAGE_BUFFER_CENTS,
            FEE_RATE=CFG.FEE_RATE)
        self.pipeline = MarketOpportunityPipeline(
            client, self.router, gates=self.gates,
            fresh_book_fn=self.fresh_book,
            observer=self._shadow_observer,
            scanner=self.scanner, data_dir=CFG.DATA_DIR)
        assert_real_demo_integrity(client, CFG.SHADOW_MODE)
        log_execution_banner(client)
        self._probability_engine_report()
        # Recovery apres crash + broker source de verite
        self.orders.reconcile_startup(self.tlog, self.posmgr)
        self.posmgr.reconcile_startup()
        self.posmgr.reconcile_with_broker()

    def fresh_book(self, ticker: str):
        """Relecture du carnet JUSTE avant decision puis avant ordre."""
        m = self.client.get_market(ticker) or {}
        return m, MarketValidator.normalize_book(m)

    def _shadow_observer(self, snapshot, book, dec):
        """Journalise CHAQUE candidat BTC evalue (accepte ou rejete) dans le
        shadow store — base de la calibration et du backtest."""
        try:
            if not (dec.strategy or "").startswith("btc15m"):
                return
            mo = getattr(dec, "model_output", None) or {}
            feats = mo.get("features", {})
            self.shadow_store.record(
                ticker=dec.ticker, cycle_ts_iso=now_iso(),
                market=snapshot.raw_market or {},
                strike=feats.get("strike"), spot=feats.get("spot"),
                minutes_remaining=snapshot.minutes_remaining,
                yes_bid=(book or {}).get("yes_bid"),
                yes_ask=(book or {}).get("yes_ask"),
                no_bid=(book or {}).get("no_bid"),
                no_ask=(book or {}).get("no_ask"),
                spread=(book or {}).get("spread"),
                ranker_score=getattr(getattr(snapshot, "quality", None),
                                     "total_score", None),
                features=feats,
                probability_yes=mo.get("probability_yes"),
                probability_no=mo.get("probability_no"),
                confidence=mo.get("confidence"),
                estimated_fee=dec.estimated_fees,
                estimated_slippage=dec.expected_slippage,
                gross_edge=dec.gross_edge, net_edge=dec.net_edge,
                net_ev=dec.net_ev,
                shadow_decision=(dec.side if dec.accepted else "none"),
                decision_reason=(dec.rejection_reason or "accepted"))
        except Exception as e:
            log.warning(f"[SHADOW] enregistrement: {e}")

    def _balance_gate(self):
        """Solde reel a chaque cycle. effective_capital = min(plafond,
        solde broker). Prod sans solde = blocage ; demo : secours possible
        via ALLOW_FALLBACK_CAPITAL=1, clairement journalise."""
        bal = self.client.get_balance()
        self.last_balance = bal
        if bal is not None:
            self.capital = min(self.configured_capital, bal) \
                if self.configured_capital else bal
            self.risk.capital = self.capital
            return True, f"solde={bal:.2f}$ capital_effectif={self.capital:.2f}$"
        if self.client.env != "demo":
            return False, "solde broker INDISPONIBLE en production -- aucun trade"
        if CFG.ALLOW_FALLBACK_CAPITAL:
            log_rsk.warning(f"DEMO: solde indisponible, capital de secours "
                            f"{self.configured_capital:.2f}$ (ALLOW_FALLBACK_CAPITAL=1)")
            self.capital = self.configured_capital
            return True, "capital de secours (demo, journalise)"
        return False, ("solde indisponible; en demo, ALLOW_FALLBACK_CAPITAL=1 "
                       "requis pour un secours explicite")

    def _probability_engine_report(self):
        """Audit du Probability Engine au demarrage : pour chaque strategie,
        sa source de probabilite ; puis une sonde REELLE du fournisseur BTC
        (valid/reason/spot/qualite/sources) pour reveler immediatement dans
        les logs Railway pourquoi un modele ne produirait rien.
        PROBE_PROVIDERS_ON_START=0 pour desactiver (tests hors-ligne)."""
        log.info("[PROBABILITY_ENGINE]")
        for mt, st in sorted(self.router._by_type.items()):
            src = "OUI" if st.has_probability_source() else "NON"
            log.info(f"  {mt:24} strategie={st.name:24} "
                     f"source_probabilite={src} ({st.provider_desc()})")
        if os.getenv("PROBE_PROVIDERS_ON_START", "1") != "1":
            return
        if not BTC_AVAILABLE:
            log.warning("  sonde BTC: btc_context ABSENT (module non "
                        "importable) -> tout candidat crypto sera rejete "
                        "no_model_probability:btc_context_absent")
            return
        try:
            ctx = get_btc_context()
            n_src = getattr(ctx, "n_valid_sources", "?")
            log.info(f"  sonde BTC: valid={getattr(ctx, 'valid', False)} "
                     f"reason='{getattr(ctx, 'reason', '')}' "
                     f"spot={getattr(ctx, 'spot', None)} "
                     f"sources_valides={n_src} "
                     f"qualite={getattr(ctx, 'data_quality_score', None)} "
                     f"vol_1m={getattr(ctx, 'realized_vol_1m', None)} "
                     f"flags={getattr(ctx, 'quality_flags', [])}")
            if not getattr(ctx, "valid", False):
                log.warning("  sonde BTC INVALIDE: chaque marche BTC sera "
                            "rejete no_model_probability tant que le "
                            "fournisseur de donnees ne repond pas — "
                            "verifier l'acces sortant de Railway vers "
                            "les APIs spot (Coinbase/Kraken/Bitstamp) et "
                            "klines (Binance).")
        except Exception as e:                              # noqa: BLE001
            log.warning(f"  sonde BTC: EXCEPTION {type(e).__name__}: {e}")

    def cycle(self, n: int) -> int:
        log.info(f"── CYCLE #{n} ─────────────────────────────────────────────")
        self.stats.maybe_daily_report()

        # 0) Reglement des predictions shadow (journal de recherche)
        try:
            n_shadow = self.shadow_store.settle_pending(self.client.get_market)
            self.shadow_settled_total = len(self.shadow_store.settled())
            if n_shadow:
                log.info(f"[SHADOW] {n_shadow} prediction(s) reglee(s) "
                         f"(total regle: {len(self.shadow_store.settled())})")
        except Exception as e:
            log.warning(f"[SHADOW] reglement: {e}")

        # 1) Reglements d'abord : le PnL realise conditionne les portes
        for _t in self.posmgr.check_settlements():
            log_rsk.info(f"PnL jour realise: {self.risk.daily_realized_pnl():+.2f}$ "
                         f"/ limite -{CFG.MAX_DAILY_LOSS:.2f}$")

        # 2) Kill switch (seule porte qui ne depend pas du capital effectif)
        if CFG.KILL_SWITCH:
            log_rsk.warning("KILL_SWITCH actif -- aucun ordre ce cycle.")
            return 0

        # 3) Solde reel du broker -- CORRECTIF AUDIT : deplace AVANT les
        # portes de risque (stop %, drawdown %, budget ouvert). Avant ce
        # correctif, can_trade()/rolling_drawdown_pct() lisaient
        # self.risk.capital, qui n'etait mis a jour QU'APRES ces controles
        # (par le present appel) : les portes utilisaient donc le capital
        # effectif du cycle PRECEDENT, pas le solde courant. En cas de
        # variation de solde (depot/retrait/PnL importants), les limites
        # de risque en % pouvaient etre evaluees sur un capital perime.
        ok, why = self._balance_gate()
        log_rsk.info(f"[CAPITAL] {why}")
        if not ok:
            return 0

        # 4) Portes de risque globales (dependent desormais du capital a jour)
        ok, why = self.risk.can_trade(cycle_trades=0)
        if not ok:
            log_rsk.warning(f"Trading bloque: {why}")
            self.stats.log_summary(); return 0
        if self.posmgr.open_count() >= CFG.MAX_OPEN_POSITIONS:
            log_rsk.warning(f"MAX_OPEN_POSITIONS={CFG.MAX_OPEN_POSITIONS} atteint.")
            return 0
        dd_pct = self.risk.rolling_drawdown_pct()
        if dd_pct >= CFG.MAX_EQUITY_DRAWDOWN_PCT:
            log_rsk.warning(
                f"Drawdown {dd_pct:.1f}% "
                f"({self.risk.rolling_drawdown():.2f}$) >= "
                f"{CFG.MAX_EQUITY_DRAWDOWN_PCT:g}% -- trading coupe.")
            return 0

        # 5) PIPELINE integre (multi-candidats, jamais bloque sur un ticker)
        res = self.pipeline.run_cycle(
            max_accepted=CFG.MAX_TRADES_CYCLE,
            skip_ticker_fn=(lambda tk: CFG.ONE_TRADE_PER_MKT and
                            (tk in self.posmgr.tickers_open()
                             or self.tlog.has_open_on(tk))))
        report = res["report"]
        placed = 0
        for dec in res["accepted"]:
            if placed >= CFG.MAX_TRADES_CYCLE:
                break
            placed += self._execute_decision(dec, report)
        report["fills"] = placed
        # Tache 8 : pourcentages de conversion recalcules sur les compteurs
        # FINAUX (risk/ordres/fills sont incrementes pendant l'execution).
        conv = report.get("funnel_conversion") or {}
        prev = None
        for name in ("scanned_raw", "open_cached", "liquid", "supported",
                     "model_evaluated", "positive_edge", "positive_net_ev",
                     "risk_passed", "orders_submitted", "fills"):
            n = int(report.get(name) or 0)
            conv[name] = {"n": n,
                          "pct_of_prev": round(100.0 * n / prev, 2)
                          if prev else (100.0 if n else 0.0)}
            prev = n if n else prev
        report["funnel_conversion"] = conv
        report["fills_confirmed"] = placed
        report["orders"] = report.get("orders_submitted", 0)
        # UN SEUL resume structure par cycle (exigence G). Les details par
        # decision sont dans decisions.jsonl (rotatif), pas ici.
        summary = {k: report.get(k) for k in
                   ("cycle_id", "scanned_raw", "open_cached", "liquid",
                    "supported", "model_evaluated", "positive_edge",
                    "positive_net_ev", "risk_passed", "orders_submitted",
                    "fills", "rejections_by_reason", "cycle_duration_ms")}
        summary["cycle"] = n
        import json as _json
        log.info(f"[CYCLE-SUMMARY] {_json.dumps(summary, ensure_ascii=False)}")
        JsonStore.save(_p("cycle_report.json"), {"cycle": n, **report})
        # Etat dashboard : UNIQUEMENT des donnees reelles du cycle (les
        # champs non disponibles restent absents -- l'UI affiche « — »).
        try:
            cands = []
            for d in (res.get("accepted") or []):
                cands.append({
                    "ticker": getattr(d, "ticker", None),
                    "strategy": getattr(d, "strategy", None),
                    "side": getattr(d, "side", None),
                    "model_probability": getattr(d, "model_probability", None),
                    "market_probability": getattr(d, "market_probability", None),
                    "edge_net": getattr(d, "edge_net", None),
                    "ev_net": getattr(d, "ev_net", None),
                    "confidence": getattr(d, "confidence", None),
                    "status": "submitted" if placed else "evalue",
                })
            JsonStore.save(_p("dashboard_state.json"), {
                "ts": now_iso(), "version": ENGINE_VERSION,
                "env": getattr(self.client, "env", "demo"),
                "cycle": n, "balance": getattr(self, "last_balance", None),
                "capital": self.capital,
                "configured_capital": self.configured_capital,
                "shadow_settled": getattr(self, "shadow_settled_total", None),
                "exchange_paused": time.time() <
                getattr(self.orders, "exchange_pause_until", 0.0),
                "candidates": cands,
            })
        except Exception as e:
            log.warning(f"dashboard_state: {e}")
        JsonStore.save(_p("pipeline_stats.json"), {
            "cycle": n,
            "scanned": report["scanned"],
            "valid": report.get("scanner_included"),
            "eligible": report["ranker_eligible"],
            "strategy_supported": report.get("strategy_supported"),
            "model_probability": report.get("model_probability"),
            "positive_edge": report.get("positive_edge"),
            "positive_net_ev": report.get("positive_net_ev"),
            "risk_passed": report.get("risk_passed", 0),
            "accepted": report["accepted"],
            "orders": report.get("orders_submitted", 0),
        })
        JsonStore.save(_p("reject_reasons.json"),
                       {"cycle": n, "reject_reasons": report["rejections"]})
        if placed == 0:
            self.stats.log_summary()
        return placed

    def _execute_decision(self, dec, report) -> int:
        ticker = dec.ticker
        # 5a) carnet FRAIS une DERNIERE fois, juste avant l'ordre (TEST L)
        m, book = self.fresh_book(ticker)
        if not book:
            log.info(f"CARNET DISPARU avant execution sur {ticker} -- annule.")
            report["rejections"]["stale_book"] = \
                report["rejections"].get("stale_book", 0) + 1
            return 0
        ask = book.get("yes_ask") if dec.side == "yes" else book.get("no_ask")
        if ask is None or not (1 <= int(ask) <= 99):
            report["rejections"]["no_executable_ask"] = \
                report["rejections"].get("no_executable_ask", 0) + 1
            return 0
        entry = int(ask)

        # 5b) budgets risque categorie / marche (sur capital effectif)
        cat = getattr(dec, "category", None) or "Other"
        cat_risk = self.posmgr.open_risk_by_category().get(cat, 0.0)
        if cat_risk >= self.capital * CFG.MAX_CATEGORY_RISK_PCT / 100.0:
            report["rejections"]["category_budget"] = \
                report["rejections"].get("category_budget", 0) + 1
            return 0
        if self.posmgr.open_risk_on(ticker) >= \
                self.capital * CFG.MAX_SINGLE_MARKET_RISK_PCT / 100.0:
            report["rejections"]["risk_blocked"] = \
                report["rejections"].get("risk_blocked", 0) + 1
            return 0

        # 5c) taille sur capital EFFECTIF (solde reel plafonne) (TEST K)
        count = PositionSizer.contracts(
            self.capital, entry, dec.taille, dec.confidence,
            self.risk.rolling_drawdown(), self.posmgr.open_risk())
        if count <= 0:
            log_rsk.info(f"[REJECT] {ticker}: risk_blocked (taille=0)")
            report["rejections"]["risk_blocked"] = \
                report["rejections"].get("risk_blocked", 0) + 1
            return 0
        report["risk_passed"] = report.get("risk_passed", 0) + 1
        log_rsk.info(f"[RISK] {ticker}: portes de risque PASSEES "
                     f"(taille={count}, capital={self.capital:.2f}$)")

        est_fee_total = FeeModel.trading_fee(count, entry)
        log_trd.info(f"[SIGNAL VALIDE] {ticker} {dec.side.upper()} x{count} "
                     f"@ {entry}c | modele={dec.model_probability:.1%} "
                     f"marche={dec.market_probability:.1%} "
                     f"edge_net={dec.net_edge:+.3f} ev_net={dec.net_ev:+.3f} "
                     f"strat={dec.strategy}")

        # 5d) SHADOW : decision complete journalisee, AUCUN ordre
        if CFG.SHADOW_MODE:
            log_trd.info("[SHADOW] ordre NON envoye (mode shadow).")
            report["rejections"]["shadow_mode"] = \
                report["rejections"].get("shadow_mode", 0) + 1
            return 0

        # Circuit breaker demi-ouvert : la reservation se fait au dernier
        # moment, apres toutes les autres portes, juste avant la soumission.
        # Ainsi un cycle sans candidat ne consomme jamais l'unique essai.
        claimed, claim_reason = self.risk.claim_half_open_attempt(ticker)
        if not claimed:
            log_rsk.warning(f"[REJECT] {ticker}: {claim_reason}")
            report["rejections"]["half_open_already_claimed"] = (
                report["rejections"].get("half_open_already_claimed", 0) + 1)
            return 0

        report["orders_submitted"] = report.get("orders_submitted", 0) + 1
        log_trd.info(f"[EXECUTION] {ticker} {dec.side.upper()} x{count} "
                     f"@ {entry}c -> envoi de l'ordre")
        exec_res = self.orders.place_and_track(ticker, dec.side, count, entry)
        if exec_res.filled <= 0:
            # Ne pas consumer l'unique essai si aucune soumission n'a ete
            # acceptee, ou si l'annulation sans fill est explicitement confirmee.
            # Un order_id avec etat incertain reste verrouille par prudence.
            if exec_res.order_id is None:
                self.risk.release_half_open_attempt(ticker,
                    f"soumission non acceptee: {exec_res.status}")
            elif exec_res.state == "cancelled" and exec_res.status not in ("unverified", "unknown"):
                self.risk.release_half_open_attempt(ticker,
                    f"ordre confirme sans fill: {exec_res.status}")
            log_trd.warning(f"NON EXECUTE ({exec_res.state}: {exec_res.status}) "
                            f"-- AUCUN trade enregistre.")
            return 0

        # 5e) frais REELS d'abord (reponse d'ordre puis fills) (TEST M)
        try:
            fills = self.client.get_fills(exec_res.order_id) \
                if exec_res.order_id else []
        except KalshiAPIError:
            fills = []
        fee_amt, fee_src = FeeModel.from_api({}, fills,
                                             exec_res.filled, exec_res.avg_price)
        trade = self.tlog.open_trade(
            ticker=ticker, market_title=m.get("title", ""),
            side=dec.side, req_price=entry,
            avg_price=exec_res.avg_price, req_count=count,
            filled_count=exec_res.filled, spread=book["spread"], fees=fee_amt,
            edge=dec.net_edge, ev=dec.net_ev, confidence=dec.confidence,
            grade="B", reason=dec.reason,
            analysis={"market_prob": dec.market_probability,
                      "model_prob": dec.model_probability,
                      "gross_edge": dec.gross_edge,
                      "fee_source": fee_src,
                      "estimated_fee_before_order": est_fee_total,
                      "actual_fee_after_fill": fee_amt,
                      "strategy": dec.strategy},
            order_id=exec_res.order_id, order_status=exec_res.status)
        self.posmgr.open_position(trade, extra={
            "strategy": dec.strategy, "category": cat, "market_score": None,
            "entry_edge": dec.net_edge, "entry_ev": dec.net_ev,
            "fill_ids": [f.get("fill_id") or f.get("id")
                         for f in fills if f.get("fill_id") or f.get("id")]})
        # Exigence 7 : la position n'est declaree OUVERTE qu'apres avoir ete
        # RETROUVEE dans /portfolio/positions. Sinon on le dit honnetement.
        try:
            brk = self.client.get_positions()
        except KalshiAPIError as e:
            brk = None
            log_pos.warning(f"[POSITION_VERIFY] lecture impossible: {e}")
        found = None
        for p in (brk or []):
            if str(pick(p, "ticker", "market_ticker", default="")) == dec.ticker:
                found = p
                break
        if found is not None:
            log_pos.info("[POSITION_VERIFY] "
                         f"ticker={dec.ticker} position_found=true "
                         f"net_position={pick_int(found, 'position', 'total_traded', default=exec_res.filled)} "
                         f"market_exposure={found.get('market_exposure', '-')} "
                         f"realized_pnl={found.get('realized_pnl', '-')} "
                         f"fees_paid={found.get('fees_paid', '-')}")
            log_pos.info(f"[POSITION_OPENED] {dec.ticker} confirme par l'API")
        elif brk is not None:
            log_pos.warning("[POSITION_VERIFY] "
                            f"ticker={dec.ticker} position_found=false — "
                            "fill confirme par /fills mais position pas "
                            "encore visible dans /positions (position "
                            "enregistree localement, statut POSITION_OPENED "
                            "NON emis)")
        snap = self.risk.snapshot()
        log_rsk.info(f"risque_ouvert={snap['open_risk']}$ "
                     f"pnl_jour={snap['daily_realized_pnl']}$ "
                     f"frais_cumules={snap['fees_paid']}$ (source={fee_src})")
        return 1

# ══════════════════════════════════════════════════════════════════════════
# S16. CLI / BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════════════════════════

def banner(client: KalshiClient, capital: float):
    bal = client.get_balance()
    log.info("=" * 62)
    log.info("  KALSHI ALPHA ENGINE  v11 PRO")
    log.info("=" * 62)
    log.info(f"Version       : {ENGINE_VERSION} | btc_context={BTC_CTX_VERSION}")
    log.info(f"Environnement : {client.env.upper()} -> {client.base_url}")
    log.info(f"               ORDRES REELS -- aucun dry-run, aucune simulation")
    log.info(f"Identifiants  : {client.cred_src}")
    log.info(f"Solde compte  : {f'{bal:,.2f}$' if bal is not None else 'indisponible (verifier cles/env)'}")
    eff = min(capital, bal) if bal is not None else capital
    log.info(f"Capital       : plafond configure {capital:,.2f}$ | "
             f"EFFECTIF (sizing/risque) = min(plafond, solde) = {eff:,.2f}$")
    stop = min(CFG.MAX_DAILY_LOSS, eff * CFG.MAX_DAILY_LOSS_PCT / 100.0)
    log.info(f"Risque        : stop jour -{stop:.2f}$ "
             f"({CFG.MAX_DAILY_LOSS_PCT:g}% du capital effectif "
             f"{eff:.2f}$, plafond {CFG.MAX_DAILY_LOSS:.2f}$) | "
             f"max {CFG.MAX_POS_PCT:g}%/position | budget ouvert "
             f"{CFG.RISK_BUDGET_PCT:g}% | arret apres "
             f"{CFG.MAX_CONSECUTIVE_LOSSES} pertes consecutives | "
             f"{CFG.MAX_OPEN_POSITIONS} positions max")
    log.info(f"Protections   : entree<={CFG.MAX_ENTRY_CENTS}c | "
             f"1 trade/marche={'OUI' if CFG.ONE_TRADE_PER_MKT else 'NON'} | "
             f"min {CFG.MIN_MINUTES:g}min | TTL ordre {CFG.ORDER_TTL_SECONDS}s")
    log.info("=" * 62)

def _start_dashboard_if_enabled():
    """Dashboard web en thread daemon. Zero dependance, lecture seule des
    fichiers d'etat -- ne peut ni bloquer ni influencer le trading."""
    if not CFG.DASHBOARD_ENABLED:
        return None
    try:
        from dashboard_web import start_dashboard
        srv = start_dashboard(CFG.DATA_DIR, CFG.DASHBOARD_PORT)
        log.info(f"[DASHBOARD] http://0.0.0.0:{CFG.DASHBOARD_PORT} "
                 f"(donnees: {CFG.DATA_DIR}) -- donnees REELLES uniquement, "
                 f"low_sample affiche tant que n<30 trades regles.")
        return srv
    except Exception as e:
        log.warning(f"[DASHBOARD] non demarre: {e} "
                    "(le trading continue normalement)")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="environnement Kalshi DEMO (ordres reels sur demo-api)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--scan-only", action="store_true",
                    help="scanner d'univers de marches: authentifie, scanne, "
                         "ecrit market_universe.json + market_scanner_report.json, "
                         "NE PASSE AUCUN ORDRE, puis quitte (code 0)")
    ap.add_argument("--rank-only", action="store_true",
                    help="scanner + ranker: scanne, met a jour l'historique, "
                         "classe par qualite d'execution, ecrit "
                         "market_rankings.json + market_ranker_report.json, "
                         "NE PASSE AUCUN ORDRE, puis quitte (code 0)")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--capital", type=float, default=_env_f("CAPITAL", 500.0),
                    help="PLAFOND de capital; le solde broker reel prime "
                         "s'il est inferieur")
    ap.add_argument("--shadow", action="store_true",
                    help="mode shadow: pipeline et decisions complets, "
                         "AUCUN ordre envoye")
    ap.add_argument("--stats", action="store_true",
                    help="affiche les statistiques de performance et sort")
    args = ap.parse_args()
    if args.stats:
        from performance import load_report, format_report
        print(format_report(load_report(CFG.DATA_DIR, capital=args.capital)))
        return
    if args.shadow:
        CFG.SHADOW_MODE = True

    env = "demo" if (args.demo or os.getenv("DEMO_TRADING", "") == "1") \
        else "prod"
    if env == "prod" and not (args.scan_only or args.rank_only):
        # DOUBLE confirmation exigee pour l'argent reel.
        if os.getenv("KALSHI_ENV_CONFIRM", "") != "LIVE":
            log.error("PRODUCTION demandee sans KALSHI_ENV_CONFIRM=LIVE. Arret.")
            sys.exit(1)
        if os.getenv("LIVE_TRADING_CONFIRMED", "") != "YES" \
                and not CFG.SHADOW_MODE:
            log.error("PRODUCTION demandee sans LIVE_TRADING_CONFIRMED=YES. "
                      "Definir les DEUX variables, ou utiliser --shadow. Arret.")
            sys.exit(1)
        if os.getenv("LIVE_TRADING", "") != "1":
            log.error("PRODUCTION: LIVE_TRADING=1 requis (interdit par defaut). Arret.")
            sys.exit(1)
        # GATEKEEPER : le live reste bloque sans validation modele recente.
        from model_gatekeeper import check_live_allowed
        ok_gate, failed = check_live_allowed()
        if not ok_gate:
            log.error("GATEKEEPER: live REFUSE. Criteres echoues:")
            for f in failed:
                log.error(f"  - {f}")
            sys.exit(1)
        log.warning("PRODUCTION REAL MONEY ENABLED (double confirmation + "
                    "gatekeeper valides).")

    client = KalshiClient(env)
    banner(client, args.capital)
    _start_dashboard_if_enabled()

    if args.scan_only or args.rank_only:
        # Modes analyse : jamais d'ExecutionEngine, donc aucun chemin vers
        # l'envoi d'ordre. Ecrit les rapports et sort proprement.
        if args.rank_only:
            from market_ranker import run_ranking
            res = run_ranking(client)
            rep = res["report"]
            log.info(f"RANKING TERMINE: {rep['markets_scored']} marches "
                     f"scores | eligibles={rep['eligible']} "
                     f"| exclusions={rep['excluded_by_reason']}")
        else:
            from market_scanner import run_scan
            res = run_scan(client)
            rep = res["report"]
            log.info(f"SCAN TERMINE: {rep['total_markets_received']} marches "
                     f"({rep['api_pages']} pages) | valides={rep['valid_markets']} "
                     f"| exclusions={rep['excluded_by_reason']}")
        sys.exit(0)

    if not BTC_AVAILABLE:
        log.error("btc_context.py manquant -- arret."); sys.exit(1)

    engine = ExecutionEngine(client, args.capital)
    n = 0
    while True:
        n += 1
        try:
            engine.cycle(n)
        except KalshiAPIError as e:
            log.error(f"Cycle #{n}: erreur API non recuperee: {e}")
        except Exception as e:
            log.exception(f"Cycle #{n}: erreur inattendue: {e}")
        if not args.loop:
            break
        time.sleep(max(5, args.interval))

if __name__ == "__main__":
    main()
