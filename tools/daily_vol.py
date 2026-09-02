"""
daily_vol.py — volatility estimators for a ~15-hour horizon.

WHY
    The deployed model scales a 1-minute volatility, estimated from ~29
    one-minute returns, by sqrt(T) out to T≈900 minutes. That assumes
    returns are i.i.d. for fifteen hours and that a half-hour window
    knows the day's volatility. Measured against the data it produces an
    implied 24h sigma of ~1.6% where BTC realises 2.5–4%: the model is
    overconfident on every daily strike, and the "edge" it reports on
    them is the misspecification talking.

    This module holds the candidate replacements as pure functions over a
    candle series, so they can be compared on the same data with the same
    walk-forward harness. Every estimator reads only candles that CLOSED
    before the evaluation time.

WHAT IS AND IS NOT HERE
    Estimators and their tests on synthetic geometric Brownian motion,
    where the true sigma is known and each estimator can be checked for
    bias. No claim about which estimator is best on real BTC is made here;
    that needs a real candle history, which the research environment
    could not reach (every exchange and archive endpoint is egress-
    blocked) and the engine does not retain (it fetches 30 candles and
    keeps none).
"""

import math
from typing import Optional, Sequence

CandleSeq = Sequence[dict]      # {"ts", "open", "high", "low", "close"}


def _closed_before(candles: CandleSeq, t: float, minutes: float) -> list:
    """Candles whose CLOSE (ts + 60s) is at or before t, within the window."""
    lo = t - minutes * 60.0
    return [c for c in candles
            if c["ts"] + 60.0 <= t and c["ts"] >= lo]


def _log_returns(closes: Sequence[float]) -> list:
    return [math.log(closes[i + 1] / closes[i])
            for i in range(len(closes) - 1)
            if closes[i] > 0 and closes[i + 1] > 0]


def _pstdev(xs) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mu = sum(xs) / n
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / n)


def realized_1m(candles: CandleSeq, t: float, minutes: float,
                min_obs: int = 20) -> Optional[float]:
    """Close-to-close 1-minute sigma over the trailing window. The deployed
    estimator is this with minutes≈30. None below min_obs returns — a
    sigma from too few points is a guess, not an estimate."""
    w = _closed_before(candles, t, minutes)
    rets = _log_returns([c["close"] for c in w])
    return _pstdev(rets) if len(rets) >= min_obs else None


def ewma_1m(candles: CandleSeq, t: float, minutes: float,
            halflife_min: float, min_obs: int = 20) -> Optional[float]:
    """Exponentially weighted 1-minute sigma: recent returns count more,
    old ones are never dropped outright. halflife in minutes."""
    w = _closed_before(candles, t, minutes)
    rets = _log_returns([c["close"] for c in w])
    if len(rets) < min_obs:
        return None
    lam = 0.5 ** (1.0 / halflife_min)
    var, wsum = 0.0, 0.0
    weight = 1.0
    for r in reversed(rets):
        var += weight * r * r
        wsum += weight
        weight *= lam
    return math.sqrt(var / wsum) if wsum > 0 else None


def parkinson_1m(candles: CandleSeq, t: float, minutes: float,
                 min_obs: int = 20) -> Optional[float]:
    """Parkinson (1980) high-low estimator: uses the intra-candle range,
    ~5x more efficient than close-to-close under GBM."""
    w = [c for c in _closed_before(candles, t, minutes)
         if c.get("high") and c.get("low") and c["low"] > 0
         and c["high"] >= c["low"]]
    if len(w) < min_obs:
        return None
    s = sum(math.log(c["high"] / c["low"]) ** 2 for c in w)
    return math.sqrt(s / (4.0 * math.log(2.0) * len(w)))


def garman_klass_1m(candles: CandleSeq, t: float, minutes: float,
                    min_obs: int = 20) -> Optional[float]:
    """Garman–Klass (1980): open/high/low/close, ~8x close-to-close
    efficiency under GBM with no drift."""
    w = [c for c in _closed_before(candles, t, minutes)
         if all(c.get(k) for k in ("open", "high", "low", "close"))
         and c["low"] > 0 and c["open"] > 0]
    if len(w) < min_obs:
        return None
    s = 0.0
    for c in w:
        hl = math.log(c["high"] / c["low"])
        co = math.log(c["close"] / c["open"])
        s += 0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co
    v = s / len(w)
    return math.sqrt(v) if v > 0 else None


def scale_to_horizon(sigma_1m: Optional[float],
                     horizon_min: float) -> Optional[float]:
    """sqrt-time scaling. Correct for i.i.d. returns; the whole point of
    the multi-horizon comparison is to find out how wrong it is."""
    if sigma_1m is None or horizon_min <= 0:
        return None
    return sigma_1m * math.sqrt(horizon_min)


def term_structure(candles: CandleSeq, t: float,
                   windows_min=(60, 240, 720, 1440)) -> dict:
    """Realized 1-minute sigma at several trailing windows, plus the ratio
    of the longest to the shortest. A ratio far from 1 says the last half
    hour is not the day."""
    out = {}
    for m in windows_min:
        out[f"rv_{m}m"] = realized_1m(candles, t, m)
    lo, hi = out.get(f"rv_{windows_min[0]}m"), out.get(f"rv_{windows_min[-1]}m")
    out["long_over_short"] = (hi / lo) if lo and hi else None
    return out


def realized_forward(candles: CandleSeq, t: float,
                     horizon_min: float) -> Optional[float]:
    """The quantity every estimator is trying to predict: the absolute log
    move from the last close at/before t to the last close at/before
    t+horizon. FOR EVALUATION ONLY — it reads the future by construction
    and must never be a feature."""
    before = [c for c in candles if c["ts"] + 60.0 <= t]
    after = [c for c in candles if c["ts"] + 60.0 <= t + horizon_min * 60.0]
    if not before or not after or after[-1] is before[-1]:
        return None
    a, b = before[-1]["close"], after[-1]["close"]
    return abs(math.log(b / a)) if a > 0 and b > 0 else None


def gbm_candles(n: int, sigma_1m: float, s0: float = 50000.0,
                seed: int = 7, drift_1m: float = 0.0) -> list:
    """Synthetic 1-minute GBM candles with a KNOWN sigma, for testing.
    Deterministic for a given seed; no numpy, so identical everywhere."""
    import random
    rng = random.Random(seed)
    out, s = [], s0
    for i in range(n):
        o = s
        path = [o]
        for _ in range(60):                       # 60 sub-steps per minute
            path.append(path[-1] * math.exp(
                drift_1m / 60 + sigma_1m / math.sqrt(60) * rng.gauss(0, 1)))
        s = path[-1]
        out.append({"ts": float(i * 60), "open": o, "high": max(path),
                    "low": min(path), "close": s})
    return out
