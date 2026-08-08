"""Kalshi API client: authentication, retries, and V2 order lifecycle."""

import base64
import json
import logging
import re
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests

from config import CFG

log_api = logging.getLogger("API")
log = logging.getLogger("BOT")

class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str, body: str = ""):
        self.status, self.body = status, body[:500]
        super().__init__(f"HTTP {status}: {message}")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# ══════════════════════════════════════════════════════════════════════════
# S5b. TELEMETRIE DES APPELS API (OBSERVABILITE UNIQUEMENT)
#
# Un evenement structure par TENTATIVE. Aucune requete supplementaire n'est
# emise, aucun parametre, aucune politique de retry, aucun timeout et aucune
# semantique d'exception ne change : ce bloc ne fait que decrire ce qui se
# passe deja.
# ══════════════════════════════════════════════════════════════════════════

#: Classes de resultat. Ensemble FERME : une etiquette de metrique doit avoir
#: une cardinalite bornee, connue a l'avance.
RESULT_CLASSES = frozenset({
    "SUCCESS", "HTTP_ERROR", "TIMEOUT", "AUTH_ERROR", "RATE_LIMIT",
    "TRANSPORT_ERROR", "PARSE_ERROR", "UNEXPECTED_ERROR",
})

#: Catalogue des chemins sortants : (methode, motif, operation, categorie).
#: `operation` est la methode publique proprietaire du chemin ; `categorie`
#: est l'etiquette bornee utilisee par les metriques. Les deux viennent de
#: cette table unique, donc un nouvel endpoint ne peut pas produire
#: silencieusement une etiquette non bornee. '/*' = exactement un segment.
_ENDPOINT_TABLE = (
    ("GET",    "/markets",                   "get_markets",   "markets_list"),
    ("GET",    "/markets/*",                 "get_market",    "market_detail"),
    ("GET",    "/portfolio/balance",         "get_balance",   "balance"),
    ("GET",    "/portfolio/positions",       "get_positions", "positions"),
    ("GET",    "/portfolio/fills",           "get_fills",     "fills"),
    ("GET",    "/portfolio/orders/*",        "get_order",     "order_detail"),
    ("POST",   "/portfolio/events/orders",   "create_order",  "orders_create"),
    ("DELETE", "/portfolio/events/orders/*", "cancel_order",  "orders_cancel"),
)

#: Categories connues + le repli borne.
ENDPOINT_CATEGORIES = frozenset(
    [entry[3] for entry in _ENDPOINT_TABLE] + ["other"])

#: Champs autorises dans un evenement de telemetrie. LISTE BLANCHE : tout ce
#: qui n'est pas explicitement approuve est supprime, donc un champ ajoute
#: plus tard et transportant un secret ne peut pas atteindre un log par
#: simple oubli. La valeur par defaut est la redaction, jamais la
#: publication.
TELEMETRY_FIELDS = frozenset({
    "event", "operation", "http_method", "endpoint_category", "environment",
    "operation_id", "attempt_number", "max_attempts", "latency_ms",
    "operation_latency_ms", "result_class", "http_status",
    "kalshi_error_code", "response_size_class", "retry_scheduled",
    "final_attempt", "resource_id", "exception_type",
})

#: Code d'erreur Kalshi extrait du corps. Le motif borne la valeur (64
#: caracteres, alphabet sur), donc un corps hostile ne peut ni faire exploser
#: la cardinalite ni injecter de contenu arbitraire dans un log.
_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_.\-]{1,64})"')


def classify_endpoint(method: str, path: str) -> tuple:
    """(operation, categorie, identifiant_de_ressource) pour un chemin.

    L'identifiant (ticker, order_id) est un champ de LOG a des fins de
    diagnostic : il ne doit jamais servir d'etiquette de metrique, sous peine
    de cardinalite non bornee.
    """
    base = (path or "").split("?", 1)[0].rstrip("/") or "/"
    verb = (method or "").upper()
    for entry_method, pattern, operation, category in _ENDPOINT_TABLE:
        if entry_method != verb:
            continue
        if pattern.endswith("/*"):
            head = pattern[:-2]
            if base.startswith(head + "/"):
                tail = base[len(head) + 1:]
                if tail and "/" not in tail:
                    return operation, category, tail
        elif base == pattern:
            return operation, category, None
    return "unknown", "other", None


def response_size_class(size) -> str:
    """Taille de reponse en classes bornees (jamais l'octet exact)."""
    if not isinstance(size, int) or size < 0:
        return "unknown"
    if size == 0:
        return "empty"
    if size < 1024:
        return "small"
    if size < 65536:
        return "medium"
    return "large"


def kalshi_error_code(body: str):
    """Code d'erreur Kalshi si le corps en contient un, sinon None."""
    match = _ERROR_CODE_RE.search(body or "")
    return match.group(1) if match else None


