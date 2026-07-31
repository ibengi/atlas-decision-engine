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
    RISK_BUDGET_PCT   = _env_f("RISK_BUDGET_PCT", 5.0)      # % capital en risque ouvert total
    DD_THROTTLE_PCT   = _env_f("DD_THROTTLE_PCT", 10.0)     # au-dela: taille /2
    MAX_OPEN_POSITIONS      = _env_i("MAX_OPEN_POSITIONS", 3)
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

# ══════════════════════════════════════════════════════════════════════════
# S3. PERSISTANCE ATOMIQUE (tmp + os.replace, checksum, rotation de backups)
# ══════════════════════════════════════════════════════════════════════════

class JsonStore:
    """Ecriture atomique, checksum sha256, rotation de sauvegardes,
    et lecture avec reprise automatique sur backup en cas de corruption."""

    @staticmethod
    def _sha(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def save(cls, path: str, data) -> bool:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            payload = json.dumps(data, indent=1, ensure_ascii=False).encode()
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            # rotation des backups AVANT remplacement
            if os.path.exists(path):
                for i in range(CFG.BACKUPS - 1, 0, -1):
                    src, dst = f"{path}.bak{i}", f"{path}.bak{i+1}"
                    if os.path.exists(src): shutil.copy2(src, dst)
                shutil.copy2(path, f"{path}.bak1")
            os.replace(tmp, path)
            sha_tmp = path + ".sha256.tmp"
            with open(sha_tmp, "w", encoding="utf-8") as f:
                f.write(cls._sha(payload)); f.flush(); os.fsync(f.fileno())
            os.replace(sha_tmp, path + ".sha256")
            return True
        except Exception as e:
            log.error(f"JsonStore.save({path}): {e}")
            return False

    @classmethod
    def load(cls, path: str, default):
        candidates = [path] + [f"{path}.bak{i}" for i in range(1, CFG.BACKUPS + 1)]
        for cand in candidates:
            if not os.path.exists(cand):
                continue
            try:
                raw = open(cand, "rb").read()
                data = json.loads(raw.decode())
                if cand == path and os.path.exists(path + ".sha256"):
                    want = open(path + ".sha256").read().strip()
                    if want and want != cls._sha(raw):
                        log.warning(f"JsonStore: checksum invalide pour {path} "
                                    f"-- tentative sur backup.")
                        continue
                if cand != path:
                    log.warning(f"JsonStore: {path} corrompu/absent -- "
                                f"recupere depuis {cand}.")
                return data
            except Exception:
                continue
        return default

# ══════════════════════════════════════════════════════════════════════════
# S4. ERREURS API + RETRY/BACKOFF
# ══════════════════════════════════════════════════════════════════════════

class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str, body: str = ""):
        self.status, self.body = status, body[:500]
        super().__init__(f"HTTP {status}: {message}")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

def pick(d: dict, *names, default=None):
    """Extraction tolerante : retourne la premiere cle presente et non nulle."""
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default

def pick_int(d: dict, *names, default=0) -> int:
    v = pick(d, *names, default=None)
    try:    return int(float(v))
    except (TypeError, ValueError): return default

# ══════════════════════════════════════════════════════════════════════════
# S5. CLIENT KALSHI (env demo/prod, signature RSA, retry/backoff)
# ══════════════════════════════════════════════════════════════════════════

