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
import hashlib
import json
import os
import time
import logging
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

log = logging.getLogger("BTCCTX")

# ── Parametres (env-surchargables cote appelant si besoin) ───────────────────
HTTP_TIMEOUT_S      = 5.0
MAX_RETRIES         = 1            # retry LIMITE par source
CACHE_TTL_S         = 10.0         # cache court : donnees "ultra fraiches"
MAX_PRICE_AGE_S     = 90.0         # au-dela : donnee PERIMEE
MAX_DISPERSION_PCT  = 0.5          # ecart max entre exchanges (aberrant sinon)
MIN_VALID_SOURCES   = 2
KLINE_INTERVAL_S = 60
MAX_KLINE_CLOSE_AGE_S = 120.0
KLINE_POLICY_VERSION = "closed-1m-v1"
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


def _finite_number(value, *, wire=False):
    """Reject booleans, overflow and non-finite input before numeric use.

    Wire adapters alone accept numeric strings: exchange OHLC prices use them.
    The normalized row contract requires actual finite numbers.
    """
    if type(value) not in ((int, float, str) if wire else (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _wire_number(value):
    number = _finite_number(value, wire=True)
    if number is None:
        raise ValueError("invalid wire number")
    return number


def qualify_klines(rows, now, closed_before=None):
    """All-or-nothing row validation before any model or cache acceptance.

    Bars are one-minute, UTC-grid-aligned and strictly consecutive. Bars not
    completed at the conservative observation cutoff are omitted explicitly;
    future or malformed bars invalidate the entire response. At least MIN_KLINES
    completed bars are needed.
    The age bound measures the last completed bar's close, never fetch time.
    """
    now = _finite_number(now)
    if now is None or now <= 0:
        return None, "invalid_clock"
    cutoff = now if closed_before is None else _finite_number(closed_before)
    if cutoff is None or cutoff <= 0 or cutoff > now:
        return None, "invalid_observation_cutoff"
    if not isinstance(rows, list) or not rows:
        return None, "no_rows"
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            return None, "row_schema"
        values = {key: _finite_number(row.get(key))
                  for key in ("ts", "open", "high", "low", "close", "volume")}
        if any(value is None for value in values.values()):
            return None, "row_number"
        ts = values["ts"]
        if ts <= 0 or ts != int(ts) or ts % KLINE_INTERVAL_S:
            return None, "timestamp_grid"
        if ts > now:
            return None, "future_bar"
        if any(values[key] <= 0 for key in ("open", "high", "low", "close")):
            return None, "nonpositive_price"
        if values["volume"] < 0:
            return None, "negative_volume"
        if not (values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            return None, "ohlc_range"
        if normalized and ts - normalized[-1]["ts"] != KLINE_INTERVAL_S:
            return None, "cadence_or_order"
        normalized.append(values)
    # This is the only permitted row omission, after complete schema validation.
    normalized = [row for row in normalized
                  if row["ts"] + KLINE_INTERVAL_S <= cutoff]
    if len(normalized) < MIN_KLINES:
        return None, "insufficient_closed_bars"
    if now - (normalized[-1]["ts"] + KLINE_INTERVAL_S) > MAX_KLINE_CLOSE_AGE_S:
        return None, "stale_closed_bar"
    return normalized, "ok"


def _klines_provenance(rows, source, now):
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    name = source.removeprefix("fresh:")
    origin = {
        "binance": ("https://api.binance.com/api/v3/klines", "BTCUSDT", "USDT"),
        "kraken": ("https://api.kraken.com/0/public/OHLC", "XBTUSD", "USD"),
        "coinbase": ("https://api.exchange.coinbase.com/products/BTC-USD/candles", "BTC-USD", "USD"),
    }.get(name, (None, None, None))
    return {"schema": KLINE_POLICY_VERSION, "source": source,
            "endpoint": origin[0], "instrument": origin[1], "quote_currency": origin[2],
            "interval_seconds": KLINE_INTERVAL_S,
            "first_open_ts": rows[0]["ts"], "last_open_ts": rows[-1]["ts"],
            "last_close_ts": rows[-1]["ts"] + KLINE_INTERVAL_S,
            "validated_at": now, "row_count": len(rows),
            "normalized_sha256": hashlib.sha256(canonical).hexdigest(),
            "normalized_rows": rows,
            "max_close_age_seconds": MAX_KLINE_CLOSE_AGE_S,
            "degraded_cache_allowed": False,
            "qualification": "schema_and_freshness_only"}


def decision_candles_current(model_output, now=None):
    """Recheck bound model inputs at the execution boundary; no I/O.

    This verifies our retained normalization/preimage and its expiry. It does
    not authenticate an exchange or qualify a provider's economic suitability.
    Missing/foreign/malformed proof is refusal, including legacy predictions.
    """
    now = _finite_number(time.time() if now is None else now)
    if now is None or not isinstance(model_output, dict) or model_output.get("valid") is not True:
        return False
    features = model_output.get("features")
    proof = features.get("candle_provenance") if isinstance(features, dict) else None
    if not isinstance(proof, dict):
        return False
    source = proof.get("source")
    if source not in ("fresh:binance", "fresh:kraken", "fresh:coinbase"):
        return False
    validated_at = _finite_number(proof.get("validated_at"))
    expires_at = _finite_number(proof.get("valid_until"))
    if validated_at is None or expires_at is None or not validated_at <= now <= expires_at:
        return False
    if proof.get("degraded_cache_allowed") is not False:
        return False
    rows, _ = qualify_klines(proof.get("normalized_rows"), now)
    if rows is None:
        return False
    expected = _klines_provenance(rows, source, validated_at)
    if any(type(proof.get(key)) is not type(value) or proof.get(key) != value
           for key, value in expected.items()):
        return False
    if expires_at > rows[-1]["ts"] + KLINE_INTERVAL_S + MAX_KLINE_CLOSE_AGE_S:
        return False
    # The scalar model inputs must still describe this exact retained window.
    logs = [math.log(row["close"]) for row in rows]
    sigma = statistics.pstdev(b-a for a, b in zip(logs, logs[1:]))
    return (_finite_number(features.get("sigma_1m")) == sigma
            and _finite_number(features.get("ret_5m")) == logs[-1] - logs[-6])


def _context_still_fresh(ctx, now):
    now = _finite_number(now)
    if now is None or now < ctx.generated_ts:
        return False
    proof = ctx.klines_provenance
    if not isinstance(proof, dict) or proof.get("schema") != KLINE_POLICY_VERSION:
        return False
    expires_at = _finite_number(proof.get("valid_until"))
    last_close = _finite_number(proof.get("last_close_ts"))
    if expires_at is None or last_close is None or now > expires_at:
        return False
    age = now - last_close
    sources, _ = _validate_sources(ctx.sources, now)
    return 0 <= age <= MAX_KLINE_CLOSE_AGE_S and len(sources) >= MIN_VALID_SOURCES


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
        if not isinstance(d, list):
            raise ValueError("invalid Binance candle envelope")
        for k in d:
            if not isinstance(k, list) or len(k) != 12:
                raise ValueError("invalid Binance candle row")
            opened, closed = _wire_number(k[0]), _wire_number(k[6])
            if opened != int(opened) or closed != opened + 59999:
                raise ValueError("contradictory Binance candle interval")
        return [{"ts": _wire_number(k[0]) / 1000.0, "open": _wire_number(k[1]),
                 "high": _wire_number(k[2]), "low": _wire_number(k[3]),
                 "close": _wire_number(k[4]), "volume": _wire_number(k[5])}
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
        if not isinstance(d, dict) or d.get("error") != []:
            raise ValueError("uncertain Kraken response")
        result = d.get("result")
        if not isinstance(result, dict) or set(result) != {"XXBTZUSD", "last"}:
            raise ValueError("unknown Kraken response identity")
        _wire_number(result["last"])
        rows = result["XXBTZUSD"]
        if not isinstance(rows, list) or any(not isinstance(k, list) or len(k) != 8 for k in rows):
            raise ValueError("invalid Kraken candle row")
        out = [{"ts": _wire_number(k[0]), "open": _wire_number(k[1]), "high": _wire_number(k[2]),
                "low": _wire_number(k[3]), "close": _wire_number(k[4]),
                "volume": _wire_number(k[6])} for k in rows]
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
        if not isinstance(d, list):
            raise ValueError("invalid candle envelope")
        timestamps = [_wire_number(k[0]) for k in d]
        if any(a <= b for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("unexpected Coinbase candle ordering")
        if any(not isinstance(k, list) or len(k) != 6 for k in d):
            raise ValueError("invalid Coinbase candle row")
        rows = list(reversed(d))
        out = [{"ts": _wire_number(k[0]), "low": _wire_number(k[1]), "high": _wire_number(k[2]),
                "open": _wire_number(k[3]), "close": _wire_number(k[4]),
                "volume": _wire_number(k[5])} for k in rows]
        return out, meta
    except (TypeError, ValueError, IndexError, KeyError):
        meta["error"] = meta.get("error") or "parse_error"
        return None, meta


DEFAULT_KLINES_PROVIDERS = (("binance", fetch_klines_binance),
                            ("kraken", fetch_klines_kraken),
                            ("coinbase", fetch_klines_coinbase))

# Compatibility constants only; outage cache is never executable input.
KLINES_STALE_MAX_S = 600.0
_last_good_klines = {"kl": None, "ts": 0.0, "provider": None}


def fetch_klines_with_fallback(limit: int = 30, providers=None, now=None):
    """Return only fresh, complete one-minute rows from a qualified adapter.

    Qualification here is a local schema/freshness policy, not proof of a live
    provider's availability or independent economic fitness. Outage => refusal.
    """
    import os as _os
    clock = (lambda: now) if now is not None else time.time
    if providers is None:
        order = [p.strip() for p in _os.getenv(
            "KLINES_PROVIDER_ORDER", "binance,kraken,coinbase").split(",")]
        by_name = dict(DEFAULT_KLINES_PROVIDERS)
        providers = [(n, by_name[n]) for n in order if n in by_name]
    if _finite_number(clock()) is None or type(limit) is not int or not MIN_KLINES <= limit <= 720:
        return None, "none"
    rejected = None
    for name, fn in providers:
        try:
            closed_before = clock()
            res = fn(limit)
            kl, meta = res if isinstance(res, tuple) and len(res) == 2 else (res, {})
            meta = meta if isinstance(meta, dict) else {}
            qualified, reason = qualify_klines(kl, clock(), closed_before=closed_before)
        except Exception as e:            # noqa: BLE001
            _report_provider(name, "klines",
                             {"http_status": None, "elapsed_ms": None,
                              "error": f"{type(e).__name__}: {e}"[:160]},
                             False, "exception")
            continue
        if qualified is not None:
            _report_provider(name, "klines", meta, True,
                             f"ok({len(qualified)}),policy={KLINE_POLICY_VERSION}")
            return qualified[-limit:], f"fresh:{name}"
        _report_provider(name, "klines", meta, False, reason)
        if kl is not None:
            rejected = f"rejected:{name}:{reason}"
    # An outage never reheats cached volatility into executable model inputs.
    # Each fallback must independently satisfy the same complete bar contract.
    return None, rejected or "none"


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
    klines_provenance: dict = field(default_factory=dict)

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
    explicit_now = now
    clock = (lambda: explicit_now) if explicit_now is not None else time.time
    now = clock()
    if _finite_number(now) is None or now <= 0:
        return BtcMarketContext(valid=False, reason="invalid_clock", generated_ts=0.0)
    # P8 : contexte memoise PAR CYCLE — meme strike/meme horizon => meme
    # objet contexte (les fetches reseau n'ont lieu qu'une fois par cycle).
    # Seuls les contextes VALIDES sont memoises (un invalide peut devenir
    # valide en cours de cycle sans nouveau fetch — on ne le fige pas).
    cycle_key = (strike, minutes_remaining)
    if use_cache and _cycle_cache_active() and cycle_key in _cycle_ctx_cache:
        cached_ctx = _cycle_ctx_cache[cycle_key]
        if _context_still_fresh(cached_ctx, now):
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
    now = clock()
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
        def pull_injected():
            cutoff = clock()
            raw = klines_fn()
            raw = raw[0] if isinstance(raw, tuple) else raw
            return qualify_klines(raw, clock(), closed_before=cutoff)[0]
        raw_kl = (_cached("klines", _data_ttl(), pull_injected)
                  if use_cache else pull_injected())
        kl, kl_src = (raw_kl or []), ("fresh:injected" if raw_kl else "none")
    else:
        def pull_kl():
            return fetch_klines_with_fallback(now=explicit_now)
        kl, kl_src = (_cached("klines_fb", _data_ttl(), pull_kl)
                      if use_cache else pull_kl())
        kl = kl or []
    if kl_src != "none":
        flags.append(f"klines:source={kl_src}")
    # Provider I/O may take time. Revalidate both dependencies at completion.
    now = clock()
    ctx.generated_ts = now
    current_sources, expired_flags = _validate_sources(sources, now)
    if len(current_sources) < MIN_VALID_SOURCES:
        ctx.sources = current_sources
        ctx.n_valid_sources = len(current_sources)
        flags.extend(expired_flags)
        ctx.reason = "donnees_insuffisantes:spot_apres_klines"
        return ctx
    # Validate again after cache lookup and on injected paths. Readable cached
    # data is not automatically fresh, and one malformed row is never dropped.
    qualified, qualification = qualify_klines(kl, now)
    if not kl_src.startswith("fresh:"):
        qualified, qualification = None, "unqualified_source"
    kl = qualified or []
    if qualified is None:
        flags.append(f"klines:{qualification}")
    ctx.klines_count = len(kl)

    if len(kl) >= MIN_KLINES:
        ctx.klines_provenance = _klines_provenance(kl, kl_src, now)
        ctx.klines_provenance["valid_until"] = min(
            kl[-1]["ts"] + KLINE_INTERVAL_S + MAX_KLINE_CLOSE_AGE_S,
            *(source["ts"] + MAX_PRICE_AGE_S for source in sources))
        log_closes = [math.log(k["close"]) for k in kl]
        def lr(n):
            return log_closes[-1] - log_closes[-1 - n]
        ctx.returns = {"1m": lr(1), "3m": lr(3), "5m": lr(5), "10m": lr(10)}
        ctx.momentum_per_min = (kl[-1]["close"] - kl[-6]["close"]) / 5
        rets = [log_closes[i + 1] - log_closes[i]
                for i in range(len(log_closes) - 1)]
        ctx.realized_vol_1m = statistics.pstdev(rets)
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
