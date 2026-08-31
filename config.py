"""Centralized environment-backed configuration for the trading engine."""

import os

def _env_f(name, default): 
    try: return float(os.getenv(name, str(default)))
    except ValueError: return default
def _env_i(name, default):
    try: return int(os.getenv(name, str(default)))
    except ValueError: return default
def _env_b(name, *, default):
    return os.getenv(name, "1" if default else "0").strip().lower() not in ("0","false","no","non")

class Config:
    # Environnements
    PROD_URL  = "https://api.elections.kalshi.com/trade-api/v2"
    DEMO_URL  = "https://demo-api.kalshi.co/trade-api/v2"
    # Identifiants (prod) et identifiants demo distincts si fournis
    KEY_ID        = os.getenv("KALSHI_KEY_ID", "")
    PRIV_KEY      = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
    DEMO_KEY_ID   = os.getenv("KALSHI_DEMO_KEY_ID", "")
    DEMO_PRIV_KEY = os.getenv("KALSHI_DEMO_PRIVATE_KEY", "").replace("\\n", "\n")
    # Strategie / protections
    SERIES            = os.getenv("BTC_SERIES", "KXBTC15M")
    MAX_ENTRY_CENTS   = _env_i("MAX_ENTRY_CENTS", 85)
    ONE_TRADE_PER_MKT = _env_b("ONE_TRADE_PER_MARKET", default=True)
    MIN_MINUTES       = _env_f("BTC_MIN_MINUTES", 5.0)
    MAX_SPREAD_PAY    = _env_i("MAX_SPREAD_PAY", 5)
    # Risque — TOUTES les limites sont recalculees sur le CAPITAL EFFECTIF
    # (= min(solde broker, plafond configure)) a chaque cycle. Une valeur $
    # fixe fondee sur un capital de reference superieur au solde est
    # INTERDITE (bug corrige : stop -50$ sur un compte de 93,26$ = 54 %).
    MAX_DAILY_LOSS    = _env_f("MAX_DAILY_LOSS", 50.0)      # $ plafond ABSOLU
    MAX_DAILY_LOSS_PCT = _env_f("MAX_DAILY_LOSS_PCT", 5.0)  # % du capital effectif
    MAX_CONSECUTIVE_LOSSES = _env_i("MAX_CONSECUTIVE_LOSSES", 3)
    # CORRECTIF AUDIT 2026-07-28 : sans cooldown, le kill-switch ci-dessus
    # est un blocage PERMANENT (aucun trade possible => aucun moyen
    # d'obtenir le trade gagnant qui romprait la serie). Passe ce delai
    # apres le dernier trade REGLE, un seul nouvel essai est autorise
    # (circuit-breaker "demi-ouvert") ; s'il perd aussi, un nouveau
    # cooldown redemarre a partir de son propre settled_at. L'historique
    # de pertes n'est jamais efface silencieusement.
    CONSECUTIVE_LOSS_COOLDOWN_S = _env_f(
        "CONSECUTIVE_LOSS_COOLDOWN_SECONDS", 3600.0)      # 1h par defaut
    MAX_TRADES_CYCLE  = _env_i("MAX_TRADES_CYCLE", 3)
    MAX_POS_PCT       = _env_f("MAX_POSITION_PCT", 1.0)     # % capital / position (plafond dur)
    # Adaptive Kelly sizing is opt-in to preserve the legacy 1% behavior.
    KELLY_ENABLED     = _env_b("KELLY_ENABLED", default=False)
    KELLY_FRACTION    = _env_f("KELLY_FRACTION", 0.5)
    KELLY_MAX_POS_PCT = _env_f("KELLY_MAX_POSITION_PCT", 10.0)
    KELLY_MIN_BET     = _env_f("KELLY_MIN_BET", 1.0)
    RISK_BUDGET_PCT   = _env_f("RISK_BUDGET_PCT", 5.0)      # % capital en risque ouvert total
    DD_THROTTLE_PCT   = _env_f("DD_THROTTLE_PCT", 10.0)     # au-dela: taille /2
    MAX_OPEN_POSITIONS      = _env_i("MAX_OPEN_POSITIONS", 3)
    MAX_POSITION_AGE_DAYS   = _env_i("MAX_POSITION_AGE_DAYS", 30)
    # Plafond DUR de contrats par ordre, independant de tout sizing en % :
    # clampe apres PositionSizer ET re-verifie juste avant le POST broker
    # (defense en profondeur). Validation stricte via contract_cap_config():
    # une valeur invalide, nulle, negative ou deraisonnable DESACTIVE les
    # soumissions fail-closed — une erreur de configuration operateur n'est
    # JAMAIS reinterpretee silencieusement. Valeur absente : defaut sur
    # documente (CONTRACT_CAP_DEFAULT) hors config LIVE-capable ; une config
    # LIVE-capable (REQUIRE_PERSISTENT_STATE=true) EXIGE une valeur
    # explicite. Canary LIVE prevu: MAX_CONTRACTS_PER_ORDER=1.
    MAX_CONTRACTS_PER_ORDER = os.getenv("MAX_CONTRACTS_PER_ORDER")
    # Reconciliation broker PERIODIQUE (l'appel au demarrage ne suffit pas :
    # 15 jours d'uptime = 15 jours sans verification). 0 desactive le
    # passage periodique (comportement historique, tests uniquement).
    RECONCILE_INTERVAL_S    = _env_f("RECONCILE_INTERVAL_SECONDS", 900.0)
    # Deploiement LIVE-capable : exiger la continuite de l'etat persistant.
    # Un marqueur state_epoch.json absent (disque neuf ou EFFACE) bloque
    # les soumissions fail-closed au lieu de reprendre comme si de rien
    # n'etait. ALLOW_FRESH_STATE=true est l'acquittement operateur explicite
    # d'un repertoire d'etat volontairement vide (premier montage du
    # volume). Les DEUX sont off par defaut: zero changement en DEMO.
    REQUIRE_PERSISTENT_STATE = _env_b("REQUIRE_PERSISTENT_STATE", default=False)
    ALLOW_FRESH_STATE        = _env_b("ALLOW_FRESH_STATE", default=False)
    # Portfolio controls are opt-in (0 disables each percentage cap).
    MAX_CORRELATION_GROUP_PCT = _env_f("MAX_CORRELATION_GROUP_PCT", 0.0)
    MAX_PORTFOLIO_RISK_PCT = _env_f("MAX_PORTFOLIO_RISK_PCT", 0.0)
    PORTFOLIO_DRAWDOWN_THROTTLE_PCT = _env_f("PORTFOLIO_DRAWDOWN_THROTTLE_PCT", 0.0)
    DRAWDOWN_THROTTLE_FACTOR = _env_f("DRAWDOWN_THROTTLE_FACTOR", 0.5)
    # ── Performance (P8) : parallelisme + caches, TOUS DESACTIVES par
    # ── defaut — sans flag, le moteur est strictement sequentiel et sans
    # ── cache (zero changement de comportement).
    API_CACHE_ENABLED     = _env_b("API_CACHE_ENABLED", default=False)
    API_BALANCE_TTL_S     = _env_f("API_BALANCE_TTL_S", 15.0)   # cache solde
    API_MARKET_TTL_S      = _env_f("API_MARKET_TTL_S", 30.0)    # cache listings
    API_PARALLEL_ENABLED  = _env_b("API_PARALLEL_ENABLED", default=False)
    API_PARALLEL_WORKERS  = max(1, _env_i("API_PARALLEL_WORKERS", 4))
    SCANNER_PARALLEL_SERIES = _env_b("SCANNER_PARALLEL_SERIES", default=False)
    SCANNER_PARALLEL_WORKERS = max(1, _env_i("SCANNER_PARALLEL_WORKERS", 4))
    # Contexte BTC calcule UNE FOIS par cycle (au lieu d'un refresh reseau
    # toutes les ~10 s) — voir btc_context.begin_cycle().
    BTC_CONTEXT_CYCLE_CACHE = _env_b("BTC_CONTEXT_CYCLE_CACHE", default=False)
    BTC_CONTEXT_CYCLE_TTL_S = _env_f("BTC_CONTEXT_CYCLE_TTL_S", 3600.0)
    # Mode d'execution explicite (exigence 2). "real_demo" active le
    # garde anti-mock : tout client non authentique => arret FATAL.
    EXECUTION_MODE    = os.getenv("EXECUTION_MODE", "standard").lower()
    DRY_RUN           = _env_b("DRY_RUN", default=False)
    ALLOW_ORDER_SUBMISSION = _env_b("ALLOW_ORDER_SUBMISSION", default=True)
    ORDER_VERIFY_INTERVAL_S = _env_f("ORDER_VERIFY_INTERVAL_SECONDS", 3.0)
    # Verrou anti-doublon de session (s) : re-soumettre le meme ticker est
    # refuse pendant ce delai meme sans trade local (defense contre toute
    # panne de verification). 0 = desactive (tests uniquement).
    SUBMIT_DEDUP_TTL_S = _env_f("SUBMIT_DEDUP_TTL_SECONDS", 6 * 3600.0)
    EXCHANGE_503_COOLDOWN_S = _env_f("EXCHANGE_503_COOLDOWN_SECONDS", 300.0)
    # Dashboard web (lecture seule des fichiers d'etat, zero dependance).
    DASHBOARD_ENABLED = _env_b("DASHBOARD_ENABLED", default=True)
    DASHBOARD_PORT = int(os.environ.get("PORT",
                         os.environ.get("DASHBOARD_PORT", "8080")))
    CANCEL_UNFILLED_ORDERS  = _env_b("CANCEL_UNFILLED_ORDERS", default=True)
    MAX_CATEGORY_RISK_PCT   = _env_f("MAX_CATEGORY_RISK_PCT", 3.0)
    MAX_SINGLE_MARKET_RISK_PCT = _env_f("MAX_SINGLE_MARKET_RISK_PCT", 1.0)
    MAX_EQUITY_DRAWDOWN_PCT = _env_f("MAX_EQUITY_DRAWDOWN_PCT", 20.0)
    # Portes edge/EV du pipeline (voir strategy_router.GateConfig)
    MIN_MODEL_CONFIDENCE  = _env_i("MIN_MODEL_CONFIDENCE", 6)
    MIN_GROSS_EDGE        = _env_f("MIN_GROSS_EDGE", 0.05)
    MIN_NET_EDGE          = _env_f("MIN_NET_EDGE", 0.03)
    MIN_NET_EV            = _env_f("MIN_NET_EV", 0.02)
    MAX_ACCEPTABLE_SPREAD = _env_i("MAX_ACCEPTABLE_SPREAD", 4)
    MIN_MARKET_SCORE      = _env_f("MIN_MARKET_SCORE", 50.0)
    MIN_FILL_PROXY        = _env_f("MIN_FILL_PROXY", 40.0)
    SLIPPAGE_BUFFER_CENTS = _env_i("SLIPPAGE_BUFFER_CENTS", 1)
    # Solde / modes
    ALLOW_FALLBACK_CAPITAL = _env_b("ALLOW_FALLBACK_CAPITAL", default=False)
    SHADOW_MODE            = _env_b("SHADOW_MODE", default=False)   # decide, n'envoie pas
    KILL_SWITCH            = _env_b("KILL_SWITCH", default=False)   # coupe tout ordre
    # Ordres
    ORDER_TTL_SECONDS = _env_i("ORDER_FILL_TIMEOUT_SECONDS",
                               _env_i("ORDER_TTL_SECONDS", 45))
    ORDER_POLL_START  = 1.0
    ORDER_POLL_MAX    = 5.0
    # Frais (A VERIFIER contre le bareme officiel Kalshi)
    FEE_RATE          = _env_f("KALSHI_FEE_RATE_TRADING", 0.07)
    # Fichiers
    DATA_DIR    = os.getenv("DATA_DIR", ".")
    TRADES_FILE     = "kalshi_trades.json"
    POSITIONS_FILE  = "positions_state.json"
    ORDERS_FILE     = "orders_state.json"
    RISK_FILE       = "risk_state.json"
    CURVE_FILE      = "capital_curve.json"
    REPORT_DIR      = "reports"
    BACKUPS         = 3