class KalshiClient:
    """Client HTTP signe. env='demo' -> demo-api (cles demo si fournies),
    env='prod' -> production. TOUT (donnees, ordres, reglements) passe par
    le MEME environnement, condition de coherence d'un vrai broker."""

    def __init__(self, env: str = "demo"):
        self.env      = env
        self.base_url = CFG.DEMO_URL if env == "demo" else CFG.PROD_URL
        if env == "demo":
            # REGLE ABSOLUE : cles demo obligatoires, repli PROD interdit.
            if not (CFG.DEMO_KEY_ID and CFG.DEMO_PRIV_KEY.strip()):
                raise RuntimeError(
                    "Mode DEMO: KALSHI_DEMO_KEY_ID et KALSHI_DEMO_PRIVATE_KEY "
                    "sont obligatoires (variables d'environnement). Le repli "
                    "silencieux sur les cles PRODUCTION est interdit. Arret.")
            self.key_id, key_pem = CFG.DEMO_KEY_ID, CFG.DEMO_PRIV_KEY
            self.cred_src = "cles DEMO dediees"
        else:
            self.key_id, key_pem = CFG.KEY_ID, CFG.PRIV_KEY
            self.cred_src = "cles PROD"
        self.session = requests.Session()
        self._pk = self._load_key(key_pem)
        self._raw_logged = set()   # types de reponses deja loggees en brut

    # -- Signature ----------------------------------------------------------
    def _load_key(self, key_pem: str):
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            # Sans ce paquet, AUCUNE requete authentifiee ne peut aboutir :
            # continuer produirait un 401 silencieux a chaque cycle (bug
            # observe en production le 2026-07-25). Arret net + remede.
            log.critical(
                "[FATAL] Le paquet 'cryptography' est absent : impossible de "
                "signer les requetes Kalshi (KALSHI-ACCESS-SIGNATURE). "
                "Remede: l'ajouter aux dependances installees au deploiement "
                "(requirements.txt: cryptography>=42) puis redeployer.")
            raise SystemExit(4)
        try:
            key_text = (key_pem or "").strip()
            if not key_text.startswith("-----"):
                log_api.warning("Cle privee absente ou non-PEM -- les "
                                "endpoints /portfolio seront REFUSES "
                                "explicitement (pas de 401 silencieux).")
                return None
            return serialization.load_pem_private_key(key_text.encode(), password=None)
        except Exception as e:
            log_api.warning(f"Chargement cle RSA impossible: {e} -- les "
                            f"endpoints /portfolio seront REFUSES "
                            f"explicitement.")
            return None

    def _sign_headers(self, method: str, url: str) -> dict:
        if not self._pk or not self.key_id:
            return {"Content-Type": "application/json"}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts  = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{urlparse(url).path}".encode()
        sig = self._pk.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "Content-Type":            "application/json",
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    # -- Requete avec retry/backoff ------------------------------------------
    def _req(self, method: str, path: str, *, retries: int = 3, **kw) -> dict:
        if self._pk is None and path.startswith("/portfolio"):
            raise KalshiAPIError(
                0, f"{method} {path}: requete authentifiee IMPOSSIBLE — cle "
                   f"RSA non chargee (cle absente/non-PEM ou dependance "
                   f"manquante). Verifier KALSHI_DEMO_PRIVATE_KEY (PEM "
                   f"complet avec les lignes -----BEGIN/END-----) et le "
                   f"paquet 'cryptography'.")
        url = self.base_url + path
        attempt, delay = 0, 1.0
        while True:
            attempt += 1
            try:
                r = self.session.request(method.upper(), url,
                                         headers=self._sign_headers(method, url),
                                         timeout=15, **kw)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt > retries:
                    raise KalshiAPIError(0, f"reseau: {e}")
                log_api.warning(f"{method} {path}: {type(e).__name__} -- "
                                f"retry {attempt}/{retries} dans {delay:.0f}s")
                time.sleep(delay); delay = min(delay * 2, 8); continue

            if r.status_code in RETRYABLE_STATUS and attempt <= retries:
                wait = delay
                if r.status_code == 429:
                    try: wait = max(wait, float(r.headers.get("Retry-After", delay)))
                    except ValueError: pass
                log_api.warning(f"{method} {path}: HTTP {r.status_code} -- "
                                f"retry {attempt}/{retries} dans {wait:.0f}s")
                time.sleep(wait); delay = min(delay * 2, 8); continue

            self.last_http_status = r.status_code
            if r.status_code == 410 and "deprecated" in (r.text or "").lower():
                raise KalshiAPIError(
                    410, f"{method} {path}: ENDPOINT V1 OBSOLETE cote Kalshi "
                         f"-- migrer ce chemin vers son equivalent V2 "
                         f"(cf. docs.kalshi.com)", r.text)
            if r.status_code >= 400:
                raise KalshiAPIError(r.status_code, f"{method} {path}", r.text)

            try:
                return r.json() if r.text.strip() else {}
            except ValueError:
                raise KalshiAPIError(r.status_code, f"{method} {path}: JSON invalide", r.text)

    def _log_raw_once(self, kind: str, payload: dict):
        """Logge UNE FOIS la reponse brute de chaque type d'appel critique,
        pour verifier les noms de champs reels de l'API."""
        if kind not in self._raw_logged:
            self._raw_logged.add(kind)
            log_api.info(f"[RAW:{kind}] {json.dumps(payload, ensure_ascii=False)[:800]}")

    # -- Endpoints -----------------------------------------------------------
    def get_markets(self, series: str, status: str = "open", limit: int = 50) -> list:
        try:
            r = self._req("GET", "/markets",
                          params={"series_ticker": series, "status": status, "limit": limit})
            return r.get("markets", []) or []
        except KalshiAPIError as e:
            log_api.error(f"get_markets({series}): {e}")
            return []

    def get_market(self, ticker: str) -> dict:
        try:
            r = self._req("GET", f"/markets/{ticker}")
            return r.get("market", r) or {}
        except KalshiAPIError as e:
            log_api.warning(f"get_market({ticker}): {e}")
            return {}

    def get_balance(self) -> Optional[float]:
        """Solde du compte en $. Champ 'balance' attendu en cents (a verifier)."""
        try:
            r = self._req("GET", "/portfolio/balance")
            self._log_raw_once("balance", r)
            dollars = pick(r, "balance_dollars", "available_balance_dollars", default=None)
            if dollars is not None:
                try:
                    value = float(dollars)
                    return value if value >= 0 else None
                except (TypeError, ValueError):
                    pass
            cents = pick_int(r, "balance", "available_balance", default=-1)
            return cents / 100.0 if cents >= 0 else None
        except KalshiAPIError as e:
            log_api.warning(f"get_balance: {e}")
            return None

    # Create Order V2 (docs.kalshi.com/api-reference/orders/create-order-v2,
    # OpenAPI 3.20.0). L'ancien POST /portfolio/orders repond HTTP 410
    # deprecated_v1_order_endpoint depuis 2026 (observe en prod 2026-07-25).
    ORDERS_V2_PATH = "/portfolio/events/orders"

    def create_order(self, ticker: str, side: str, count: int,
                     price_cents: int, client_order_id: str = None) -> dict:
        """Ordre limite ACHAT via le schema V2 : tout est cote sur le carnet
        YES ('bid'=acheter YES ; 'ask'=vendre YES = acheter NO a 1-prix),
        prix en dollars fixed-point, quantite en chaine. La reponse V2 n'a
        PAS de champ status : il est DERIVE de fill/remaining, et la reponse
        est normalisee vers le schema interne (compteurs entiers, prix en
        cents ramene a NOTRE cote)."""
        price_cents = int(price_cents)
        if side == "yes":
            v2_side, v2_price_c = "bid", price_cents
        else:                       # acheter NO a n cents == ask YES a 100-n
            v2_side, v2_price_c = "ask", 100 - price_cents
        payload = {
            "ticker":          ticker,
            "client_order_id": client_order_id or f"alpha_{uuid.uuid4().hex}",
            "side":            v2_side,
            "count":           str(int(count)),
            "price":           f"{v2_price_c / 100:.4f}",
            "time_in_force":   "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        r = self._req("POST", self.ORDERS_V2_PATH, json=payload)
        self._log_raw_once("create_order_v2", r)
        raw = r.get("order", r) or {}

        def _fp_int(v):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return None
        filled = _fp_int(raw.get("fill_count"))
        remaining = _fp_int(raw.get("remaining_count"))
        if filled is not None and filled >= int(count):
            status = "executed"
        elif remaining is not None and remaining > 0:
            status = "resting"
        elif filled and filled > 0:
            status = "canceled"     # reste annule (IOC partiel)
        else:
            status = str(raw.get("status") or "resting")
        avg = None
        if raw.get("average_fill_price") is not None:
            try:
                yes_c = round(float(raw["average_fill_price"]) * 100)
                avg = yes_c if side == "yes" else 100 - yes_c
            except (TypeError, ValueError):
                avg = None
        return {
            "order_id": raw.get("order_id"),
            "client_order_id": raw.get("client_order_id")
            or payload["client_order_id"],
            "status": status,
            "taker_fill_count": filled if filled is not None else 0,
            "remaining_count": remaining,
            "avg_price_cents": avg,
            "average_fee_paid": raw.get("average_fee_paid"),
            "ts_ms": raw.get("ts_ms"),
            "v2_side": v2_side, "v2_price": payload["price"],
            "raw": raw,
        }

    def get_order(self, order_id: str) -> dict:
        r = self._req("GET", f"/portfolio/orders/{order_id}")
        self._log_raw_once("get_order", r)
        return r.get("order", r) or {}

    def cancel_order(self, order_id: str) -> dict:
        """Annule un ordre V2 et exige une preuve exploitable.

        La reponse V2 contient normalement order_id, client_order_id et
        reduced_by. Une erreur HTTP n'est jamais transformee en faux succes.
        """
        r = self._req("DELETE", f"{self.ORDERS_V2_PATH}/{order_id}")
        raw = r.get("order", r) or {}
        returned_id = str(raw.get("order_id") or order_id)
        reduced_by = pick_int(raw, "reduced_by", "reduced_count", default=-1)
        if returned_id != str(order_id):
            raise KalshiAPIError(0, "cancel V2: order_id incoherent", json.dumps(raw))
        if reduced_by < 0:
            raise KalshiAPIError(0, "cancel V2: preuve reduced_by absente", json.dumps(raw))
        log_api.info(f"[CANCEL_V2_CONFIRMED] kalshi_order_id={order_id} "
                     f"reduced_by={reduced_by} "
                     f"endpoint={self.ORDERS_V2_PATH}/{order_id}")
        return raw

    def get_fills(self, order_id: str, *, strict: bool = False) -> list:
        try:
            r = self._req("GET", "/portfolio/fills", params={"order_id": order_id})
            self._log_raw_once("fills", r)
            return r.get("fills", []) or []
        except KalshiAPIError as e:
            log_api.warning(f"get_fills({order_id}): {e}")
            if strict:
                raise
            return []

    def get_positions(self) -> list:
        """Positions cote broker (source de verite pour la reconciliation)."""
        try:
            r = self._req("GET", "/portfolio/positions")
            self._log_raw_once("positions", r)
            return r.get("market_positions", r.get("positions", [])) or []
        except KalshiAPIError as e:
            log_api.warning(f"get_positions: {e}")
            return None

# ══════════════════════════════════════════════════════════════════════════
# S6. MODELE DE FRAIS
# ══════════════════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════════════════
# S7. TRADE LOGGER (journal complet, un enregistrement = un trade REEL)
# ══════════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class TradeLogger:
    """Journal des trades. REGLE ABSOLUE : on n'enregistre un trade que si
    filled_count > 0 (execution verifiee). Les anciens enregistrements
    'dry_run' sont archives a part, jamais melanges aux vrais."""

    SCHEMA = "v11"

    def __init__(self):
        self.path = _p(CFG.TRADES_FILE)
        raw = JsonStore.load(self.path, [])
        legacy = [t for t in raw if t.get("schema") != self.SCHEMA]
        self.trades = [t for t in raw if t.get("schema") == self.SCHEMA]
        if legacy:
            legacy_path = _p("kalshi_trades_legacy.json")
            old = JsonStore.load(legacy_path, [])
            JsonStore.save(legacy_path, old + legacy)
            JsonStore.save(self.path, self.trades)
            log_trd.warning(f"{len(legacy)} enregistrement(s) heritee(s) "
                            f"(dry-run/ancien schema) archives dans "
                            f"kalshi_trades_legacy.json -- exclus des statistiques.")

    def open_trade(self, *, ticker, market_title, side, req_price, avg_price,
                   req_count, filled_count, spread, fees, edge, ev, confidence,
                   grade, reason, analysis, order_id, order_status) -> dict:
        rec = {
            "schema": self.SCHEMA, "trade_id": uuid.uuid4().hex[:12],
            "timestamp": now_iso(), "ticker": ticker, "market": market_title,
            "side": side, "requested_price": req_price, "avg_fill_price": avg_price,
            "requested_count": req_count, "filled_count": filled_count,
            "spread": spread, "fees": round(fees, 2), "edge": edge, "ev": ev,
            "confidence": confidence, "grade": grade, "reason": reason,
            "analysis": analysis, "order_id": order_id, "order_status": order_status,
            "state": "open",       # open -> settled | expired
            "result": None, "won": None,
            "gross_pnl": None, "net_pnl": None, "roi": None,
            "holding_seconds": None, "settled_at": None,
        }
        self.trades.append(rec)
        self.flush()
        log_trd.info(f"OUVERT {ticker} {side.upper()} {filled_count}/{req_count} "
                     f"@ {avg_price}c (frais {fees:.2f}$) ordre={order_id}")
        return rec

    def settle_trade(self, trade_id: str, result: str, won: bool,
                     gross_pnl: float, net_pnl: float):
        for t in self.trades:
            if t["trade_id"] == trade_id:
                opened = datetime.fromisoformat(t["timestamp"])
                t.update({
                    "state": "settled", "result": result, "won": won,
                    "gross_pnl": round(gross_pnl, 2), "net_pnl": round(net_pnl, 2),
                    "roi": round(net_pnl / max(0.01, t["avg_fill_price"] / 100.0
                                               * t["filled_count"]), 4),
                    "settled_at": now_iso(),
                    "holding_seconds": int((datetime.now(timezone.utc) - opened)
                                           .total_seconds()),
                })
                self.flush()
                log_trd.info(f"REGLE  {t['ticker']} -> {result.upper()} | "
                             f"{'GAGNE' if won else 'PERDU'} | net {net_pnl:+.2f}$")
                return t
        log_trd.error(f"settle_trade: trade_id {trade_id} introuvable.")
        return None

    def has_open_on(self, ticker: str) -> bool:
        return any(t["ticker"] == ticker and t["state"] == "open" for t in self.trades)

    def open_trades(self) -> list:
        return [t for t in self.trades if t["state"] == "open"]

    def settled_trades(self) -> list:
        return [t for t in self.trades if t["state"] == "settled"]

    def flush(self):
        JsonStore.save(self.path, self.trades)

# ══════════════════════════════════════════════════════════════════════════
# S8. POSITION MANAGER (positions ouvertes, reglement, PnL realise)
# ══════════════════════════════════════════════════════════════════════════

class PositionManager:
    """Positions indexees par trade_id (plusieurs lots possibles par ticker
    si ONE_TRADE_PER_MARKET est desactive). Migration automatique de
    l'ancienne structure ticker->pos. Reconciliation broker idempotente."""

    def __init__(self, client: KalshiClient, trade_log: TradeLogger):
        self.client, self.tlog = client, trade_log
        raw = JsonStore.load(_p(CFG.POSITIONS_FILE), {})
        self.positions = self._migrate(raw)          # trade_id -> pos
        self.seen_fill_ids = set(
            JsonStore.load(_p("seen_fill_ids.json"), []))

    @staticmethod
    def _migrate(raw: dict) -> dict:
        out = {}
        for k, p in (raw or {}).items():
            if "ticker" in p:                        # nouveau format
                out[k] = p
            else:                                    # ancien: cle = ticker
                tid = p.get("trade_id") or f"mig-{k}"
                out[tid] = {**p, "ticker": k}
        return out

    def flush(self):
        JsonStore.save(_p(CFG.POSITIONS_FILE), self.positions)
        JsonStore.save(_p("seen_fill_ids.json"),
                       sorted(self.seen_fill_ids)[-5000:])

    def open_position(self, trade: dict, extra: dict = None):
        pos = {
            "trade_id": trade["trade_id"], "ticker": trade["ticker"],
            "side": trade["side"],
            "count_initial": trade["filled_count"],
            "count": trade["filled_count"],
            "avg_price": trade["avg_fill_price"],
            "fees": trade["fees"], "opened_at": trade["timestamp"],
            "order_ids": [trade.get("order_id")],
            "fill_ids": (extra or {}).get("fill_ids", []),
            "state": "open",
            "strategy": (extra or {}).get("strategy"),
            "category": (extra or {}).get("category", "Other"),
            "market_score": (extra or {}).get("market_score"),
            "entry_edge": (extra or {}).get("entry_edge"),
            "entry_ev": (extra or {}).get("entry_ev"),
        }
        self.positions[trade["trade_id"]] = pos
        for fid in pos["fill_ids"]:
            self.seen_fill_ids.add(fid)
        self.flush()
        log_pos.info(f"{trade['ticker']}: {trade['side'].upper()} "
                     f"x{trade['filled_count']} @ {trade['avg_fill_price']}c")

    def _active_positions(self):
        return (p for p in self.positions.values() if p.get("state", "open") == "open")

    def tickers_open(self) -> set:
        return {p["ticker"] for p in self._active_positions()}

    def open_count(self) -> int:
        return sum(1 for _ in self._active_positions())

    def open_risk(self) -> float:
        """Capital en risque = cout total des positions effectivement ouvertes."""
        return sum(p["count"] * p["avg_price"] / 100.0
                   for p in self._active_positions())

    def open_risk_by_category(self) -> dict:
        out = {}
        for p in self._active_positions():
            cat = p.get("category", "Other")
            out[cat] = out.get(cat, 0.0) + p["count"] * p["avg_price"] / 100.0
        return out

    def open_risk_on(self, ticker: str) -> float:
        return sum(p["count"] * p["avg_price"] / 100.0
                   for p in self._active_positions() if p["ticker"] == ticker)

    def unrealized_pnl(self, mid_price_lookup=None) -> float:
        """PnL latent estime au prix mid courant (0 si donnee indisponible)."""
        total = 0.0
        for p in self._active_positions():
            if not mid_price_lookup: continue
            mid = mid_price_lookup(p["ticker"], p["side"])
            if mid is None: continue
            total += p["count"] * (mid - p["avg_price"]) / 100.0
        return total

    def check_settlements(self) -> list:
        """Interroge l'API pour les marches regles ; realise le PnL.
        Ecriture du reglement AVANT retrait de la position : un crash entre
        les deux laisse au pire un doublon detecte (trade deja settled),
        jamais un trade zombie."""
        realized = []
        for tid, p in list(self.positions.items()):
            if p.get("state", "open") != "open":
                continue
            m = self.client.get_market(p["ticker"])
            if not m: continue
            result = str(pick(m, "result", default="") or "").lower()
            status = str(pick(m, "status", default="") or "").lower()
            if result not in ("yes", "no"):
                if status in ("settled", "finalized"):
                    log_pos.warning(f"{p['ticker']}: statut '{status}' mais champ "
                                    f"'result' illisible -- verifier le schema API.")
                continue
            won  = (result == p["side"])
            cost = p["count"] * p["avg_price"] / 100.0
            gross = (p["count"] * 1.0 - cost) if won else -cost
            net   = gross - p["fees"]
            t = self.tlog.settle_trade(p["trade_id"], result, won, gross, net)
            self.positions.pop(tid, None)
            self.flush()
            if t: realized.append(t)
        return realized

    def reconcile_with_broker(self) -> dict:
        """Broker = source de verite. Reconstruit les positions presentes
        chez Kalshi mais absentes localement (id stable => idempotent),
        marque 'ghost' les positions locales absentes du broker."""
        report = {"rebuilt": [], "ghost": [], "matched": []}
        broker = self.client.get_positions()
        if broker is None:
            return report
        seen_tickers = set()
        for bp in broker:
            tk = bp.get("ticker")
            if not tk:
                continue
            qty = pick_int(bp, "position", "quantity", "count", default=0)
            if qty == 0:
                continue
            side = "yes" if qty > 0 else "no"
            seen_tickers.add(tk)
            local = [p for p in self._active_positions() if p["ticker"] == tk]
            if local:
                report["matched"].append(tk)
                continue
            tid = f"brk-{tk}-{side}"                 # ID STABLE = idempotent
            if tid in self.positions:
                continue
            avg = pick_int(bp, "avg_price", "market_exposure", default=50) or 50
            self.positions[tid] = {
                "trade_id": tid, "ticker": tk, "side": side,
                "count_initial": abs(qty), "count": abs(qty),
                "avg_price": avg, "fees": 0.0, "opened_at": now_iso(),
                "order_ids": [], "fill_ids": [], "state": "open",
                "strategy": "reconciled", "market_score": None,
                "entry_edge": None, "entry_ev": None,
            }
            report["rebuilt"].append(tk)
        for tid, p in list(self.positions.items()):
            if p["ticker"] not in seen_tickers and not tid.startswith("mig-"):
                p["state"] = "ghost_local_only"
                report["ghost"].append(p["ticker"])
        if report["rebuilt"] or report["ghost"]:
            self.flush()
            JsonStore.save(_p("reconciliation_report.json"), report)
            log_pos.warning(f"Reconciliation broker: reconstruites="
                            f"{report['rebuilt']} fantomes={report['ghost']}")
        return report

    def reconcile_startup(self):
        """Apres crash/redemarrage : les positions persistees restent valides
        (elles vivent chez le broker) ; on les re-verifie au prochain cycle."""
        if self.positions:
            log_pos.info(f"Recovery: {len(self.positions)} position(s) ouverte(s) "
                         f"rechargee(s): {', '.join(self.tickers_open())}")

# ══════════════════════════════════════════════════════════════════════════
# S9. ORDER MANAGER (placement, surveillance, TTL, fills partiels, recovery)
# ══════════════════════════════════════════════════════════════════════════

class ExecutionResult:
    def __init__(self, order_id, requested, filled, avg_price, status, state):
        self.order_id, self.requested, self.filled = order_id, requested, filled
        self.avg_price, self.status, self.state = avg_price, status, state
        # state: "filled" | "partial" | "cancelled" | "rejected" | "unknown"

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


class OrderManager:
    TERMINAL = {"executed", "canceled", "cancelled", "expired", "filled"}

    def __init__(self, client: KalshiClient):
        self.client = client
        self.open_orders = JsonStore.load(_p(CFG.ORDERS_FILE), {})  # id -> meta
        # Garde anti-doublon de SESSION, independante de l'enregistrement des
        # trades : un ordre soumis (201) sur un ticker verrouille ce ticker
        # pour la duree configuree, MEME si la verification echoue ensuite.
        # Motif : bug 2026-07-25 (GET V1 404 sur ordres V2) -> le meme signal
        # a ete re-soumis et REMPLI ~8 fois, solde debite sans aucun trade
        # local. Ce verrou rend ce mode de defaillance impossible.
        guard_raw = JsonStore.load(_p("submission_guard.json"), {})
        now = time.time()
        self.session_submitted = {
            str(tk): float(ts) for tk, ts in (guard_raw or {}).items()
            if isinstance(ts, (int, float)) and
            (CFG.SUBMIT_DEDUP_TTL_S <= 0 or now - float(ts) < CFG.SUBMIT_DEDUP_TTL_S)
        }                                   # ticker -> epoch de soumission
        # Cooldown apres 503 exchange (demo en pause/maintenance) : inutile
        # et bruyant de marteler l'API a chaque cycle.
        self.exchange_pause_until = 0.0
        log_api.info(f"[SUBMISSION_GUARD_LOADED] active_tickers={len(self.session_submitted)} "
                     f"ttl_seconds={CFG.SUBMIT_DEDUP_TTL_S:.0f}")

    def flush(self):
        JsonStore.save(_p(CFG.ORDERS_FILE), self.open_orders)
        self._flush_submission_guard()

    def _flush_submission_guard(self):
        """Persiste le verrou anti-doublon afin qu'un redemarrage Railway
        ne permette pas de resoumettre le meme ticker pendant le TTL."""
        now = time.time()
        if CFG.SUBMIT_DEDUP_TTL_S > 0:
            self.session_submitted = {
                tk: ts for tk, ts in self.session_submitted.items()
                if now - ts < CFG.SUBMIT_DEDUP_TTL_S
            }
        JsonStore.save(_p("submission_guard.json"), self.session_submitted)

    # -- extraction tolerante de l'etat d'un ordre ---------------------------
    @staticmethod
    def _extract(order: dict, requested: int):
        status = str(pick(order, "status", "order_status", default="") or "").lower()
        # Formats numeriques ET a virgule fixe *_fp ("2.00") reellement
        # renvoyes par l'API (fixture utilisateur) -- D1.
        filled = pick_int(order, "taker_fill_count", "fill_count", "fill_count_fp",
                          "filled_count", "filled_quantity", default=-1)
        remaining = pick_int(order, "remaining_count", "remaining_count_fp",
                             "remaining_quantity", default=-1)
        if filled < 0 and remaining >= 0:
            filled = max(0, requested - remaining)
        # D2 : un statut "executed" seul ne vaut JAMAIS confirmation de fill.
        # La confirmation passe par les compteurs ci-dessus ou, en dernier
        # recours, par /portfolio/fills (voir place_and_track).
        if filled < 0:
            filled = 0
        return status, min(filled, requested)

    def _avg_fill_price(self, order_id: str, side: str, fallback: int,
                        fills: list = None) -> int:
        # CORRECTIF AUDIT : accepte des fills deja recuperes pour eviter
        # un second appel a /portfolio/fills pour le meme order_id.
        if fills is None:
            try:
                fills = self.client.get_fills(order_id)
            except KalshiAPIError:
                fills = []
        tot_c, tot_px = 0, 0
        for f in fills:
            c  = pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
            raw = pick(f, f"{side}_price_dollars", "price_dollars",
                       f"{side}_price", "price", default=None)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            # v >= 1 -> deja en cents ; v < 1 -> en dollars ("0.1200" = 12c)
            px = int(round(v)) if v >= 1 else int(round(v * 100))
            if c > 0 and 1 <= px <= 99:
                tot_c += c; tot_px += c * px
        if tot_c > 0:
            return round(tot_px / tot_c)
        log_api.warning(f"Fills indisponibles pour {order_id} -- prix moyen "
                        f"suppose = prix limite ({fallback}c).")
        return fallback

    # -- cycle de vie complet d'un ordre --------------------------------------
    def place_and_track(self, ticker: str, side: str, count: int,
                        limit_cents: int) -> ExecutionResult:
        # INVARIANT DUR : aucun create_order sans prix executable valide.
        # Derniere ligne de defense contre un carnet vide qui aurait
        # traverse scanner, ranker et validateur.
        if limit_cents is None or not isinstance(limit_cents, (int, float)) \
                or not (1 <= int(limit_cents) <= 99) \
                or side not in ("yes", "no") or count <= 0:
            log_api.error(f"ORDRE BLOQUE (invariant): {ticker} side={side} "
                          f"count={count} limite={limit_cents!r} -- "
                          f"prix/ask invalide, create_order NON appele.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:no_executable_ask", "rejected")
        limit_cents = int(limit_cents)
        assert_real_demo_integrity(self.client, CFG.SHADOW_MODE)
        if not CFG.ALLOW_ORDER_SUBMISSION:
            log_api.error("[ORDER_SUBMIT_ATTEMPT] bloque: "
                          "ALLOW_ORDER_SUBMISSION=false")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:submission_disabled", "rejected")
        now = time.time()
        if now < self.exchange_pause_until:
            log_api.warning("[ORDER_SUBMIT_SKIPPED] exchange en cooldown "
                            f"apres 503 (reprise dans "
                            f"{self.exchange_pause_until - now:.0f}s)")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:exchange_unavailable_cooldown",
                                   "rejected")
        last = self.session_submitted.get(ticker)
        if last is not None and now - last < CFG.SUBMIT_DEDUP_TTL_S:
            log_api.warning("[ORDER_SUBMIT_SKIPPED] garde anti-doublon: un "
                            f"ordre a deja ete soumis sur {ticker} il y a "
                            f"{now - last:.0f}s (TTL "
                            f"{CFG.SUBMIT_DEDUP_TTL_S:.0f}s) -- re-soumission "
                            "refusee meme si le trade local est absent.")
            return ExecutionResult(None, count, 0, limit_cents,
                                   "blocked:duplicate_submission_guard",
                                   "rejected")
        client_order_id = f"alpha_{uuid.uuid4().hex}"
        env_name = "DEMO" if getattr(self.client, "env", "demo") == "demo" \
            else "PROD"
        # Exigence 4 : preuve d'envoi AVANT l'appel. Jamais de cle, secret,
        # signature ni token dans les logs (seuls les champs metier).
        v2_side = "bid" if side == "yes" else "ask"
        v2_price = (limit_cents if side == "yes"
                    else 100 - limit_cents) / 100.0
        log_api.info("[ORDER_SUBMIT_ATTEMPT] "
                     f"ticker={ticker} side={side} action=buy count={count} "
                     f"price_cents={limit_cents} "
                     f"(V2: side={v2_side} price={v2_price:.4f}) "
                     f"client_order_id={client_order_id} "
                     f"environment={env_name} "
                     f"endpoint="
                     f"{getattr(self.client, 'ORDERS_V2_PATH', '/portfolio/events/orders')}")
        try:
            order = self.client.create_order(ticker, side, count, limit_cents,
                                             client_order_id=client_order_id)
        except KalshiAPIError as e:
            log_api.error("[ORDER_SUBMIT_FAILED] "
                          f"http_status={e.status} error_code={e.status} "
                          f"error_message={e} "
                          f"response_body_sanitized={str(e.body)[:300]}")
            if getattr(e, "status", None) == 503:
                self.exchange_pause_until = time.time() + CFG.EXCHANGE_503_COOLDOWN_S
                log_api.warning("[EXCHANGE_COOLDOWN] 503 exchange -- pause "
                                f"des soumissions {CFG.EXCHANGE_503_COOLDOWN_S:.0f}s "
                                "(demo probablement en maintenance).")
            return ExecutionResult(None, count, 0, limit_cents, f"rejected:{e.status}",
                                   "rejected")
        self.exchange_pause_until = 0.0
        self.session_submitted[ticker] = time.time()
        # Persistance IMMEDIATE : meme un crash entre le HTTP 201 et
        # l'enregistrement local ne peut plus autoriser un doublon.
        self._flush_submission_guard()
        http_status = getattr(self.client, "last_http_status", None)
        order_id = str(pick(order, "order_id", "id", default="") or "")
        log_api.info("[ORDER_SUBMIT_RESPONSE] "
                     f"http_status={http_status} "
                     f"request_id={order.get('request_id', '-')} "
                     f"kalshi_order_id={order_id or '-'} "
                     f"client_order_id={client_order_id} "
                     f"status={pick(order, 'status', 'order_status', default='-')} "
                     f"raw_response_sanitized="
                     f"{json.dumps(order, default=str)[:400]}")
        # Exigence 5 : relecture IMMEDIATE depuis l'API. Tant qu'elle n'a
        # pas reussi, l'ordre n'est PAS considere comme confirme.
        if order_id:
            try:
                verified = self.client.get_order(order_id)
            except KalshiAPIError as e:
                verified = {}
                log_api.warning(f"[ORDER_VERIFY] relecture en erreur: {e}")
                # BUG CORRIGE (logs 2026-07-25 23:14+) : les ordres crees via
                # V2 repondent 404 sur le GET V1 -> tous les fills REELS
                # etaient rejetes "unverified", aucun trade enregistre, et le
                # MEME signal etait re-soumis chaque cycle (solde debite en
                # silence). La reponse de creation V2 vient du moteur
                # d'appariement (order_id, fill_count, remaining_count,
                # ts_ms) : c'est une source de verite valide. On l'utilise
                # comme verification quand le GET est indisponible ; la
                # confirmation finale reste FILL_VERIFY + POSITION_VERIFY.
                if getattr(e, "status", None) == 404 and (
                        order.get("ts_ms") or "fill_count" in
                        (order.get("raw") or {})):
                    verified = order
                    log_api.info("[ORDER_VERIFY] "
                                 "source=create_response_v2 (GET ordre "
                                 "indisponible pour les ordres V2 -- etat "
                                 "certifie par la reponse du moteur "
                                 f"d'appariement, ts_ms={order.get('ts_ms')})")
            if verified:
                v_status, v_filled = self._extract(verified, count)
                log_api.info("[ORDER_VERIFY] "
                             f"kalshi_order_id={order_id} status={v_status} "
                             f"remaining_count={pick_int(verified, 'remaining_count', default=-1)} "
                             f"filled_count={v_filled} "
                             f"yes_price={verified.get('yes_price', '-')} "
                             f"no_price={verified.get('no_price', '-')}")
                order = verified
            else:
                log_api.error("[ORDER_VERIFY_FAILED] "
                              "reason=order_not_found_after_submission")
                return ExecutionResult(order_id, count, 0, limit_cents,
                                       "unverified", "rejected")
        if not order_id:
            log_api.error(f"Reponse d'ordre sans identifiant -- trade NON enregistre. "
                          f"Reponse: {json.dumps(order)[:300]}")
            return ExecutionResult(None, count, 0, limit_cents, "no_id", "rejected")

        self.open_orders[order_id] = {"ticker": ticker, "side": side,
                                      "count": count, "price": limit_cents,
                                      "placed_at": now_iso()}
        self.flush()

        start = time.time()
        deadline = start + CFG.ORDER_TTL_SECONDS
        delay = max(2.0, min(5.0, CFG.ORDER_VERIFY_INTERVAL_S))
        status, filled = self._extract(order, count)
        get_order_available = True     # 404 V2 : suivi via /fills a la place
        while time.time() < deadline and status not in self.TERMINAL and filled < count:
            log_api.info("[ORDER_WAITING_FOR_FILL] "
                         f"elapsed_seconds={time.time() - start:.0f} "
                         f"status={status or 'resting'} "
                         f"remaining_count={count - filled}")
            time.sleep(delay)
            try:
                if get_order_available:
                    order = self.client.get_order(order_id)
                    status, filled = self._extract(order, count)
                else:
                    raise KalshiAPIError(404, "get_order indisponible (V2)")
            except KalshiAPIError as e:
                if getattr(e, "status", None) == 404:
                    get_order_available = False
                    try:            # source de verite alternative : /fills
                        fills = self.client.get_fills(order_id)
                        got = sum(pick_int(fl, "count", "count_fp", "quantity", "quantity_fp", default=0)
                                  for fl in fills)
                        if got > filled:
                            filled = min(count, got)
                        if filled >= count:
                            status = "executed"
                    except KalshiAPIError as e2:
                        log_api.warning(f"Suivi ordre {order_id} via fills: {e2}")
                else:
                    log_api.warning(f"Suivi ordre {order_id}: {e}")

        if status not in self.TERMINAL and filled < count:
            if not CFG.CANCEL_UNFILLED_ORDERS:
                log_api.info(f"[ORDER_STILL_RESTING] {order_id} "
                             f"(CANCEL_UNFILLED_ORDERS=false)")
                return ExecutionResult(order_id, count, filled, limit_cents,
                                       status or "resting", "resting")

            pre_cancel_filled = filled
            log_api.info("[ORDER_CANCEL_ATTEMPT] "
                         f"kalshi_order_id={order_id} "
                         f"timeout_seconds={CFG.ORDER_TTL_SECONDS} "
                         f"known_filled={filled}/{count}")
            try:
                cancel_resp = self.client.cancel_order(order_id)
                reduced_by = pick_int(cancel_resp, "reduced_by", "reduced_count", default=-1)
                # Reconciliation obligatoire apres annulation : les fills ont
                # pu augmenter entre le dernier poll et le DELETE.
                fills_after = self.client.get_fills(order_id, strict=True)
                filled_after = min(count, sum(
                    pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                    for f in fills_after))
                filled = max(pre_cancel_filled, filled_after)
                expected_reduction = max(0, count - filled)
                if reduced_by < 0 or reduced_by < expected_reduction:
                    raise KalshiAPIError(
                        0,
                        f"annulation non prouvee: reduced_by={reduced_by}, "
                        f"reste_attendu={expected_reduction}",
                        json.dumps(cancel_resp))
                status = "canceled"
                log_api.info("[ORDER_CANCEL_CONFIRMED] "
                             f"kalshi_order_id={order_id} reduced_by={reduced_by} "
                             f"final_filled={filled}/{count}")
            except KalshiAPIError as e:
                # FAIL-CLOSED : conserver l'ordre dans orders_state.json et
                # refuser de pretendre qu'il est annule. Le garde anti-doublon
                # reste actif et la reconciliation de demarrage reprendra.
                meta = self.open_orders.get(order_id, {})
                meta.update({"state": "unknown_cancel_failed",
                             "last_error": str(e),
                             "last_checked_at": now_iso(),
                             "known_filled": filled})
                self.open_orders[order_id] = meta
                self.flush()
                log_api.error("[ORDER_CANCEL_FAILED] "
                              f"kalshi_order_id={order_id} error={e} "
                              "state=UNKNOWN trading_for_ticker_halted=true")
                return ExecutionResult(order_id, count, filled, limit_cents,
                                       "unknown_cancel_failed", "unknown")

        self.open_orders.pop(order_id, None); self.flush()

        # Confirmation par l'endpoint des fills (jamais par le statut seul) :
        # si le statut pretend "executed"/"filled" sans compteur exploitable,
        # on interroge /portfolio/fills, source de verite.
        if filled <= 0 and status in ("executed", "filled"):
            try:
                fills = self.client.get_fills(order_id)
            except KalshiAPIError as e:
                log_api.warning(f"/fills indisponible pour {order_id}: {e}")
                fills = []
            filled = min(count, sum(pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                                    for f in fills))
            if filled > 0:
                log_api.info(f"Ordre {order_id}: statut '{status}' sans compteur "
                             f"-- {filled} contrat(s) confirme(s) via /fills.")

        if filled <= 0:
            log_api.info("[FILL_VERIFY] "
                         f"kalshi_order_id={order_id} fills_count=0 "
                         f"filled_contracts=0 average_fill_price=- fees=- "
                         f"(ordre accepte par l'API mais AUCUN fill — "
                         f"accepted != filled)")
            return ExecutionResult(order_id, count, 0, limit_cents,
                                   status or "unfilled", "cancelled")
        try:
            fills = self.client.get_fills(order_id)
        except KalshiAPIError as e:
            log_api.warning(f"/fills indisponible pour {order_id}: {e} -- "
                            "frais/prix moyens estimes depuis la reponse de "
                            "creation V2.")
            fills = []
        # CORRECTIF AUDIT : le calcul local de "fees" ici a ete supprime --
        # il n'etait jamais transmis a l'appelant (ExecutionResult n'a pas
        # de champ fees) et sommait le champ "fees" des fills SANS la
        # conversion cents/dollars appliquee ailleurs par FeeModel._amount,
        # ce qui aurait pu donner un montant errone de x100 si jamais
        # utilise. Le frais REEL est calcule une seule fois, correctement,
        # dans _execute_decision via FeeModel.from_api. On reutilise les
        # `fills` deja recuperes ci-dessus pour le prix moyen (evite un
        # second appel a /portfolio/fills pour le meme ordre).
        avg = self._avg_fill_price(order_id, side, limit_cents, fills=fills)
        log_api.info("[FILL_VERIFY] "
                     f"kalshi_order_id={order_id} fills_count={len(fills)} "
                     f"filled_contracts={filled} average_fill_price={avg}")
        state = "filled" if filled >= count else "partial"
        log_api.info(f"[ORDER_FILLED] kalshi_order_id={order_id} "
                     f"state={state} {filled}/{count} @ {avg}c")
        return ExecutionResult(order_id, count, filled, avg, status or "executed", state)

    def reconcile_startup(self, tlog: TradeLogger, posmgr: PositionManager):
        """Recovery apres crash avec politique fail-closed.

        Un ordre n'est retire du journal local que si son etat est resolu.
        Si GET ordre est indisponible, /fills puis l'annulation V2 servent de
        sources alternatives. Un ordre encore incertain reste persiste pour
        le prochain cycle au lieu d'etre oublie.
        """
        if not self.open_orders:
            return
        log_api.warning(f"Recovery: {len(self.open_orders)} ordre(s) non conclu(s) "
                        "au dernier arret -- reconciliation...")
        for oid, meta in list(self.open_orders.items()):
            resolved = False
            status, filled = "", 0
            try:
                try:
                    order = self.client.get_order(oid)
                    status, filled = self._extract(order, meta["count"])
                except KalshiAPIError as e:
                    if e.status != 404:
                        raise
                    fills = self.client.get_fills(oid)
                    filled = min(meta["count"], sum(
                        pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0) for f in fills))
                    status = "executed" if filled >= meta["count"] else "unknown"

                if status not in self.TERMINAL and filled < meta["count"]:
                    cancel_resp = self.client.cancel_order(oid)
                    reduced_by = pick_int(cancel_resp, "reduced_by", "reduced_count", default=-1)
                    fills = self.client.get_fills(oid, strict=True)
                    filled = min(meta["count"], sum(
                        pick_int(f, "count", "count_fp", "quantity", "quantity_fp", default=0)
                        for f in fills))
                    expected_reduction = max(0, meta["count"] - filled)
                    if reduced_by < 0 or reduced_by < expected_reduction:
                        raise KalshiAPIError(
                            0,
                            f"recovery cancel non prouve: reduced_by={reduced_by}, "
                            f"reste_attendu={expected_reduction}",
                            json.dumps(cancel_resp))
                    status = "partial" if filled > 0 else "cancelled"
                    log_api.info("[RECOVERY_CANCEL_CONFIRMED] "
                                 f"kalshi_order_id={oid} reduced_by={reduced_by} "
                                 f"final_filled={filled}/{meta['count']}")
                    resolved = True
                else:
                    resolved = True

                if filled > 0 and not tlog.has_open_on(meta["ticker"]):
                    avg = self._avg_fill_price(oid, meta["side"], meta["price"])
                    fills = self.client.get_fills(oid)
                    fees, _fee_src = FeeModel.from_api({}, fills, filled, avg)
                    t = tlog.open_trade(
                        ticker=meta["ticker"], market_title="(recovery)",
                        side=meta["side"], req_price=meta["price"], avg_price=avg,
                        req_count=meta["count"], filled_count=filled,
                        spread=None, fees=fees, edge=0.0, ev=0.0, confidence=0,
                        grade="R", reason="ordre recupere apres crash",
                        analysis={"fee_source": _fee_src}, order_id=oid,
                        order_status=status)
                    posmgr.open_position(t)
            except KalshiAPIError as e:
                log_api.error(f"Recovery ordre {oid}: {e} -- ordre conserve localement")
                resolved = False
            except Exception as e:
                log_api.exception(f"Recovery ordre {oid}: {e} -- ordre conserve localement")
                resolved = False
            if resolved:
                self.open_orders.pop(oid, None)
        self.flush()

