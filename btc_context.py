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
from typing import Optional, Callable

log = logging.getLogger("BTCCTX")

#: Module version, exported for the startup banner. AUD-OBS-001: the
#: banner printed "btc_context=inconnue" because this attribute did not
#: exist — the label read like a health state while it only meant "no
#: VERSION exported". The module was present and working.
VERSION = "v2.1-fallback-observability"

# ── Parametres (env-surchargables cote appelant si besoin) ───────────────────
HTTP_TIMEOUT_S      = 5.0
MAX_RETRIES         = 1            # retry LIMITE par source
CACHE_TTL_S         = 10.0         # cache court : donnees "ultra fraiches"
MAX_PRICE_AGE_S     = 90.0         # au-dela : donnee PERIMEE
MAX_DISPERSION_PCT  = 0.5          # ecart max entre exchanges (aberrant sinon)
MIN_VALID_SOURCES   = 2
MIN_KLINES          = 11           # pour rendement 10m + vol realisee
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
        if now - ts < ttl:
            return val
    val = fn()
    if val is not None:
        _cache[key] = (val, now)
    return val


# ── Fetchers par defaut (reseau) — remplacables par injection ────────────────

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
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
            meta["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            meta["http_status"] = r.status_code
            r.raise_for_status()
            return r.json(), meta
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


def fetch_klines_binance(limit: int = 30):
    d, meta = _http_get_json_meta(
        "https://api.binance.com/api/v3/klines",
        {"symbol": "BTCUSDT", "interval": "1m", "limit": limit})
    if not d:
        return None, meta
    try:
        return [{"ts": k[0] / 1000.0, "open": float(k[1]),
                 "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])}
                for k in d], meta
    except (TypeError, ValueError, IndexError):
        meta["error"] = "parse_error"
        return None, meta


def fetch_klines_kraken(limit: int = 30):
    """Secours n°1 : Kraken OHLC 1m (public, accessible depuis les IP US,
    contrairement a Binance qui geo-bloque frequemment en HTTP 451)."""
    d, meta = _http_get_json_meta("https://api.kraken.com/0/public/OHLC",
                                  {"pair": "XBTUSD", "interval": 1})
    try:
        rows = d["result"]["XXBTZUSD"]
        out = [{"ts": float(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[6])} for k in rows][-limit:]
        return out, meta
    except (KeyError, TypeError, ValueError, IndexError):
        meta["error"] = meta.get("error") or "parse_error"
        return None, meta


def fetch_klines_coinbase(limit: int = 30):
    """Secours n°2 : Coinbase Exchange candles 60s (public).
    Reponse triee du plus RECENT au plus ancien -> inversee."""
    d, meta = _http_get_json_meta(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        {"granularity": 60})
    try:
        rows = sorted(d, key=lambda k: k[0])[-limit:]
        out = [{"ts": float(k[0]), "low": float(k[1]), "high": float(k[2]),
                "open": float(k[3]), "close": float(k[4]),
                "volume": float(k[5])} for k in rows]
        return out, meta
    except (TypeError, ValueError, IndexError, KeyError):
        meta["error"] = meta.get("error") or "parse_error"
        return None, meta


DEFAULT_KLINES_PROVIDERS = (("binance", fetch_klines_binance),
                            ("kraken", fetch_klines_kraken),
                            ("coinbase", fetch_klines_coinbase))

# cache des DERNIERES bougies valides : une panne TEMPORAIRE des
# fournisseurs ne bloque plus toutes les decisions (mode degrade borne)
KLINES_STALE_MAX_S = 600.0         # au-dela : donnees refusees
_last_good_klines = {"kl": None, "ts": 0.0, "provider": None}


def _report_klines_state(state: str, used: str, failures: list,
                         klines, now: float) -> None:
    """AUD-OBS-002: ONE INFO line per klines resolution naming the
    audit states (PRIMARY_OK / FALLBACK_OK / DEGRADED_PARTIAL /
    DEGRADED_STALE_CACHE / ALL_PROVIDERS_FAILED), the provider that
    actually feeds the model features, every failed provider with its
    HTTP status, and a freshness proof (last-candle age). Before this,
    successful fetches logged only at DEBUG: production logs showed the
    Binance 451 but never which provider took over."""
    last_age = (f"{now - klines[-1]['ts']:.0f}"
                if klines else "n/a")
    log.info(f"[DATA_PROVIDER_STATE] kind=klines state={state} "
             f"used={used or 'none'} "
             f"failed={','.join(failures) if failures else 'none'} "
             f"last_candle_age_s={last_age} "
             f"count={len(klines or [])}")


def fetch_klines_with_fallback(limit: int = 30, providers=None, now=None):
    """Essaie chaque fournisseur dans l'ordre ; journalise chacun ;
    retourne (klines, source_info). source_info distingue :
      - fresh:<provider>                  donnees fraiches completes
      - partial:<provider>(n)             reponses mais < MIN_KLINES
      - stale_cache:<provider>(age)       secours cache borne
      - none                              AUCUNE donnee nulle part"""
    import os as _os
    now = now if now is not None else time.time()
    if providers is None:
        order = [p.strip() for p in _os.getenv(
            "KLINES_PROVIDER_ORDER", "binance,kraken,coinbase").split(",")]
        by_name = dict(DEFAULT_KLINES_PROVIDERS)
        providers = [(n, by_name[n]) for n in order if n in by_name]
    primary_name = providers[0][0] if providers else None
    failures: list[str] = []
    best_partial, best_name = None, None
    for name, fn in providers:
        try:
            res = fn(limit)
        except Exception as e:            # noqa: BLE001
            _report_provider(name, "klines",
                             {"http_status": None, "elapsed_ms": None,
                              "error": f"{type(e).__name__}: {e}"[:160]},
                             False, "exception")
            failures.append(f"{name}(exception)")
            continue
        kl, meta = res if isinstance(res, tuple) else (res, {})
        n = len(kl or [])
        if kl and n >= MIN_KLINES:
            _report_provider(name, "klines", meta, True, f"ok({n})")
            _last_good_klines.update(kl=kl, ts=now, provider=name)
            _report_klines_state(
                "PRIMARY_OK" if name == primary_name else "FALLBACK_OK",
                name, failures, kl, now)
            return kl, f"fresh:{name}"
        _report_provider(name, "klines", meta, False,
                         f"insuffisant({n}/{MIN_KLINES})" if kl
                         else "aucune_donnee")
        failures.append(f"{name}({meta.get('http_status') or 'no_data'})")
        if kl and (best_partial is None or n > len(best_partial)):
            best_partial, best_name = kl, name
    stale = _last_good_klines["kl"]
    age = now - _last_good_klines["ts"]
    if stale and age <= KLINES_STALE_MAX_S:
        log.warning(f"[DATA_PROVIDER] klines: TOUS les fournisseurs "
                    f"indisponibles -- secours cache "
                    f"({_last_good_klines['provider']}, age {age:.0f}s)")
        _report_klines_state("DEGRADED_STALE_CACHE",
                             _last_good_klines["provider"], failures,
                             stale, now)
        return stale, (f"stale_cache:{_last_good_klines['provider']}"
                       f"({age:.0f}s)")
    if best_partial:
        _report_klines_state("DEGRADED_PARTIAL", best_name, failures,
                             best_partial, now)
        return best_partial, f"partial:{best_name}({len(best_partial)})"
    _report_klines_state("ALL_PROVIDERS_FAILED", None, failures, None,
                         now)
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
    now = now if now is not None else time.time()
    # P8 : contexte memoise PAR CYCLE — meme strike/meme horizon => meme
    # objet contexte (les fetches reseau n'ont lieu qu'une fois par cycle).
    # Seuls les contextes VALIDES sont memoises (un invalide peut devenir
    # valide en cours de cycle sans nouveau fetch — on ne le fige pas).
    cycle_key = (strike, minutes_remaining)
    if use_cache and _cycle_cache_active() and cycle_key in _cycle_ctx_cache:
        return _cycle_ctx_cache[cycle_key]
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

    if klines_fn is not None:
        # compat tests/injection : un seul fournisseur, sortie brute
        raw_kl = (_cached("klines", _data_ttl(), lambda: klines_fn())
                  if use_cache else klines_fn())
        raw_kl = raw_kl[0] if isinstance(raw_kl, tuple) else raw_kl
        kl, kl_src = (raw_kl or []), ("fresh:injected" if raw_kl else "none")
    else:
        def pull_kl():
            return fetch_klines_with_fallback(now=now)
        kl, kl_src = (_cached("klines_fb", _data_ttl(), pull_kl)
                      if use_cache else pull_kl())
        kl = kl or []
    is_stale = kl_src.startswith("stale_cache")
    if kl_src != "none":
        flags.append(f"klines:source={kl_src}")
    # validation des timestamps des bougies (chronologie + fraicheur)
    kl = [k for k in kl if isinstance(k.get("close"), (int, float))
          and k["close"] > 0]
    if kl and any(kl[i]["ts"] >= kl[i + 1]["ts"] for i in range(len(kl) - 1)):
        flags.append("klines:timestamps_non_monotones")
        kl = []
    max_age = KLINES_STALE_MAX_S if is_stale else 3 * 60
    if kl and now - kl[-1]["ts"] > max_age:
        flags.append("klines:perimees")
        kl = []
    ctx.klines_count = len(kl)

    if len(kl) >= MIN_KLINES:
        closes = [k["close"] for k in kl]
        def lr(n):  # log-rendement sur n minutes
            return math.log(closes[-1] / closes[-1 - n])
        if is_stale:
            # mode degrade : la volatilite (ordre de grandeur stable sur
            # quelques minutes) reste utilisable ; le MOMENTUM (directionnel,
            # perime en secondes) est NEUTRALISE, jamais rechauffe.
            ctx.returns = {}
            ctx.momentum_per_min = None
            flags.append("klines:momentum_neutralise_car_cache")
        else:
            ctx.returns = {"1m": lr(1), "3m": lr(3), "5m": lr(5),
                           "10m": lr(10)}
            ctx.momentum_per_min = (closes[-1] - closes[-6]) / 5 \
                if len(closes) >= 6 else None
        rets = [math.log(closes[i + 1] / closes[i])
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
        if is_stale:
            age = now - _last_good_klines["ts"]
            score += 15.0 * max(0.0, 1.0 - age / KLINES_STALE_MAX_S)
        else:
            score += 15.0                                   # historique 1m
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
