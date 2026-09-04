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

import config
from config import (Config, CFG, _env_b, _env_i, _env_f, _p,
                    prod_credentials_config)

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
    # MODES D'ACCES PRODUCTION, mutuellement exclusifs. Volontairement des
    # drapeaux DISTINCTS de --capital, qui est un MONTANT de dimensionnement
    # et non un mode : surcharger --capital en mode aurait rendu ambigu un
    # argument existant, exactement le genre d'ambiguite que ce travail
    # supprime.
    ap.add_argument("--live-read-only", action="store_true",
                    help="PRODUCTION en LECTURE SEULE: observation et shadow "
                         "complet sur donnees reelles, aucune mutation broker "
                         "possible. N'implique NI approbation modele, NI "
                         "autorisation d'ecriture, NI soumission d'ordres.")
    ap.add_argument("--live-capital", action="store_true",
                    help="PRODUCTION en mode CAPITAL. N'AUTORISE rien par "
                         "lui-meme: toutes les portes existantes restent "
                         "requises (gatekeeper modele, autorisation "
                         "d'ecriture, ALLOW_ORDER_SUBMISSION, coupe-circuit, "
                         "quarantaine quotidienne, risque, anti-doublon).")
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

    # Restauration zero-perte AVANT toute lecture d'etat (Phase 2B): si le
    # volume est vierge et que l'operateur fournit le backup golden en env,
    # les cinq fichiers critiques sont recrees octet pour octet. Une
    # restauration demandee mais impossible ARRETE le processus ici meme:
    # continuer contaminerait la destination vierge (risk_state.json de
    # demarrage) et bloquerait definitivement le restore never-overwrite.
    from state_restore import restore_or_die
    restore_or_die()

    # ── Modes d'acces production : un seul, jamais deux ─────────────────
    if args.live_read_only and args.live_capital:
        log.error("--live-read-only et --live-capital sont exclusifs. Arret.")
        sys.exit(1)
    if (args.live_read_only or args.live_capital) and args.demo:
        log.error("--demo est incompatible avec un mode d'acces PRODUCTION. "
                  "Arret.")
        sys.exit(1)
    if args.live_read_only:
        os.environ["PROD_ACCESS_MODE"] = config.PROD_READ_ONLY
    elif args.live_capital:
        os.environ["PROD_ACCESS_MODE"] = config.PROD_CAPITAL

    env = "demo" if (args.demo or os.getenv("DEMO_TRADING", "") == "1") \
        else "prod"
    if env == "prod" and not (args.scan_only or args.rank_only):
        mode = config.prod_access_mode()
        # CHOIX A : une valeur absente, vide, mal orthographiee ou inconnue
        # ARRETE le processus. Un processus qui ne demarre pas ne peut pas
        # muter un compte, et la preuve tient en ce seul point de controle.
        # (La formulation inverse de `prod_is_read_only` couvre en plus tout
        # chemin qui atteindrait le client sans passer par ici.)
        if mode is None:
            raw = os.getenv("PROD_ACCESS_MODE")
            log.error(
                f"PRODUCTION demandee avec PROD_ACCESS_MODE={raw!r}: valeur "
                f"absente ou non reconnue. Valeurs admises: "
                f"{'/'.join(config.PROD_ACCESS_MODES)}. Une valeur illisible "
                f"n'est JAMAIS interpretee comme CAPITAL. Arret.")
            sys.exit(1)
        # Confirmation d'INTENTION de production. Exigee dans les deux modes:
        # atteindre le compte reel, meme en lecture, doit etre delibere.
        if os.getenv("KALSHI_ENV_CONFIRM", "") != "LIVE":
            log.error("PRODUCTION demandee sans KALSHI_ENV_CONFIRM=LIVE. Arret.")
            sys.exit(1)

        if mode == config.PROD_CAPITAL:
            # DOUBLE confirmation exigee pour l'argent reel.
            if os.getenv("LIVE_TRADING_CONFIRMED", "") != "YES" \
                    and not CFG.SHADOW_MODE:
                log.error("PRODUCTION CAPITAL sans LIVE_TRADING_CONFIRMED=YES. "
                          "Definir les DEUX variables, ou utiliser --shadow. "
                          "Arret.")
                sys.exit(1)
            if os.getenv("LIVE_TRADING", "") != "1":
                log.error("PRODUCTION CAPITAL: LIVE_TRADING=1 requis "
                          "(interdit par defaut). Arret.")
                sys.exit(1)
            # GATEKEEPER : le live reste bloque sans validation modele recente.
            # Les criteres scientifiques eux-memes sont INCHANGES.
            from model_gatekeeper import check_live_allowed
            ok_gate, failed = check_live_allowed()
            if not ok_gate:
                log.error("GATEKEEPER: live REFUSE. Criteres echoues:")
                for f in failed:
                    log.error(f"  - {f}")
                sys.exit(1)
            log.warning("PRODUCTION REAL MONEY ENABLED (double confirmation + "
                        "gatekeeper valides).")
        else:
            # LECTURE SEULE. Le gatekeeper modele n'est PAS consulte, et
            # c'est le coeur de ce changement : `check_live_allowed` traitait
            # l'ACCES aux donnees de production comme equivalent a
            # l'AUTORISATION d'engager du capital de production. Observer un
            # marche n'exige aucune preuve d'edge; engager de l'argent, si.
            # Confondre les deux forcait a approuver le modele pour pouvoir
            # seulement REGARDER -- la pire raison possible d'approuver un
            # modele.
            log.warning(
                "PRODUCTION EN LECTURE SEULE (PROD_ACCESS_MODE=READ_ONLY): "
                "observation et decisions completes sur donnees reelles. "
                "AUCUNE mutation broker n'est possible, quels que soient les "
                "drapeaux de trading. Le gatekeeper modele n'est pas requis "
                "pour observer.")

    # INVARIANT DUR : en PRODUCTION, des identifiants absents ou
    # illisibles ARRETENT le processus AVANT toute construction de client,
    # tout manager, toute reconciliation, tout scan et tout appel broker.
    # Sans ce garde, _load_key se contentait d'un avertissement et
    # _sign_headers omettait la signature : le moteur demarrait et
    # interrogeait le broker en NON AUTHENTIFIE (401 silencieux a chaque
    # cycle). Le mode DEMO refusait deja de demarrer sans ses cles ; la
    # PRODUCTION, ou l'argent est reel, ne peut pas etre plus permissive.
    if env == "prod":
        creds_ok, creds_err = prod_credentials_config()
        if not creds_ok:
            log.critical(
                f"[FATAL] PRODUCTION: identifiants invalides -- {creds_err}. "
                f"ARRET avant toute initialisation: aucun client, aucun "
                f"manager, aucune reconciliation, aucun scan, AUCUN appel "
                f"broker. Remede: fournir KALSHI_KEY_ID et "
                f"KALSHI_PRIVATE_KEY (PEM RSA complet) puis redemarrer.")
            sys.exit(1)

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
