"""Kalshi API client: authentication, retries, and V2 order lifecycle."""

import base64
import json
import logging
import time
import uuid
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlparse

import requests

from config import CFG, daily_quarantine_blocks, prod_is_read_only

log_api = logging.getLogger("API")
log = logging.getLogger("BOT")

class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str, body: str = ""):
        self.status, self.body = status, body[:500]
        super().__init__(f"HTTP {status}: {message}")

class BrokerWriteForbidden(KalshiAPIError):
    """Une ecriture broker a ete refusee par POLITIQUE, pas par le reseau.

    Sous-classe de KalshiAPIError pour que tout appelant qui gere deja les
    erreurs API continue de fonctionner, mais distincte pour qu'un test — et
    un operateur lisant un journal — puisse separer "le broker a refuse" de
    "nous avons refuse d'appeler le broker".
    """


RETRYABLE_STATUS = {429, 500, 502, 503, 504}

POSITION_QUANTITY_FIELDS = ("position", "position_fp", "quantity", "count")
MAX_EXACT_CONTRACTS = 2**53 - 1


def portfolio_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate portfolio JSON member")
        result[key] = value
    return result


def portfolio_json_constant(value):
    raise ValueError("non-finite portfolio JSON constant")


def portfolio_identity(value, field):
    """Canonical, bounded broker identity; never coerce absence or numbers."""
    if (not isinstance(value, str) or not value or len(value) > 300
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ValueError(field + " is not a canonical broker identity")
    return value


def portfolio_integer(value, field):
    """Exact whole contracts, within the engine's lossless numeric range."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(field + " has an unsupported quantity type")
    if isinstance(value, int):
        if abs(value) > MAX_EXACT_CONTRACTS:
            raise ValueError(field + " exceeds the exact contract range")
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(field + " is not finite")
    text = str(value)
    if len(text) > 128 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", text):
        raise ValueError(field + " is not a fixed-point contract quantity")
    try:
        exact = Decimal(text)
        if not exact.is_finite() or exact != exact.to_integral_value() or abs(exact) > MAX_EXACT_CONTRACTS:
            raise ValueError(field + " is fractional or outside the exact contract range")
        return int(exact)
    except InvalidOperation as exc:
        raise ValueError(field + " is not a contract quantity") from exc


def position_quantity(row):
    if not isinstance(row, dict):
        raise ValueError("position row is not an object")
    values = [portfolio_integer(row[field], field) for field in POSITION_QUANTITY_FIELDS if field in row]
    if not values or len(set(values)) != 1:
        raise ValueError("position quantity fields are missing or contradictory")
    return values[0]


def event_position_identity(row):
    """Validate supplemental event summaries without treating them as holdings."""
    allowed = {"event_ticker", "total_cost_dollars", "total_cost_shares_fp",
               "event_exposure_dollars", "realized_pnl_dollars", "fees_paid_dollars",
               "total_cost", "total_cost_shares", "event_exposure", "realized_pnl", "fees_paid"}
    if not isinstance(row, dict) or set(row) - allowed:
        raise ValueError("unknown auxiliary event position schema")
    identity = portfolio_identity(row.get("event_ticker"), "event_ticker")
    for key, value in row.items():
        if key == "event_ticker":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError("malformed auxiliary event value")
        text = str(value)
        if len(text) > 128 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", text):
            raise ValueError("malformed auxiliary event value")
    return identity


def portfolio_json_compatible(value, field=""):
    """Normalize only after schema/identity validation, before publication.

    The wire decoder keeps lexical decimal precision until quantity checks
    finish. Whole quantities then become integers; non-quantity finite JSON
    numbers retain the legacy float interface. No Decimal escapes into order
    persistence or JSON diagnostics. Fixed-point strings remain unchanged.
    """
    quantity_fields = set(POSITION_QUANTITY_FIELDS) | {
        "fill_count", "fill_count_fp", "remaining_count", "remaining_count_fp",
        "initial_count", "initial_count_fp", "total_cost_shares", "total_cost_shares_fp"}
    if isinstance(value, Decimal):
        if field in quantity_fields:
            return portfolio_integer(value, field)
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-quantity portfolio number exceeds finite JSON range")
        return number
    if isinstance(value, dict):
        return {key: portfolio_json_compatible(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [portfolio_json_compatible(item) for item in value]
    return value

#: Verbes HTTP qui MUTENT l'etat cote broker. Tout ce qui n'est pas une
#: lecture. La liste est volontairement exhaustive plutot que limitee aux
#: verbes actuellement utilises (POST, DELETE) : une methode future qui
#: emploierait PUT ou PATCH doit etre couverte le jour ou elle est ecrite,
#: pas le jour ou quelqu'un pense a mettre a jour cette liste.
MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Verbes de LECTURE reconnus. Une methode qui n'est ni dans cette liste ni
#: dans MUTATING_HTTP_METHODS est INCLASSABLE, et une methode inclassable est
#: traitee comme mutante (voir `_is_mutating_method`).
READ_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalized_http_method(method):
    """Nom de methode canonique en majuscules, ou None si inclassable.

    `method.upper()` seul ne suffit PAS. `b"POST".upper()` vaut `b"POST"`,
    qui n'appartient pas a un ensemble de chaines : une methode passee en
    OCTETS traversait donc le butoir de transport sans etre reconnue comme
    mutante, et `requests` l'envoyait ensuite comme un POST parfaitement
    valide. Le test de politique et le test d'envoi ne regardaient pas la
    meme valeur.
    """
    if isinstance(method, str):
        text = method
    elif isinstance(method, (bytes, bytearray)):
        try:
            text = bytes(method).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            return None
    else:
        return None
    text = text.strip().upper()
    return text or None


def _is_mutating_method(method) -> bool:
    """Vrai si la methode MUTE, ou si on ne peut pas prouver qu'elle lit.

    FAIL-CLOSED. Un objet exotique (None, entier, objet avec un `.upper()`
    fantaisiste, octets non ASCII) n'est pas classable : le refuser comme une
    ecriture est le seul choix sur pour un compte de production. Une lecture
    perdue est un incident; une ecriture non gardee est un ordre.
    """
    normalized = _normalized_http_method(method)
    if normalized is None:
        return True
    return normalized not in READ_HTTP_METHODS


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

    # -- Autorisation d'ecriture broker ---------------------------------------
    def _assert_broker_write_allowed(self, operation: str) -> None:
        """INVARIANT DE SECURITE — point de controle UNIQUE des ecritures.

            "Une ecriture broker en PRODUCTION exige une autorisation
             explicite au niveau du client. L'observation LIVE en lecture
             seule n'en exige aucune."

        Toute methode qui MUTE l'etat cote broker passe par ici, et le
        transport (`_req`) le re-verifie pour tout verbe mutant : une methode
        d'ecriture future est donc couverte le jour ou elle est ecrite, meme
        si son auteur oublie d'appeler ce garde.

        L'autorisation est DELIBEREMENT distincte de ALLOW_ORDER_SUBMISSION,
        LIVE_TRADING, LIVE_TRADING_CONFIRMED et MODEL_APPROVED. Chacune de
        celles-la peut etre ouverte pour une raison legitime sans que
        quiconque ait decide qu'une ecriture reelle sur le compte de
        production est autorisee. Aucune d'elles ne peut donc armer une
        ecriture LIVE a la place de celle-ci.

        DEMO est inchange : ce garde ne concerne que l'environnement de
        production.
        """
        if self.env == "demo":
            return
        # PRIORITE MAXIMALE — DOMINANCE DE LA LECTURE SEULE.
        # Teste AVANT toute autre autorisation, y compris
        # LIVE_BROKER_WRITES_AUTHORIZED. En PROD_ACCESS_MODE=READ_ONLY aucune
        # combinaison de drapeaux ne peut produire une mutation : ni
        # ALLOW_ORDER_SUBMISSION, ni LIVE_TRADING, ni LIVE_TRADING_CONFIRMED,
        # ni MODEL_APPROVED_FOR_LIVE, ni LIVE_BROKER_WRITES_AUTHORIZED, ni
        # l'etat du coupe-circuit. `prod_is_read_only` est vrai sauf si
        # CAPITAL a ete demande EXPLICITEMENT : une valeur absente, vide ou
        # mal orthographiee laisse donc la production en lecture seule.
        if prod_is_read_only():
            raise BrokerWriteForbidden(
                0, f"{operation} REFUSE au niveau client: "
                   f"PROD_ACCESS_MODE n'est pas CAPITAL, donc l'environnement "
                   f"{self.env!r} est en LECTURE SEULE. Aucun drapeau de "
                   f"trading ne peut lever cette interdiction. Aucune requete "
                   f"reseau mutante n'a ete emise.")
        if not CFG.LIVE_BROKER_WRITES_AUTHORIZED:
            raise BrokerWriteForbidden(
                0, f"{operation} REFUSE au niveau client: environnement "
                   f"{self.env!r} (production) et "
                   f"LIVE_BROKER_WRITES_AUTHORIZED n'est pas explicitement "
                   f"vrai. Le LIVE est en LECTURE SEULE par construction. "
                   f"Aucune requete reseau mutante n'a ete emise.")

    # -- Requete avec retry/backoff ------------------------------------------
    def _req(self, method: str, path: str, *, retries: int = 3, **kw) -> dict:
        # BUTOIR DE TRANSPORT. Place AVANT tout le reste (y compris la
        # verification de cle) pour qu'une ecriture LIVE non autorisee soit
        # refusee quelle que soit la raison pour laquelle elle serait sinon
        # partie. C'est le point le plus bas que toute mutation doit
        # traverser : aucun appelant, present ou futur, ne peut l'eviter.
        if _is_mutating_method(method):
            self._assert_broker_write_allowed(
                f"{_normalized_http_method(method) or repr(method)} {path}")
        # La politique d'abord, la validation de type ensuite : sur un compte
        # de production non autorise, la reponse doit etre le REFUS de
        # politique, pas une erreur de type qui masquerait la raison reelle.
        method = _normalized_http_method(method)
        if method is None:
            raise KalshiAPIError(
                0, f"methode HTTP inutilisable pour {path}: valeur non "
                   f"classable. Aucune requete emise.")
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
                r = self.session.request(method, url,
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
                # Only the two read-enumeration routes use strict decoding.
                # A duplicate cursor or envelope must not be discarded by
                # JSON decoding before the collector can validate it.
                if method == "GET" and path in ("/portfolio/positions", "/portfolio/orders"):
                    return r.json(object_pairs_hook=portfolio_json_object,
                                  parse_constant=portfolio_json_constant,
                                  parse_float=Decimal) if r.text.strip() else {}
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
        cents ramene a NOTRE cote).

        BUTOIR DE DERNIER RECOURS. Les gardes de politique vivent en amont,
        dans OrderManager.place_and_track. Elles sont relues ICI parce que
        `place_and_track` n'est pas le seul appelant possible de cette
        methode : un outil, un script d'integration, une session de debug ou
        un appelant futur peut tenir une instance de client et appeler
        create_order directement, en contournant toute la sequence de gardes.
        Un inventaire des chemins d'ecriture broker n'a de valeur que si le
        POINT DE PASSAGE COMMUN refuse aussi. Ce butoir n'est pas une
        duplication defensive : c'est le seul endroit qu'AUCUN chemin ne peut
        eviter.

        `cancel_order` n'est deliberement PAS garde de la meme facon : annuler
        REDUIT l'exposition. Un coupe-circuit qui empecherait d'annuler
        piegerait un ordre ouvert au lieu de proteger le compte."""
        # Refus INDEPENDANT par methode, en plus du butoir de transport : si
        # `_req` changeait un jour, cette methode refuserait encore.
        self._assert_broker_write_allowed("create_order")
        if not CFG.ALLOW_ORDER_SUBMISSION:
            raise KalshiAPIError(
                0, "create_order refuse au niveau client: "
                   "ALLOW_ORDER_SUBMISSION=false. Aucun appel reseau emis.")
        if CFG.KILL_SWITCH:
            raise KalshiAPIError(
                0, "create_order refuse au niveau client: KILL_SWITCH actif. "
                   "Aucun appel reseau emis.")
        if daily_quarantine_blocks(ticker):
            raise KalshiAPIError(
                0, f"create_order refuse au niveau client: {ticker!r} est un "
                   f"marche BTC quotidien et l'oracle de reglement n'est pas "
                   f"approuve. Aucun appel reseau emis.")
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
        # Une ANNULATION mute aussi l'etat cote broker. Elle reduit
        # l'exposition, donc le coupe-circuit ne la bloque pas -- mais sur un
        # compte de PRODUCTION non autorise en ecriture, elle reste une
        # mutation d'un compte reel et doit etre refusee comme les autres.
        # "Lecture seule" ne signifie pas "sauf quand cela nous arrange".
        self._assert_broker_write_allowed("cancel_order")
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

    #: Listing officiel des ordres du portefeuille (Trade API v2). Kalshi
    #: n'expose PAS de filtre serveur sur client_order_id : le listing est
    #: donc restreint cote serveur par ticker/status (ce qui est supporte)
    #: puis filtre localement sur le client_order_id rendu dans chaque
    #: ordre. C'est la seule facon documentee de retrouver un ordre dont on
    #: n'a jamais recu l'order_id (POST ambigu).
    ORDERS_LIST_PATH = "/portfolio/orders"
    #: Enveloppes de listing ACCEPTEES, en dur. Toute autre forme est une
    #: reponse que ce client ne sait pas lire -- jamais une liste vide.
    #: "data" a ete retire: c'etait une supposition, pas une forme
    #: documentee par Kalshi, et elle elargissait la surface acceptee.
    ORDERS_ENVELOPE_KEYS = ("orders",)

    def _portfolio_pages(self, path, *, envelopes, params, max_pages,
                         row_identity, extra_keys=(), cursor_required=False,
                         log_name="portfolio"):
        """Complete validated enumeration, never an atomic broker snapshot.

        Return only after a recognized terminal cursor. Reject unfamiliar
        top-level fields rather than silently overlooking a new pagination
        indicator. Orders require a string cursor; positions may omit their
        documented optional cursor. A supplied null is never a valid cursor.
        A concurrent account change can still move rows between pages without
        an API snapshot token; this method does not prove a broker freeze.
        """
        limit = params["limit"]
        if type(limit) is not int or not 1 <= limit <= 1000 or \
                type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise KalshiAPIError(0, "listing incoherent: invalid page bounds")
        out, cursor, seen_cursors, seen_rows, envelope = [], "", set(), set(), None
        for page_number in range(max_pages):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            response = self._req("GET", path, params=query)
            if not isinstance(response, dict):
                raise KalshiAPIError(0, "listing incoherent: response must be an object")
            present = [key for key in envelopes if key in response]
            if len(present) != 1 or set(response) - set(envelopes) - {"cursor"} - set(extra_keys):
                raise KalshiAPIError(0, "listing incoherent: unknown or conflicting envelope/pagination schema")
            if envelope is not None and present[0] != envelope:
                raise KalshiAPIError(0, "listing incoherent: envelope changed between pages")
            envelope = present[0]
            rows = response[envelope]
            if not isinstance(rows, list) or len(rows) > limit:
                raise KalshiAPIError(0, "listing incoherent: entries are not a bounded list")
            for extra in extra_keys:
                if extra in response and (not isinstance(response[extra], list)
                        or any(not isinstance(row, dict) for row in response[extra])):
                    raise KalshiAPIError(0, "listing incoherent: malformed auxiliary positions")
                try:
                    auxiliary_ids = [event_position_identity(row) for row in response.get(extra, [])]
                    if len(set(auxiliary_ids)) != len(auxiliary_ids):
                        raise ValueError("duplicate auxiliary event identity")
                except (ValueError, TypeError, OverflowError) as exc:
                    raise KalshiAPIError(0, "listing incoherent: " + str(exc)) from exc
            for row in rows:
                try:
                    identity = row_identity(row)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise KalshiAPIError(0, "listing incoherent: " + str(exc)) from exc
                if identity in seen_rows:
                    raise KalshiAPIError(0, "listing incoherent: duplicate row identity across enumeration")
                if any(field in params and row.get(field) != params[field]
                       for field in ("ticker", "status")):
                    raise KalshiAPIError(0, "listing incoherent: row contradicts requested account filter")
                if any(key in row and (type(row[key]) is not int or row[key] != 0)
                       for key in ("subaccount", "subaccount_number")):
                    raise KalshiAPIError(0, "listing incoherent: row contradicts primary subaccount scope")
                seen_rows.add(identity)
                try:
                    out.append(portfolio_json_compatible(row))
                except (ValueError, OverflowError) as exc:
                    raise KalshiAPIError(0, "listing incoherent: " + str(exc)) from exc
            if page_number == 0:
                try:
                    self._log_raw_once(log_name, portfolio_json_compatible(response))
                except (ValueError, OverflowError) as exc:
                    raise KalshiAPIError(0, "listing incoherent: " + str(exc)) from exc
            if "cursor" not in response and cursor_required:
                raise KalshiAPIError(0, "listing incoherent: required cursor missing")
            next_cursor = response.get("cursor", "")
            if next_cursor == "":
                return out
            if (not isinstance(next_cursor, str) or len(next_cursor) > 4096
                    or next_cursor != next_cursor.strip()
                    or any(ord(c) < 33 or ord(c) > 126 for c in next_cursor)):
                raise KalshiAPIError(0, "listing incoherent: cursor type or format invalid")
            if next_cursor in seen_cursors:
                raise KalshiAPIError(0, "listing incoherent: cursor ne progresse pas (cycle)")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise KalshiAPIError(0, "listing tronque: page limit reached with a live cursor")

    @staticmethod
    def _order_listing_identity(row):
        if not isinstance(row, dict):
            raise ValueError("order row is not an object")
        identity = portfolio_identity(row.get("order_id"), "order_id")
        if "id" in row and portfolio_identity(row["id"], "id") != identity:
            raise ValueError("contradictory order identity aliases")
        portfolio_identity(row.get("ticker"), "ticker")
        client_ids = [portfolio_identity(row[key], key) for key in ("client_order_id", "client_id") if key in row]
        if not client_ids or len(set(client_ids)) != 1:
            raise ValueError("order client identity missing or contradictory")
        if row.get("status") not in ("resting", "executed", "canceled", "pending"):
            raise ValueError("unknown order status")
        if row.get("side") not in ("yes", "no"):
            raise ValueError("unknown order side")
        if "outcome_side" in row and row["outcome_side"] != row["side"]:
            raise ValueError("contradictory order outcome aliases")
        if "action" in row and row["action"] not in ("buy", "sell"):
            raise ValueError("unknown order action")
        if "book_side" in row:
            if row["book_side"] not in ("bid", "ask"):
                raise ValueError("unknown order book side")
            if "action" in row:
                expected = "bid" if (row["side"] == "yes") == (row["action"] == "buy") else "ask"
                if row["book_side"] != expected:
                    raise ValueError("contradictory order action/book/outcome binding")
        quantities = {}
        for names in (("fill_count", "fill_count_fp"), ("remaining_count", "remaining_count_fp"),
                      ("initial_count", "initial_count_fp")):
            counts = [portfolio_integer(row[name], name) for name in names if name in row]
            if any(count < 0 for count in counts) or len(set(counts)) > 1:
                raise ValueError("order quantity is negative or contradictory")
            if counts:
                quantities[names[0]] = counts[0]
        if "fill_count" not in quantities or "remaining_count" not in quantities:
            raise ValueError("order fill/remaining quantities are missing")
        # Initial count may refer to a pre-amendment size. Do not invent a
        # conservation equation without the complete amendment history.
        if row["status"] == "executed" and quantities["remaining_count"] != 0:
            raise ValueError("executed order still has remaining quantity")
        if row["status"] == "resting" and quantities["remaining_count"] <= 0:
            raise ValueError("resting order has no remaining quantity")
        return identity

    def list_orders(self, *, ticker: str = None, status: str = None,
                    limit: int = 200, max_pages: int = 10) -> list:
        """All matching known-schema orders, or an explicit read failure."""
        # Both collectors cover primary subaccount 0, matching create_order's
        # default. A full organizational inventory is a separate proof.
        params = {"limit": limit, "subaccount": 0}
        try:
            if ticker is not None:
                params["ticker"] = portfolio_identity(ticker, "ticker filter")
            if status is not None:
                if status not in ("resting", "executed", "canceled", "pending"):
                    raise ValueError("unknown order status filter")
                params["status"] = status
        except ValueError as exc:
            raise KalshiAPIError(0, "listing incoherent: " + str(exc)) from exc
        return self._portfolio_pages(self.ORDERS_LIST_PATH,
            envelopes=self.ORDERS_ENVELOPE_KEYS, params=params, max_pages=max_pages,
            row_identity=self._order_listing_identity, cursor_required=True,
            log_name="list_orders")

    def find_orders_by_client_order_id(self, client_order_id: str, *,
                                       ticker: str = None) -> list:
        """TOUS les ordres portant ce client_order_id (0, 1 ou plusieurs).

        Ne renvoie jamais « rien » sur une erreur : une panne de lecture
        remonte en KalshiAPIError pour rester fail-closed cote appelant.
        """
        cid = str(client_order_id or "").strip()
        if not cid:
            raise KalshiAPIError(0, "recherche par client_order_id vide")
        matches = [o for o in self.list_orders(ticker=ticker)
                   if pick(o, "client_order_id", "client_id") == cid]
        if not matches:
            # Current-order listings exclude finalized history beyond the
            # exchange retention cutoff. No timestamp/snapshot-bound history
            # proof is available here. Keep ambiguous intents UNAVAILABLE;
            # repeated incomplete absence readings cannot authorize a retry.
            raise KalshiAPIError(0, "order absence unproven: historical retention scope unavailable")
        return matches

    def get_positions(self, *, limit: int = 200, max_pages: int = 50) -> list:
        """Complete known-schema market positions; uncertainty raises.

        Event aggregates are not substituted for individual market positions.
        An exception preserves callers' reconciliation halt; no partial page
        collection or unfamiliar envelope can be presented as a flat account.
        """
        def identity(row):
            if not isinstance(row, dict):
                raise ValueError("position row is not an object")
            ticker = portfolio_identity(row.get("ticker"), "ticker")
            position_quantity(row)
            return ticker
        return self._portfolio_pages("/portfolio/positions",
            envelopes=("market_positions", "positions"), params={"limit": limit, "subaccount": 0},
            max_pages=max_pages, row_identity=identity,
            extra_keys=("event_positions",), log_name="positions")
