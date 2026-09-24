"""
btc_context.py — v2 (2026-07-12)
Acquisition et normalisation des donnees BTC pour le modele 15 minutes.

CE MODULE NE PREND AUCUNE DECISION DE TRADING. Il retourne un objet
structure (BtcMarketContext) decrivant l'etat du marche et la QUALITE des
donnees. Si moins de deux sources spot valides sont disponibles, le contexte
est invalide et AUCUNE probabilite ne pourra etre calculee en aval.

Sources par defaut (publiques, sans cle) : Coinbase, Kraken, Bitstamp pour
le spot ; Binance pour les bougies 1 minute. Toutes les sources sont
INJECTABLES pour les tests hors-ligne (aucun test ne touche le reseau).

Aucun secret dans ce fichier.
"""

import math
import os
import time
import logging
import statistics
from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
from typing import Optional, Callable

log = logging.getLogger("BTCCTX")

# ── Parametres (env-surchargables cote appelant si besoin) ───────────────────
HTTP_TIMEOUT_S      = 5.0
MAX_RETRIES         = 1            # retry LIMITE par source
CACHE_TTL_S         = 10.0         # cache court : donnees "ultra fraiches"
MAX_PRICE_AGE_S     = 90.0         # au-dela : donnee PERIMEE
MAX_DISPERSION_PCT  = 0.5          # ecart max entre exchanges (aberrant sinon)
MIN_VALID_SOURCES   = 2
MIN_KLINES          = 11           # pour rendement 10m + vol realisee
KLINE_INTERVAL_S    = 60
MAX_KLINE_AGE_S     = 180.0         # existing fresh-data limit; never enlarged
# Binding to the actual fixed adapter request, not to a caller's "fresh" label.
KLINE_ORIGINS = {
    "binance": ("BTCUSDT", "https://api.binance.com/api/v3/klines"),
    "kraken": ("XBTUSD", "https://api.kraken.com/0/public/OHLC"),
    "coinbase": ("BTC-USD", "https://api.exchange.coinbase.com/products/BTC-USD/candles"),
}


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _validate_klines(rows, now, expected_provider=None, require_min=True):
    """All-or-nothing validation of normalized CLOSED one-minute OHLCV.

    No row filtering, sorting, interpolation, timestamp repair or provenance
    inference is permitted here. The adapter must bind every row to its fixed
    endpoint/product/interval request and to the request's observation time.
    """
    if not _finite_number(now) or now <= 0:
        return "clock_invalid"
    if not isinstance(rows, list) or not rows:
        return "payload_invalid"
    previous = None
    origin = None
    for row in rows:
        if not isinstance(row, dict):
            return "row_invalid"
        if any(not _finite_number(row.get(k)) for k in
               ("ts", "open", "high", "low", "close", "volume", "close_ts")):
            return "numeric_invalid"
        ts = row["ts"]
        if ts <= 0 or ts % KLINE_INTERVAL_S != 0:
            return "timestamp_alignment_invalid"
        if previous is not None and ts - previous != KLINE_INTERVAL_S:
            return "cadence_invalid"
        previous = ts
        if row["close_ts"] != ts + KLINE_INTERVAL_S:
            return "close_time_invalid"
        if row.get("closed") is not True or row["close_ts"] > now:
            return "not_closed_or_future"
        if (min(row[k] for k in ("open", "high", "low", "close")) <= 0
                or row["volume"] < 0
                or not row["low"] <= min(row["open"], row["close"])
                or not max(row["open"], row["close"]) <= row["high"]):
            return "ohlcv_invalid"
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            return "provenance_missing"
        provider = provenance.get("provider")
        if not isinstance(provider, str) or provider not in KLINE_ORIGINS:
            return "provenance_provider_invalid"
        product, endpoint = KLINE_ORIGINS[provider]
        if (expected_provider is not None and provider != expected_provider
                or provenance.get("product") != product
                or provenance.get("endpoint") != endpoint
                or type(provenance.get("interval_s")) is not int
                or provenance["interval_s"] != KLINE_INTERVAL_S
                or provenance.get("timestamp_semantics") != "open_utc_seconds"):
            return "provenance_binding_invalid"
        observed = provenance.get("observed_ts")
        if not _finite_number(observed) or not row["close_ts"] <= observed <= now:
            return "provenance_observation_invalid"
        if origin is not None and provenance != origin:
            return "provenance_mixed"
        origin = provenance
    if now - rows[-1]["ts"] > MAX_KLINE_AGE_S:
        return "stale"
    if require_min and len(rows) < MIN_KLINES:
        return f"insufficient({len(rows)}/{MIN_KLINES})"
    return None