CFG = Config()

def _p(name: str) -> str:
    return os.path.join(CFG.DATA_DIR, name)


#: Defaut documente du plafond de contrats, valable UNIQUEMENT hors
#: configuration LIVE-capable (une config LIVE-capable exige une valeur
#: explicite).
CONTRACT_CAP_DEFAULT = 100
#: Au-dela, la valeur est jugee deraisonnable et REJETEE par la validation
#: (protection contre une faute de frappe type "10000").
CONTRACT_CAP_MAX = 1000


def contract_cap_config(live_capable=None):
    """Validate MAX_CONTRACTS_PER_ORDER. Returns (cap, error).

    cap is a positive int when the configuration is usable; cap is None
    when order submission must be DISABLED fail-closed, with `error`
    naming the operator-visible reason. An invalid operator configuration
    is never silently reinterpreted (never collapsed to 1, never to a
    default).

      valid positive int            -> (int, None)
      missing, non-LIVE-capable     -> (CONTRACT_CAP_DEFAULT, None)
      missing, LIVE-capable         -> (None, "...explicit value required")
      invalid string                -> (None, "...")
      zero / negative               -> (None, "...")
      > CONTRACT_CAP_MAX            -> (None, "...")
    """
    if live_capable is None:
        live_capable = bool(CFG.REQUIRE_PERSISTENT_STATE)
    raw = CFG.MAX_CONTRACTS_PER_ORDER
    if raw is None or str(raw).strip() == "":
        if live_capable:
            return None, ("MAX_CONTRACTS_PER_ORDER absent: une configuration "
                          "LIVE-capable exige une valeur explicite")
        return CONTRACT_CAP_DEFAULT, None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, f"MAX_CONTRACTS_PER_ORDER invalide: {raw!r}"
    if v <= 0:
        return None, f"MAX_CONTRACTS_PER_ORDER <= 0 ({v}): soumissions desactivees"
    if v > CONTRACT_CAP_MAX:
        return None, (f"MAX_CONTRACTS_PER_ORDER deraisonnable ({v} > "
                      f"{CONTRACT_CAP_MAX}): rejete par validation")
    return v, None