def redact_telemetry(fields: dict) -> dict:
    """Ne conserve que les champs de la liste blanche.

    Cle absente de TELEMETRY_FIELDS -> champ supprime. Aucune cle d'en-tete,
    de signature, de payload ou d'authentification n'y figure, donc aucune ne
    peut etre journalisee, y compris par accident.
    """
    return {k: v for k, v in fields.items() if k in TELEMETRY_FIELDS}

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

    def __init__(self, env: str = "demo", cache_enabled: Optional[bool] = None):
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
        # ── P8 : caches de requete TTL (desactives par defaut). Un cache
        # ── n'est rempli qu'avec des resultats VALIDES (jamais None / liste
        # ── vide) : une erreur API transitoire ne masque pas un
        # ── retablissement. clear_caches() est appele au debut de chaque
        # ── cycle (ExecutionEngine) : un cache ne traverse jamais un cycle.
        self.cache_enabled = (CFG.API_CACHE_ENABLED
                              if cache_enabled is None else bool(cache_enabled))
        self._balance_cache = None
        self._markets_cache = None
        if self.cache_enabled:
            from api_cache import TTLCache
            self._balance_cache = TTLCache(CFG.API_BALANCE_TTL_S)
            self._markets_cache = TTLCache(CFG.API_MARKET_TTL_S)

    def clear_caches(self) -> None:
        """Vide les caches de requete (appele en debut de cycle)."""
        if self.cache_enabled:
            self._balance_cache.clear()
            self._markets_cache.clear()

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

    # -- Telemetrie (observabilite uniquement) --------------------------------
    def _log_api_attempt(self, **fields) -> None:
        """Journalise UNE tentative d'appel API. N'appelle jamais le reseau.

        Toute exception de journalisation est avalee : la telemetrie ne doit
        jamais pouvoir faire echouer un appel qui, lui, a reussi.
        """
        try:
            event = redact_telemetry(
                {"event": "kalshi_api_call", "environment": self.env, **fields})
            log_api.info(
                f"[API_RESULT] {event.get('operation')} "
                f"{event.get('result_class')} "
                f"attempt={event.get('attempt_number')}/"
                f"{event.get('max_attempts')} "
                f"status={event.get('http_status')} "
                f"{event.get('latency_ms')}ms",
                extra=event)
        except Exception:                                     # noqa: BLE001
            pass

    # -- Requete avec retry/backoff ------------------------------------------
    def _req(self, method: str, path: str, *, retries: int = 3, **kw) -> dict:
        if self._pk is None and path.startswith("/portfolio"):
            # Aucune tentative reseau n'a lieu : rien a mesurer, donc rien a
            # journaliser. Compter ceci comme un appel fausserait le taux
            # d'echec de l'API.
            raise KalshiAPIError(
                0, f"{method} {path}: requete authentifiee IMPOSSIBLE — cle "
                   f"RSA non chargee (cle absente/non-PEM ou dependance "
                   f"manquante). Verifier KALSHI_DEMO_PRIVATE_KEY (PEM "
                   f"complet avec les lignes -----BEGIN/END-----) et le "
                   f"paquet 'cryptography'.")
        url = self.base_url + path
        operation, category, resource_id = classify_endpoint(method, path)
        operation_id = uuid.uuid4().hex[:12]
        max_attempts = retries + 1
        op_started = time.monotonic()
        def _emit(attempt, started, result_class, **extra):
            """Un evenement pour la tentative courante."""
            self._log_api_attempt(
                operation=operation, http_method=method.upper(),
                endpoint_category=category, resource_id=resource_id,
                operation_id=operation_id, attempt_number=attempt,
                max_attempts=max_attempts,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
                operation_latency_ms=round(
                    (time.monotonic() - op_started) * 1000, 3),
                result_class=result_class, **extra)

        attempt, delay = 0, 1.0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                r = self.session.request(method.upper(), url,
                                         headers=self._sign_headers(method, url),
                                         timeout=15, **kw)
            except (requests.Timeout, requests.ConnectionError) as e:
                _emit(attempt, started,
                      "TIMEOUT" if isinstance(e, requests.Timeout)
                      else "TRANSPORT_ERROR",
                      exception_type=type(e).__name__,
                      retry_scheduled=attempt <= retries,
                      final_attempt=attempt > retries)
                if attempt > retries:
                    raise KalshiAPIError(0, f"reseau: {e}")
                log_api.warning(f"{method} {path}: {type(e).__name__} -- "
                                f"retry {attempt}/{retries} dans {delay:.0f}s")
                time.sleep(delay); delay = min(delay * 2, 8); continue
            except Exception as e:                            # noqa: BLE001
                # Chemin d'exception jusqu'ici invisible : SSLError hors
                # ConnectionError, TooManyRedirects, InvalidURL... `raise` nu,
                # donc propagation et traceback rigoureusement inchanges.
                _emit(attempt, started, "UNEXPECTED_ERROR",
                      exception_type=type(e).__name__,
                      retry_scheduled=False, final_attempt=True)
                raise

            body = getattr(r, "content", None)
            size_class = response_size_class(
                len(body) if isinstance(body, (bytes, bytearray)) else None)

            if r.status_code in RETRYABLE_STATUS and attempt <= retries:
                wait = delay
                if r.status_code == 429:
                    try: wait = max(wait, float(r.headers.get("Retry-After", delay)))
                    except ValueError: pass
                _emit(attempt, started,
                      "RATE_LIMIT" if r.status_code == 429 else "HTTP_ERROR",
                      http_status=r.status_code,
                      kalshi_error_code=kalshi_error_code(getattr(r, "text", "")),
                      response_size_class=size_class,
                      retry_scheduled=True, final_attempt=False)
                log_api.warning(f"{method} {path}: HTTP {r.status_code} -- "
                                f"retry {attempt}/{retries} dans {wait:.0f}s")
                time.sleep(wait); delay = min(delay * 2, 8); continue

            self.last_http_status = r.status_code
            if r.status_code == 410 and "deprecated" in (r.text or "").lower():
                _emit(attempt, started, "HTTP_ERROR", http_status=410,
                      kalshi_error_code=kalshi_error_code(getattr(r, "text", "")),
                      response_size_class=size_class,
                      retry_scheduled=False, final_attempt=True)
                raise KalshiAPIError(
                    410, f"{method} {path}: ENDPOINT V1 OBSOLETE cote Kalshi "
                         f"-- migrer ce chemin vers son equivalent V2 "
                         f"(cf. docs.kalshi.com)", r.text)
            if r.status_code >= 400:
                _emit(attempt, started,
                      "AUTH_ERROR" if r.status_code in (401, 403)
                      else ("RATE_LIMIT" if r.status_code == 429
                            else "HTTP_ERROR"),
                      http_status=r.status_code,
                      kalshi_error_code=kalshi_error_code(getattr(r, "text", "")),
                      response_size_class=size_class,
                      retry_scheduled=False, final_attempt=True)
                raise KalshiAPIError(r.status_code, f"{method} {path}", r.text)

            try:
                payload = r.json() if r.text.strip() else {}
            except ValueError:
                _emit(attempt, started, "PARSE_ERROR",
                      http_status=r.status_code,
                      response_size_class=size_class,
                      retry_scheduled=False, final_attempt=True)
                raise KalshiAPIError(r.status_code, f"{method} {path}: JSON invalide", r.text)
            _emit(attempt, started, "SUCCESS", http_status=r.status_code,
                  response_size_class=size_class,
                  retry_scheduled=False, final_attempt=True)
            return payload

    def _log_raw_once(self, kind: str, payload: dict):
        """Logge UNE FOIS la reponse brute de chaque type d'appel critique,
        pour verifier les noms de champs reels de l'API."""
        if kind not in self._raw_logged:
            self._raw_logged.add(kind)
            log_api.info(f"[RAW:{kind}] {json.dumps(payload, ensure_ascii=False)[:800]}")

    # -- Endpoints -----------------------------------------------------------
    def get_markets(self, series: str, status: str = "open", limit: int = 50) -> list:
        """Liste les marches d'une serie. P8 : cache TTL par (serie, status,
        limit) — les listes VIDES ne sont jamais cachees (vide = soit serie
        sans marche, soit erreur API : on ne fige pas une erreur)."""
        key = (series, status, limit)
        if self.cache_enabled:
            hit = self._markets_cache.get(key)
            if hit is not None:
                log_api.debug(f"get_markets({series}): cache hit")
                return hit
        try:
            r = self._req("GET", "/markets",
                          params={"series_ticker": series, "status": status, "limit": limit})
            markets = r.get("markets", []) or []
        except KalshiAPIError as e:
            log_api.error(f"get_markets({series}): {e}")
            return []
        if self.cache_enabled and markets:
            self._markets_cache.set(key, markets)
        return markets

    def get_market(self, ticker: str) -> dict:
        try:
            r = self._req("GET", f"/markets/{ticker}")
            return r.get("market", r) or {}
        except KalshiAPIError as e:
            log_api.warning(f"get_market({ticker}): {e}")
            return {}

    def get_balance(self) -> Optional[float]:
        """Solde du compte en $. P8 : cache TTL par cycle — jamais de
        resultat None (erreur API) mis en cache."""
        if self.cache_enabled:
            hit = self._balance_cache.get("balance")
            if hit is not None:
                log_api.debug("get_balance: cache hit")
                return hit
        bal = self._fetch_balance()
        if self.cache_enabled and bal is not None:
            self._balance_cache.set("balance", bal)
        return bal

    def _fetch_balance(self) -> Optional[float]:
        """Champ 'balance' attendu en cents (a verifier)."""
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