# ══════════════════════════════════════════════════════════════════════════
# S10. RISK MANAGER (base sur le PnL REALISE, jamais le capital investi)
# ══════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, tlog: TradeLogger, posmgr: PositionManager, capital: float):
        self.tlog, self.posmgr, self.capital = tlog, posmgr, capital
        st = JsonStore.load(_p(CFG.RISK_FILE), {})
        today = datetime.now(timezone.utc).date().isoformat()
        if st.get("date") != today:
            st = {"date": today}
        self.state = st
        self.flush()

    def flush(self):
        JsonStore.save(_p(CFG.RISK_FILE), self.state)

    # -- agregats jour (recalcules depuis le journal : source de verite unique)
    def _today_settled(self) -> list:
        today = datetime.now(timezone.utc).date().isoformat()
        return [t for t in self.tlog.settled_trades()
                if (t.get("settled_at") or "").startswith(today)]

    def daily_realized_pnl(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled())

    def daily_realized_loss(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled() if t["net_pnl"] < 0)

    def daily_realized_profit(self) -> float:
        return sum(t["net_pnl"] for t in self._today_settled() if t["net_pnl"] > 0)

    def trades_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(1 for t in self.tlog.trades
                   if t["timestamp"].startswith(today))

    def rolling_drawdown(self) -> float:
        """Drawdown courant en dollars de la courbe de PnL net cumule."""
        curve, peak = 0.0, 0.0
        for t in self.tlog.settled_trades():
            curve += t["net_pnl"]
            peak = max(peak, curve)
        return max(0.0, peak - curve)

    def rolling_drawdown_pct(self) -> float:
        """Drawdown courant en pourcentage du capital effectif.

        Evite l'ancien melange d'un drawdown en dollars avec une limite en %.
        """
        if self.capital <= 0:
            return 0.0
        return 100.0 * self.rolling_drawdown() / self.capital

    def effective_daily_stop(self) -> float:
        """Stop journalier en $ : min(plafond absolu, MAX_DAILY_LOSS_PCT du
        CAPITAL EFFECTIF). Pour 93,26$ : min(50, 4.66) = 4,66$. Le capital
        de reference (500$) ne peut plus influencer un solde inferieur."""
        pct_stop = max(0.0, self.capital) * CFG.MAX_DAILY_LOSS_PCT / 100.0
        return round(min(CFG.MAX_DAILY_LOSS, pct_stop), 2)

    def consecutive_losses(self) -> int:
        """Pertes consecutives en fin de sequence des trades regles."""
        n = 0
        for t in reversed(self.tlog.settled_trades()):
            if t.get("net_pnl") is not None and t["net_pnl"] < 0:
                n += 1
            else:
                break
        return n

    def seconds_since_last_settlement(self) -> Optional[float]:
        """Anciennete (s) du dernier trade REGLE, ou None si aucun trade
        regle n'existe encore. Utilise pour le cooldown du kill-switch."""
        settled = self.tlog.settled_trades()
        if not settled:
            return None
        ts = settled[-1].get("settled_at")
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds()

    def _last_settlement_anchor(self) -> Optional[str]:
        settled = self.tlog.settled_trades()
        return (settled[-1].get("settled_at") if settled else None)

    def half_open_required(self) -> bool:
        """Vrai uniquement quand la serie de pertes depasse le seuil ET que
        le cooldown est ecoule. Le claim est persistant dans risk_state.json."""
        if self.consecutive_losses() < CFG.MAX_CONSECUTIVE_LOSSES:
            return False
        elapsed = self.seconds_since_last_settlement()
        return elapsed is not None and elapsed >= CFG.CONSECUTIVE_LOSS_COOLDOWN_S

    def claim_half_open_attempt(self, ticker: str) -> (bool, str):
        """Reserve atomiquement l'unique essai demi-ouvert.

        L'ancre est le dernier settled_at. Tant qu'aucun nouveau reglement
        n'est intervenu, une seconde soumission est refusee, y compris apres
        redemarrage du processus.
        """
        if not self.half_open_required():
            return True, ""
        anchor = self._last_settlement_anchor()
        if not anchor:
            return False, "demi-ouvert impossible: dernier reglement inconnu"
        if (self.state.get("half_open_anchor") == anchor and
                self.state.get("half_open_claimed")):
            return False, (
                "ARRET: essai demi-ouvert deja consomme depuis le dernier "
                "trade regle; attendre le reglement de cet essai")
        self.state.update({
            "half_open_anchor": anchor,
            "half_open_claimed": True,
            "half_open_claimed_at": now_iso(),
            "half_open_ticker": ticker,
        })
        self.flush()
        log_rsk.warning(
            f"[RISK] essai demi-ouvert RESERVE pour {ticker}; aucune autre "
            "soumission autorisee avant un nouveau reglement.")
        return True, ""

    def release_half_open_attempt(self, ticker: str, reason: str) -> bool:
        """Libere un claim demi-ouvert uniquement lorsqu'aucun ordre n'a ete
        accepte par Kalshi ou lorsqu'un ordre est confirme sans aucun fill.
        Un etat incertain avec order_id reste verrouille par securite.
        """
        anchor = self._last_settlement_anchor()
        if not anchor:
            return False
        if not (self.state.get("half_open_anchor") == anchor and
                self.state.get("half_open_claimed") and
                self.state.get("half_open_ticker") == ticker):
            return False
        self.state.update({
            "half_open_claimed": False,
            "half_open_released_at": now_iso(),
            "half_open_release_reason": reason,
        })
        self.flush()
        log_rsk.warning(f"[RISK] essai demi-ouvert LIBERE pour {ticker}: {reason}")
        return True

    # -- portes de risque ------------------------------------------------------
    def can_trade(self, cycle_trades: int) -> (bool, str):
        pnl = self.daily_realized_pnl()
        stop = self.effective_daily_stop()
        if pnl <= -stop:
            return False, (f"STOP JOURNALIER: PnL realise {pnl:+.2f}$ <= "
                           f"-{stop:.2f}$ (={CFG.MAX_DAILY_LOSS_PCT:g}% du "
                           f"capital effectif {self.capital:.2f}$)")
        losses = self.consecutive_losses()
        if losses >= CFG.MAX_CONSECUTIVE_LOSSES:
            elapsed = self.seconds_since_last_settlement()
            cooldown = CFG.CONSECUTIVE_LOSS_COOLDOWN_S
            if elapsed is None or elapsed < cooldown:
                remaining = cooldown - (elapsed or 0.0)
                return False, (
                    f"ARRET: {losses} pertes consecutives >= "
                    f"{CFG.MAX_CONSECUTIVE_LOSSES} -- reprise possible dans "
                    f"{max(0.0, remaining):.0f}s (cooldown "
                    f"{cooldown:.0f}s depuis le dernier trade regle)")
            # Cooldown ecoule : verifier qu'aucun essai demi-ouvert n'a
            # deja ete reserve pour le meme dernier reglement.
            anchor = self._last_settlement_anchor()
            if (anchor and self.state.get("half_open_anchor") == anchor and
                    self.state.get("half_open_claimed")):
                return False, (
                    "ARRET: essai demi-ouvert deja consomme depuis le "
                    "dernier trade regle; attendre son reglement")
            log_rsk.warning(
                f"[RISK] cooldown de {cooldown:.0f}s ecoule apres "
                f"{losses} pertes consecutives -- 1 nouvel essai disponible.")
        if cycle_trades >= CFG.MAX_TRADES_CYCLE:
            return False, f"max {CFG.MAX_TRADES_CYCLE} trades/cycle atteint"
        open_risk = self.posmgr.open_risk()
        budget    = self.capital * CFG.RISK_BUDGET_PCT / 100.0
        if open_risk >= budget:
            return False, (f"budget de risque ouvert atteint "
                           f"({open_risk:.2f}$ >= {budget:.2f}$)")
        return True, ""

    def snapshot(self) -> dict:
        settled = self.tlog.settled_trades()
        wins    = [t for t in settled if t["won"]]
        losses  = [t for t in settled if not t["won"]]
        gp = sum(t["net_pnl"] for t in wins)
        gl = -sum(t["net_pnl"] for t in losses)
        return {
            "capital_deployed":      round(self.posmgr.open_risk(), 2),
            "open_risk":             round(self.posmgr.open_risk(), 2),
            "realized_pnl":          round(sum(t["net_pnl"] for t in settled), 2),
            "unrealized_pnl":        round(self.posmgr.unrealized_pnl(), 2),
            "daily_realized_pnl":    round(self.daily_realized_pnl(), 2),
            "daily_realized_loss":   round(self.daily_realized_loss(), 2),
            "daily_realized_profit": round(self.daily_realized_profit(), 2),
            "gross_pnl":             round(sum(t["gross_pnl"] for t in settled), 2),
            "net_pnl":               round(sum(t["net_pnl"] for t in settled), 2),
            "fees_paid":             round(sum(t["fees"] for t in self.tlog.trades), 2),
            "win_rate":  round(len(wins) / len(settled), 4) if settled else 0.0,
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "rolling_drawdown": round(self.rolling_drawdown(), 2),
        }

