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


# ══════════════════════════════════════════════════════════════════════════
# S1. CONFIGURATION (centralisee, surchargeables par variables d'env)
# ══════════════════════════════════════════════════════════════════════════

from config import Config, CFG, _env_b, _env_i, _env_f, _p

# ══════════════════════════════════════════════════════════════════════════
# S2. LOGGING (canaux BOT / API / TRADE / RISK / POSITION / STATS)
# ══════════════════════════════════════════════════════════════════════════

from logging_config import setup_logging
setup_logging()   # JSON-lines sur stdout (LOG_FORMAT=json) ou texte lisible
                  # (LOG_FORMAT=text) ; niveau via LOG_LEVEL (defaut INFO).
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

from position_sizer import PositionSizer
# ══════════════════════════════════════════════════════════════════════════
# S13. VALIDATEURS (marche + signal)
# ══════════════════════════════════════════════════════════════════════════

from market_validator import MarketValidator
from signal_validator import SignalValidator

# ══════════════════════════════════════════════════════════════════════════
# S14. STRATEGIE BTC (btc_context inchange -- edge HONNETE = 0)
# ══════════════════════════════════════════════════════════════════════════

from btc_strategy import (BtcStrategy, BTC_AVAILABLE, BTC_CTX_VERSION,
                           evaluate_btc_trade, get_btc_context, get_btc_price)

# ══════════════════════════════════════════════════════════════════════════
# S15. MOTEUR D'EXECUTION
# ══════════════════════════════════════════════════════════════════════════

from execution_engine import (ExecutionEngine, ENGINE_VERSION,
                              _client_is_genuine, assert_real_demo_integrity,
                              log_execution_banner)

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