def _transport_complete(meta):
    return (isinstance(meta, dict)
            and set(meta).issubset({"http_status", "elapsed_ms", "error"})
            and meta.get("http_status") == 200
            and "error" in meta and meta["error"] is None)


def _provider_decimal(value):
    # Keep exact wire decimals until sign/range checks have completed. JSON
    # numeric tokens are decoded as Decimal; exchange decimal strings remain
    # exact too. This prevents underflow and rounding from repairing bad OHLCV.
    if type(value) not in (int, float, str, Decimal):
        raise ValueError("numeric_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("numeric_invalid") from exc
    if not number.is_finite():
        raise ValueError("numeric_invalid")
    return number


def _provider_number(value):
    exact = _provider_decimal(value)
    result = float(exact)
    if not math.isfinite(result) or (exact != 0 and result == 0):
        raise ValueError("numeric_overflow_or_underflow")
    return result


def _normalize_candles(rows, provider, observed_ts, limit):
    """Normalize documented provider schemas, excluding only the open tail.

    Kraken explicitly says its final row is not yet committed; always exclude
    that row, even if the request straddles a minute boundary. For other
    adapters the request-start clock proves closure. Validate every response
    row before tail selection, so malformed data cannot be silently discarded.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("payload_invalid")
    product, endpoint = KLINE_ORIGINS[provider]
    provenance = {"provider": provider, "product": product, "endpoint": endpoint,
                  "interval_s": KLINE_INTERVAL_S,
                  "timestamp_semantics": "open_utc_seconds",
                  "observed_ts": observed_ts}
    out = []
    for raw in rows:
        width = {"binance": 12, "kraken": 8, "coinbase": 6}[provider]
        if not isinstance(raw, list) or len(raw) != width:
            raise ValueError("row_schema_invalid")
        # Wire timestamps are integer epochs in all three documented schemas.
        # Do not float-coerce strings/fractions: rounding can erase malformed
        # timing before the minute-grid validator sees it.
        if type(raw[0]) is not int:
            raise ValueError("timestamp_wire_type_invalid")
        if provider == "binance" and type(raw[6]) is not int:
            raise ValueError("close_timestamp_wire_type_invalid")
        exact = [_provider_decimal(x) for x in raw]
        indices = {"binance": (1, 2, 3, 4, 5),
                   "kraken": (1, 2, 3, 4, 6),
                   "coinbase": (3, 2, 1, 4, 5)}[provider]
        eo, eh, el, ec, ev = [exact[i] for i in indices]
        if (min(eo, eh, el, ec) <= 0 or ev < 0
                or el > min(eo, ec) or max(eo, ec) > eh):
            raise ValueError("exact_ohlcv_invalid")
        values = [_provider_number(x) for x in exact]
        if provider == "binance":
            ts = values[0] / 1000.0
            o, h, l, c, v = values[1:6]
            if values[6] != values[0] + 60000 - 1:
                raise ValueError("provider_close_time_invalid")
        elif provider == "kraken":
            ts, o, h, l, c, _, v, _ = values
        else:
            ts, l, h, o, c, v = values
        out.append({"ts": ts, "open": o, "high": h, "low": l,
                    "close": c, "volume": v, "close_ts": ts + 60,
                    "closed": True, "provenance": dict(provenance)})
    if provider == "coinbase":
        # Coinbase returns reverse chronology. Require that documented order;
        # arbitrary sorting would hide duplicated or out-of-order responses.
        out.reverse()
    # Validate the complete response, including its expected open tail, against
    # a structural-only future boundary. Actual closure is enforced below.
    structural_now = max(observed_ts, out[-1]["close_ts"])
    for row in out:
        row["provenance"]["observed_ts"] = structural_now
    problem = _validate_klines(out, structural_now, provider, require_min=False)
    if problem:
        raise ValueError(problem)
    for row in out:
        row["provenance"]["observed_ts"] = observed_ts
    boundary = math.floor(observed_ts / 60) * 60
    if out[-1]["ts"] > boundary:
        raise ValueError("future_candle")
    if provider == "kraken" or out[-1]["ts"] == boundary:
        out = out[:-1]
    problem = _validate_klines(out, observed_ts, provider, require_min=False)
    if problem:
        raise ValueError(problem)
    return out[-limit:]

# P8 : cache PAR CYCLE (BTC_CONTEXT_CYCLE_CACHE=1). Le contexte BTC est
# calcule UNE FOIS par cycle puis partage par TOUS les marches BTC : les
# fetches reseau (spot + klines) utilisent alors un TTL long
# (BTC_CONTEXT_CYCLE_TTL_S, defaut CYCLE_CACHE_TTL_S) et le contexte
# complet est memoise par
# (strike, minutes_remaining). begin_cycle() (appele par ExecutionEngine
# en debut de cycle) purge les deux caches : aucun cycle ne voit les
# donnees du precedent.
CYCLE_CACHE_TTL_S = 3600.0


def _cycle_cache_active() -> bool:
    """Flag BTC_CONTEXT_CYCLE_CACHE, lu LAZY (testable sans reload)."""
    v = os.getenv("BTC_CONTEXT_CYCLE_CACHE", "0").strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _cycle_ttl_s() -> float:
    """TTL long du cache de cycle : BTC_CONTEXT_CYCLE_TTL_S, lu LAZY comme
    le flag ci-dessus. Semantique identique a config._env_f : valeur
    invalide => defaut CYCLE_CACHE_TTL_S."""
    try:
        return float(os.getenv("BTC_CONTEXT_CYCLE_TTL_S",
                               str(CYCLE_CACHE_TTL_S)))
    except ValueError:
        return CYCLE_CACHE_TTL_S


_cache = {}                        # key -> (value, ts)
_cycle_ctx_cache = {}              # (strike, minutes_remaining) -> context


def _data_ttl() -> float:
    """TTL des fetches bruts : court (10 s) par defaut, LONG si le cache
    de cycle est actif (les donnees sont alors figees pour tout le cycle)."""
    return _cycle_ttl_s() if _cycle_cache_active() else CACHE_TTL_S


def begin_cycle() -> None:
    """Debute un nouveau cycle : purge le cache brut ET le cache de
    contexte. A appeler au DEBUT de chaque cycle d'execution quand
    BTC_CONTEXT_CYCLE_CACHE=1 (ExecutionEngine._cycle)."""
    _cache.clear()
    _cycle_ctx_cache.clear()


def _cached(key, ttl, fn):
    now = time.time()
    if key in _cache:
        val, ts = _cache[key]
        if 0 <= now - ts < ttl:
            return val
    val = fn()
    if val is not None:
        _cache[key] = (val, now)
    return val


# ── Fetchers par defaut (reseau) — remplacables par injection ────────────────

def _unique_json_object(pairs):
    """Reject ambiguous objects before the decoder can erase any member."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("nonstandard_json_constant")


def _http_get_json_meta(url, params=None):
    """GET JSON avec telemetrie complete : (data|None, meta).
    meta = {http_status, elapsed_ms, error} — plus JAMAIS de code HTTP
    avale en debug (audit 2026-07-25 : Binance echouait sans trace)."""
    import requests
    meta = {"http_status": None, "elapsed_ms": None, "error": None}
    last = None
    for _ in range(1 + MAX_RETRIES):
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S,
                             allow_redirects=False)
            meta["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            meta["http_status"] = r.status_code
            r.raise_for_status()
            if r.status_code != 200:
                raise ValueError("incomplete_or_redirected_http_response")
            if "Content-Range" in r.headers:
                raise ValueError("partial_http_response")
            # Binding belongs to the actual response origin. Even a custom
            # transport may not relabel another host/path as this provider.
            from urllib.parse import urlsplit
            actual, expected = urlsplit(r.url), urlsplit(url)
            if (actual.scheme, actual.netloc, actual.path) != (
                    expected.scheme, expected.netloc, expected.path):
                raise ValueError("response_origin_mismatch")
            data = r.json(object_pairs_hook=_unique_json_object,
                          parse_constant=_reject_json_constant, parse_float=Decimal)
            meta["error"] = None  # a successful retry has no pending error
            return data, meta
        except Exception as e:            # noqa: BLE001 — retry limite
            meta["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            meta["error"] = f"{type(e).__name__}: {e}"[:160]
            last = e
    return None, meta


def _http_get_json(url, params=None):
    return _http_get_json_meta(url, params)[0]


def _report_provider(provider, kind, meta, accepted, reason):
    """Journal par fournisseur : accepte (DEBUG) ou rejete (INFO) avec
    code HTTP, delai et erreur — exigence d'audit."""
    line = (f"[DATA_PROVIDER] provider={provider} kind={kind} "
            f"http_status={meta.get('http_status')} "
            f"elapsed_ms={meta.get('elapsed_ms')} "
            f"accepted={str(accepted).lower()} reason={reason}")
    if meta.get("error"):
        line += f" error={meta['error']}"
    (log.debug if accepted else log.info)(line)


def fetch_coinbase() -> Optional[dict]:
    d = _http_get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    if not d:
        return None
    try:
        return {"source": "coinbase", "price": float(d["data"]["amount"]),
                "ts": time.time()}
    except (KeyError, TypeError, ValueError):
        return None


def fetch_kraken() -> Optional[dict]:
    d = _http_get_json("https://api.kraken.com/0/public/Ticker",
                       {"pair": "XBTUSD"})
    try:
        return {"source": "kraken",
                "price": float(d["result"]["XXBTZUSD"]["c"][0]),
                "ts": time.time()}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def fetch_bitstamp() -> Optional[dict]:
    d = _http_get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/")
    try:
        # bitstamp fournit son propre timestamp -> validation de fraicheur
        return {"source": "bitstamp", "price": float(d["last"]),
                "ts": float(d.get("timestamp", time.time()))}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _fetch_candles(provider, params, limit):
    observed_ts = time.time()  # closure must already hold when request starts
    d, meta = _http_get_json_meta(KLINE_ORIGINS[provider][1], params)
    try:
        if (not _transport_complete(meta)):
            raise ValueError("transport_incomplete")
        if provider == "kraken":
            if (not isinstance(d, dict) or set(d) != {"error", "result"}
                    or d.get("error") != []
                    or not isinstance(d.get("result"), dict)
                    or set(d["result"]) != {"XXBTZUSD", "last"}
                    or not _finite_number(d["result"]["last"])):
                raise ValueError("provider_envelope_invalid")
            d = d["result"]["XXBTZUSD"]
        return _normalize_candles(d, provider, observed_ts, limit), meta
    except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta["error"] = meta.get("error") or f"parse_error:{exc}"
        return None, meta


def fetch_klines_binance(limit: int = 30):
    return _fetch_candles("binance", {"symbol": "BTCUSDT", "interval": "1m",
                                    "limit": limit + 1}, limit)


def fetch_klines_kraken(limit: int = 30):
    return _fetch_candles("kraken", {"pair": "XBTUSD", "interval": 1}, limit)


def fetch_klines_coinbase(limit: int = 30):
    return _fetch_candles("coinbase", {"granularity": 60}, limit)


DEFAULT_KLINES_PROVIDERS = (("binance", fetch_klines_binance),
                            ("kraken", fetch_klines_kraken),
                            ("coinbase", fetch_klines_coinbase))

# Retained for diagnostic compatibility only: an unavailable provider does not
# grant stale data permission to enter the model.
KLINES_STALE_MAX_S = 600.0
_last_good_klines = {"kl": None, "ts": 0.0, "provider": None}


def fetch_klines_with_fallback(limit: int = 30, providers=None, now=None):
    """Accept a provider only after complete validation. No stale fallback."""
    fixed_now = now
    if providers is None:
        order = [p.strip() for p in os.getenv(
            "KLINES_PROVIDER_ORDER", "binance,kraken,coinbase").split(",")]
        by_name = dict(DEFAULT_KLINES_PROVIDERS)
        providers = [(n, by_name[n]) for n in order if n in by_name]
    best_partial, best_name = None, None
    for name, fn in providers:
        try:
            res = fn(limit)
            kl, meta = res if isinstance(res, tuple) and len(res) == 2 else (res, {})
            check_now = fixed_now if fixed_now is not None else time.time()
            if (not _transport_complete(meta)):
                problem = "transport_incomplete"
            else:
                problem = _validate_klines(kl, check_now, name)
            if problem is None:
                _report_provider(name, "klines", meta, True, f"ok({len(kl)})")
                _last_good_klines.update(kl=kl, ts=check_now, provider=name)
                return kl, f"fresh:{name}"
            _report_provider(name, "klines", meta if isinstance(meta, dict) else {},
                             False, problem)
            if (problem.startswith("insufficient(")
                    and (best_partial is None or len(kl) > len(best_partial))):
                best_partial, best_name = kl, name
        except Exception as e:            # noqa: BLE001 -- provider isolation
            _report_provider(name, "klines", {"error": f"{type(e).__name__}: {e}"[:160]},
                             False, "exception")
    if best_partial:
        return best_partial, f"partial:{best_name}({len(best_partial)})"
    return None, "none"


DEFAULT_SPOT_SOURCES = (fetch_coinbase, fetch_kraken, fetch_bitstamp)


# ── Objet retourne ───────────────────────────────────────────────────────────

@dataclass
class BtcMarketContext:
    valid: bool
    reason: str
    generated_ts: float
    spot: Optional[float] = None            # consensus (mediane des sources)
    sources: list = field(default_factory=list)   # [{source, price, ts}]
    n_valid_sources: int = 0
    dispersion_pct: Optional[float] = None  # (max-min)/mediane * 100
    strike: Optional[float] = None
    distance: Optional[float] = None        # spot - strike ($)
    distance_norm: Optional[float] = None   # ln(spot/strike)
    minutes_remaining: Optional[float] = None
    returns: dict = field(default_factory=dict)   # {"1m","3m","5m","10m"} log
    realized_vol_1m: Optional[float] = None # ecart-type des log-rendements 1m
    momentum_per_min: Optional[float] = None
    klines_count: int = 0
    data_quality_score: float = 0.0         # 0..100
    quality_flags: list = field(default_factory=list)
    data_valid_until_ts: Optional[float] = None

    def to_dict(self):
        return asdict(self)


# ── Validation des sources spot ──────────────────────────────────────────────

def _validate_sources(raw: list, now: float) -> (list, list):
    """Filtre : prix positifs, timestamps frais, aberrants exclus (ecart a
    la mediane > MAX_DISPERSION_PCT)."""
    flags = []
    fresh = []
    for s in raw:
        if not s or not isinstance(s.get("price"), (int, float)):
            continue
        if s["price"] <= 0 or math.isnan(s["price"]) or math.isinf(s["price"]):
            flags.append(f"{s.get('source','?')}:prix_invalide")
            continue
        age = now - float(s.get("ts", 0))
        if age > MAX_PRICE_AGE_S or age < -30:
            flags.append(f"{s.get('source','?')}:perime({age:.0f}s)")
            continue
        fresh.append(s)
    if len(fresh) >= 2:
        med = statistics.median(x["price"] for x in fresh)
        kept = []
        for s in fresh:
            dev = abs(s["price"] - med) / med * 100
            if dev > MAX_DISPERSION_PCT:
                flags.append(f"{s['source']}:aberrant({dev:.2f}%)")
            else:
                kept.append(s)
        fresh = kept
    return fresh, flags


# ── Construction du contexte ─────────────────────────────────────────────────

def get_btc_context(strike: Optional[float] = None,
                    minutes_remaining: Optional[float] = None,
                    spot_sources: tuple = None,
                    klines_fn: Callable = None,
                    now: Optional[float] = None,
                    use_cache: bool = True) -> BtcMarketContext:
    """Recupere, valide et normalise. spot_sources/klines_fn injectables
    (tests hors-ligne). Retourne TOUJOURS un BtcMarketContext ; valid=False
    avec 'reason' explicite si les donnees sont insuffisantes."""
    fixed_now = now
    now = now if now is not None else time.time()
    # P8 : contexte memoise PAR CYCLE — meme strike/meme horizon => meme
    # objet contexte (les fetches reseau n'ont lieu qu'une fois par cycle).
    # Seuls les contextes VALIDES sont memoises (un invalide peut devenir
    # valide en cours de cycle sans nouveau fetch — on ne le fige pas).
    cycle_key = (strike, minutes_remaining)
    if use_cache and _cycle_cache_active() and cycle_key in _cycle_ctx_cache:
        cached_ctx = _cycle_ctx_cache[cycle_key]
        if (cached_ctx.data_valid_until_ts is not None
                and cached_ctx.generated_ts <= now <= cached_ctx.data_valid_until_ts
                and now - cached_ctx.generated_ts < _data_ttl()):
            return cached_ctx
        del _cycle_ctx_cache[cycle_key]
    spot_sources = spot_sources or DEFAULT_SPOT_SOURCES
    # klines_fn=None => chaine multi-fournisseurs avec secours (defaut).
    # L'injection d'un fournisseur unique reste possible (tests).

    def pull():
        out = []
        for f in spot_sources:
            name = getattr(f, "__name__", "spot").replace("fetch_", "")
            t0 = time.time()
            try:
                r = f()
            except Exception as e:        # noqa: BLE001
                _report_provider(name, "spot",
                                 {"http_status": None,
                                  "elapsed_ms": round((time.time()-t0)*1e3, 1),
                                  "error": f"{type(e).__name__}: {e}"[:160]},
                                 False, "exception")
                out.append(None)
                continue
            _report_provider(
                name, "spot",
                {"http_status": None,
                 "elapsed_ms": round((time.time() - t0) * 1e3, 1),
                 "error": None},
                bool(r), "ok" if r else "aucune_donnee")
            out.append(r)
        return out
    raw = _cached("spot_sources", _data_ttl(), pull) if use_cache else pull()
    sources, flags = _validate_sources(raw or [], now)

    ctx = BtcMarketContext(valid=False, reason="", generated_ts=now,
                           sources=sources, n_valid_sources=len(sources),
                           quality_flags=flags,
                           strike=strike, minutes_remaining=minutes_remaining)

    if len(sources) < MIN_VALID_SOURCES:
        n_resp = len([r for r in (raw or []) if r])
        ctx.reason = ("aucune_donnee:spot" if n_resp == 0 else
                      f"donnees_insuffisantes:spot"
                      f"({len(sources)}/{MIN_VALID_SOURCES})")
        return ctx

    prices = [s["price"] for s in sources]
    ctx.spot = statistics.median(prices)
    ctx.dispersion_pct = round((max(prices) - min(prices)) / ctx.spot * 100, 4)

    try:
        if klines_fn is not None:
            raw_kl = (_cached("klines", _data_ttl(), lambda: klines_fn())
                      if use_cache else klines_fn())
            if isinstance(raw_kl, tuple):
                raw_kl, meta = raw_kl
                if (not _transport_complete(meta)):
                    raise ValueError("transport_incomplete")
            kl, kl_src = raw_kl, ("fresh:injected" if raw_kl else "none")
        else:
            def pull_kl():
                return fetch_klines_with_fallback(now=fixed_now)
            kl, kl_src = (_cached("klines_fb", _data_ttl(), pull_kl)
                          if use_cache else pull_kl())
    except Exception as exc:  # noqa: BLE001 -- bad feed is a closed gate
        ctx.reason = "invalid_klines:provider_exception"
        flags.append(f"klines:provider_exception:{type(exc).__name__}")
        return ctx
    if fixed_now is None:
        now = time.time()  # validation follows slow HTTP calls, never precedes them
        ctx.generated_ts = now
        # Spot may also have expired while waiting for candle providers.
        sources, spot_flags = _validate_sources(raw or [], now)
        flags.extend(spot_flags)
        ctx.sources, ctx.n_valid_sources = sources, len(sources)
        if len(sources) < MIN_VALID_SOURCES:
            ctx.reason = "donnees_insuffisantes:spot_after_candles"
            return ctx
        prices = [s["price"] for s in sources]
        ctx.spot = statistics.median(prices)
        ctx.dispersion_pct = round((max(prices) - min(prices)) / ctx.spot * 100, 4)
    if kl_src != "none":
        flags.append(f"klines:source={kl_src}")
    problem = ("stale_cache_forbidden" if kl_src.startswith("stale_cache")
               else _validate_klines(kl, now))
    if problem:
        flags.append(f"klines:{problem}")
        if problem.startswith("insufficient("):
            ctx.klines_count = len(kl)
            ctx.reason = f"donnees_insuffisantes:klines({len(kl)}/{MIN_KLINES},{kl_src})"
        else:
            ctx.reason = ("aucune_donnee:klines" if kl_src == "none"
                          else f"invalid_klines:{problem}")
        return ctx
    ctx.klines_count = len(kl)

    if len(kl) >= MIN_KLINES:
        closes = [k["close"] for k in kl]
        def lr(n):  # log-rendement sur n minutes
            return math.log(closes[-1]) - math.log(closes[-1 - n])
        ctx.returns = {"1m": lr(1), "3m": lr(3), "5m": lr(5), "10m": lr(10)}
        ctx.momentum_per_min = (closes[-1] - closes[-6]) / 5
        rets = [math.log(closes[i + 1]) - math.log(closes[i])
                for i in range(len(closes) - 1)]
        ctx.realized_vol_1m = statistics.pstdev(rets) if len(rets) >= 2 else None
    else:
        flags.append(f"klines:insuffisantes({len(kl)}/{MIN_KLINES})")

    if strike is not None and strike > 0 and ctx.spot:
        ctx.distance = round(ctx.spot - strike, 2)
        ctx.distance_norm = math.log(ctx.spot / strike)

    # ── data_quality_score (0..100), documente ──
    score = 0.0
    score += 40.0 * min(1.0, len(sources) / 3)              # nb de sources
    if ctx.dispersion_pct is not None:                      # accord entre elles
        score += 20.0 * max(0.0, 1.0 - ctx.dispersion_pct / MAX_DISPERSION_PCT)
    if ctx.realized_vol_1m is not None and ctx.realized_vol_1m > 0:
        score += 25.0                                       # vol mesurable
    if ctx.klines_count >= MIN_KLINES:
        score += 15.0                                       # historique 1m
    ctx.data_quality_score = round(score, 1)

    if ctx.realized_vol_1m is None or ctx.realized_vol_1m <= 0:
        # Distinction exigee par l'audit :
        #  - aucune_donnee : AUCUN fournisseur n'a repondu ET pas de cache
        #  - donnees_insuffisantes : reponses partielles (< MIN_KLINES)
        #  - volatilite_nulle : donnees completes mais variance nulle
        if ctx.klines_count == 0:
            ctx.reason = ("aucune_donnee:klines" if kl_src == "none"
                          else f"donnees_insuffisantes:klines"
                               f"(0/{MIN_KLINES},{kl_src})")
        elif ctx.klines_count < MIN_KLINES:
            ctx.reason = (f"donnees_insuffisantes:klines"
                          f"({ctx.klines_count}/{MIN_KLINES},{kl_src})")
        else:
            ctx.reason = "volatilite_nulle"
        return ctx

    ctx.valid = True
    ctx.reason = "ok"
    ctx.data_valid_until_ts = min(
        min(float(s["ts"]) + MAX_PRICE_AGE_S for s in sources),
        kl[-1]["ts"] + MAX_KLINE_AGE_S)

    if use_cache and _cycle_cache_active():
        _cycle_ctx_cache[cycle_key] = ctx
    return ctx


def get_btc_price(spot_sources: tuple = None) -> Optional[float]:
    """Compat : spot consensus, ou None si < 2 sources valides."""
    ctx = get_btc_context(spot_sources=spot_sources)
    return ctx.spot if ctx.n_valid_sources >= MIN_VALID_SOURCES else None


def clear_cache():
    """Purge caches brut + cycle (utilise par les tests)."""
    _cache.clear()
    _cycle_ctx_cache.clear()