# ══════════════════════════════════════════════════════════════════════════
# S11. STATS ENGINE (statistiques completes + rapports periodiques)
# ══════════════════════════════════════════════════════════════════════════

class StatsEngine:
    def __init__(self, tlog: TradeLogger):
        self.tlog = tlog
        self._last_report_day = None

    def compute(self) -> dict:
        settled = self.tlog.settled_trades()
        if not settled:
            return {"n": 0}
        rets   = [t["net_pnl"] for t in settled]
        wins   = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        n      = len(rets)
        wr     = len(wins) / n
        avg_w  = sum(wins) / len(wins) if wins else 0.0
        avg_l  = sum(losses) / len(losses) if losses else 0.0
        mean   = sum(rets) / n
        var    = sum((r - mean) ** 2 for r in rets) / n if n > 1 else 0.0
        std    = math.sqrt(var)
        downs  = [min(0.0, r) for r in rets]
        dvar   = sum(d ** 2 for d in downs) / n
        dstd   = math.sqrt(dvar)
        # Sharpe/Sortino PAR TRADE (pas annualises -- interpretation relative
        # uniquement ; l'annualisation sur du 15-min serait trompeuse).
        sharpe  = mean / std  if std  > 0 else None
        sortino = mean / dstd if dstd > 0 else None
        gp, gl  = sum(wins), -sum(losses)
        # Kelly (formule classique ; valable seulement si WR/gains stables)
        kelly = None
        if avg_w > 0 and avg_l < 0:
            b = avg_w / abs(avg_l)
            kelly = round(wr - (1 - wr) / b, 4)
        curve, peak, maxdd = 0.0, 0.0, 0.0
        curve_points = []
        for t in settled:
            curve += t["net_pnl"]; peak = max(peak, curve)
            maxdd = max(maxdd, peak - curve)
            curve_points.append({"t": t["settled_at"], "equity": round(curve, 2)})
        JsonStore.save(_p(CFG.CURVE_FILE), curve_points)
        durations = [t["holding_seconds"] for t in settled if t.get("holding_seconds")]
        return {
            "n": n, "win_rate": round(wr, 4),
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
            "expectancy": round(mean, 3),
            "sharpe_per_trade": round(sharpe, 3) if sharpe is not None else None,
            "sortino_per_trade": round(sortino, 3) if sortino is not None else None,
            "kelly_fraction": kelly,
            "average_win": round(avg_w, 2), "average_loss": round(avg_l, 2),
            "largest_win": round(max(rets), 2), "largest_loss": round(min(rets), 2),
            "max_drawdown": round(maxdd, 2),
            "net_pnl": round(sum(rets), 2),
            "avg_duration_s": round(sum(durations) / len(durations)) if durations else None,
        }

    def _period_report(self, label: str, day_filter) -> dict:
        settled = [t for t in self.tlog.settled_trades()
                   if day_filter((t.get("settled_at") or "")[:10])]
        pnl = sum(t["net_pnl"] for t in settled)
        wins = sum(1 for t in settled if t["won"])
        return {"period": label, "trades": len(settled), "wins": wins,
                "net_pnl": round(pnl, 2),
                "win_rate": round(wins / len(settled), 4) if settled else 0.0}

    def maybe_daily_report(self):
        """A chaque changement de jour UTC : rapport quotidien (+ hebdo le
        lundi, + mensuel le 1er), ecrit dans reports/ et logge."""
        today = datetime.now(timezone.utc).date()
        if self._last_report_day == today:
            return
        prev = self._last_report_day
        self._last_report_day = today
        if prev is None:
            return
        os.makedirs(_p(CFG.REPORT_DIR), exist_ok=True)
        day = prev.isoformat()
        rep = {"generated": now_iso(),
               "daily":  self._period_report(day, lambda d: d == day),
               "global": self.compute()}
        if today.weekday() == 0:
            week_start = date.fromordinal(today.toordinal() - 7).isoformat()
            rep["weekly"] = self._period_report(
                "7 derniers jours", lambda d: d >= week_start)
        if today.day == 1:
            rep["monthly"] = self._period_report(
                prev.strftime("%Y-%m"), lambda d: d.startswith(prev.strftime("%Y-%m")))
        JsonStore.save(os.path.join(_p(CFG.REPORT_DIR), f"report_{day}.json"), rep)
        log_sts.info(f"Rapport {day}: {json.dumps(rep['daily'], ensure_ascii=False)}")

    def log_summary(self):
        s = self.compute()
        if s.get("n"):
            log_sts.info(f"BILAN: n={s['n']} WR={s['win_rate']:.1%} "
                         f"PnL={s['net_pnl']:+.2f}$ PF={s['profit_factor']} "
                         f"Exp={s['expectancy']}$ MaxDD={s['max_drawdown']}$")

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
        if CFG.ONE_TRADE_PER_MKT and (ticker in posmgr.positions
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
