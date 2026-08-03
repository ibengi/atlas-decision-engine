"""replay_btc.py — QA3b point-in-time replay harness for the live BTC 15m model.

WHAT THIS IS
============
A point-in-time replay of the production probability model
``btc_probability_model.probability_yes`` over aligned 15-minute KXBTC15M-style
event windows. It produces the first genuine (probability, outcome) dataset for
the audit: one row per (event, decision point), where every input of a row is
derived ONLY from 1-minute candles with ``ts <= decision_ts``.

FIDELITY CONTRACT (hard rule of the audit, QA3b)
================================================
* Probabilities come from the REAL production callable
  ``btc_probability_model.probability_yes`` — imported, never re-derived.
  (``ModelOutput.probability_yes`` in strategy_router.py is a dataclass FIELD,
  not the callable; the callable entry point is the module-level function.)
* sigma_1m, the 5-minute return and the data-quality score come from the
  ENGINE's own estimators, executed by the ENGINE's own code:
  ``btc_context.get_btc_context`` is called with injected point-in-time
  fetchers (spot sources serving the candle close at ``decision_ts``, a klines
  provider serving the last <=30 candles with ``ts <= decision_ts``). This is
  the "thin point-in-time adapter" — we feed historical candles through the
  same interface the engine uses in production; we copy no maths.
* NO model defect is fixed here. The missing -0.5*sigma^2*T convexity term,
  the momentum +/-0.5 clip, the spot-proxy strike fallback, the clamps and the
  data-availability confidence score are all deliberately preserved and
  measured as-is. This file adds columns (baselines, staleness arms) but never
  modifies engine behaviour.
* A hard runtime guard asserts that no candle with ts > decision_ts is ever
  presented to the engine (``guard_no_lookahead``).

EXCLUSIONS
==========
Any event or decision point not constructible from present candles is EXCLUDED
and counted by reason — never interpolated, never forward-filled.

OUTPUTS
=======
* ``--out``: gzip CSV (gitignored; deterministic gzip via mtime=0 so two runs
  hash identically)
* ``--summary``: JSON with counts / base rate / clip-binding rates / runtime
* ``--card``: REPLAY-DATA-CARD.md (committed) rendered from the run

Run from the repo root::

    .venv/bin/python research/replay_btc.py --start 2026-02-01 --end 2026-02-28

The same command with a later --end runs the full six-month replay unchanged.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ── make the repo root importable (run as a script from anywhere) ────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── production modules (repo root must be on sys.path — run from repo root) ──
from btc_probability_model import (  # noqa: E402
    MODEL_VERSION, MOMENTUM_CAP, P_CEIL, P_FLOOR,
    ModelInputError, confidence_from_quality, norm_cdf, probability_yes,
)
from btc_context import get_btc_context  # noqa: E402
from research.btc_candle_loader import CandleCursor, missing_ranges  # noqa: E402
from research.btc_candle_quality import STEP_S  # noqa: E402

# ── replay grid constants ────────────────────────────────────────────────────
EVENT_GRID_MIN = 15            # aligned 15-minute windows (:00/:15/:30/:45)
DECISION_MINUTES = (1, 2, 3, 5, 8, 10, 15)   # sampled minutes_remaining
LOOKBACK_CANDLES = 30          # engine fetches the last 30 klines
MIN_KLINES = 11                # engine MIN_KLINES (btc_context.py)
SPOT_SOURCE_NAMES = ("coinbase", "kraken", "bitstamp")
TERCILE_MIN_PAST = 30          # minimum past rows before vol tercile is set
TERCILE_FRACS = (1.0 / 3.0, 2.0 / 3.0)
SPOT_PROXY_CONFIDENCE_CAP = 6  # strategy_router EXTENDED_HORIZON_CONFIDENCE

COLUMNS = [
    "event_id",                # = expiry_ts (unix s)
    "decision_ts",             # unix s
    "expiry_ts",               # unix s
    "minutes_remaining",       # int 1/2/3/5/8/10/15
    "hour_of_day_utc",         # int 0..23
    "date_utc",                # "YYYY-MM-DD" (decision date)
    "spot",                    # close of the candle at decision_ts
    "strike",                  # see strike rule in the data card
    "sigma_hat_1m",            # engine estimator: pstdev of 29 log returns
    "T_minutes",               # float horizon (== minutes_remaining)
    "d",                       # ln(spot/strike) / (sigma * sqrt(T))
    "p_yes_raw",               # Phi(d + mu_postclip), BEFORE P_FLOOR/P_CEIL
    "p_yes",                   # production probability_yes (AFTER clamping)
    "confidence",              # production confidence_from_quality (0..10)
    "momentum_term_preclip",   # (ret_5m/5) * T / (sigma*sqrt(T))
    "momentum_term_postclip",  # clamp(+-0.5, preclip)
    "clip_binding",            # 1 if postclip != preclip (momentum cap bound)
    "sign_5m_return",          # sign(ret_5m) in {-1, 0, 1}
    "abs_r5",                  # |ret_5m|
    "ret_5m",                  # engine estimator: ln(C_t / C_{t-5})
    "p_gbm_honest",            # Phi(d - 0.5*sigma*sqrt(T)) — momentum removed,
                               #   convexity restored (pre-reg B3), unclamped
    "p_no_momentum",           # Phi(d), unclamped
    "p_convexity_corrected",   # Phi(d + mu_postclip - 0.5*sigma*sqrt(T)), unclamped
    "p_yes_lag60",             # production p with spot lagged 60 s (clamped)
    "p_yes_lag120",            # production p with spot lagged 120 s (clamped)
    "vol_tercile",             # causal tercile of sigma_hat_1m (1/2/3, '' if
                               #   < TERCILE_MIN_PAST past rows)
    "used_spot_proxy_strike",  # 1 if strike fell back to spot (see data card)
    "data_gap_flag",           # 1 if any minute in the input window is missing
    "y",                       # 1 if close at expiry_ts > strike else 0
]

EXCLUDED = "excluded"
EMITTED = "emitted"


# ── small utilities ──────────────────────────────────────────────────────────

def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True, timeout=10)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _utc_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _iso_date(ts: int) -> str:
    return _utc_dt(ts).strftime("%Y-%m-%d")


def parse_day(s: str) -> int:
    """YYYY-MM-DD (or YYYY-MM-DDTHH:MM) -> unix ts of that UTC midnight."""
    s = s.strip()
    if "T" in s:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M")
                   .replace(tzinfo=timezone.utc).timestamp())
    return int(datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def clamp_p(x: float) -> float:
    """Production probability clamp (btc_probability_model P_FLOOR/P_CEIL)."""
    return min(P_CEIL, max(P_FLOOR, x))


# ── no-lookahead guard (the anti-lookahead runtime assertion) ────────────────

def guard_no_lookahead(candles: List[dict], decision_ts: int) -> List[dict]:
    """Assert that no candle in the input set is stamped after decision_ts.

    This is the harness's hard point-in-time boundary. It is deliberately
    reachable from tests (the mutation test injects a future candle and
    asserts this guard raises).
    """
    for c in candles:
        if c["ts"] > decision_ts:
            raise AssertionError(
                f"LOOKAHEAD: candle ts={c['ts']} > decision_ts={decision_ts}; "
                f"point-in-time boundary violated")
    return candles


# ── point-in-time adapters for the engine's data interface ──────────────────

def make_spot_source(name: str, price: float, ts: float):
    """Adapter: one production spot provider serving the replay spot price."""
    def fetch():
        return {"source": name, "price": price, "ts": ts}
    fetch.__name__ = f"fetch_{name}"
    return fetch


def make_klines_fn(window: List[dict], decision_ts: int):
    """Adapter: production klines provider serving the last <=30 candles with
    ts <= decision_ts. Guards against lookahead at the engine boundary too."""
    def klines_fn():
        guard_no_lookahead(window, decision_ts)
        return window, {"http_status": 200, "elapsed_ms": 0.0, "error": None}
    klines_fn.__name__ = "fetch_klines_point_in_time"
    return klines_fn


# ── causal volatility-regime terciler (past rows only, no lookahead) ─────────

class CausalTerciler:
    """Assigns sigma_hat_1m to a low/med/high tercile using ONLY the
    distribution of sigma_hat_1m over previously emitted rows (strictly
    earlier decision_ts). Tercile edges are the 1/3 and 2/3 quantiles of the
    past sample (linear interpolation, numpy type-7 convention). Returns None
    until at least TERCILE_MIN_PAST past rows exist."""

    def __init__(self) -> None:
        self._sorted: List[float] = []

    def add(self, sigma: float) -> None:
        bisect.insort(self._sorted, sigma)

    def n_past(self) -> int:
        return len(self._sorted)

    def tercile(self, sigma: float) -> Optional[int]:
        n = len(self._sorted)
        if n < TERCILE_MIN_PAST:
            return None
        edges = []
        for q in TERCILE_FRACS:
            idx = q * (n - 1)
            lo = int(math.floor(idx))
            frac = idx - lo
            if lo + 1 < n:
                v = (self._sorted[lo] * (1.0 - frac)
                     + self._sorted[lo + 1] * frac)
            else:
                v = self._sorted[lo]
            edges.append(v)
        if sigma <= edges[0]:
            return 1
        if sigma <= edges[1]:
            return 2
        return 3


# ── one decision point ───────────────────────────────────────────────────────

def compute_row(decision_ts: int, expiry_ts: int, minutes_remaining: int,
                strike: float, used_spot_proxy_strike: bool,
                window: List[dict], data_gap_flag: bool) -> Dict:
    """Compute one replay row from a point-in-time window.

    ``window`` must be the ascending candles with ts <= decision_ts (last
    LOOKBACK_CANDLES are fed to the engine). Raises ``ExcludedRow`` with a
    reason string when the row cannot be constructed; every other path is
    deterministic. Raises AssertionError on any lookahead (never caught).
    The causal ``vol_tercile`` needs the row's own sigma, so the caller
    assigns it after the row returns.
    """
    guard_no_lookahead(window, decision_ts)          # hard boundary, first
    if not window or window[-1]["ts"] != decision_ts:
        raise ExcludedRow("candle_missing_at_decision_ts")
    spot = window[-1]["close"]
    kl_window = window[-LOOKBACK_CANDLES:]
    t = float(minutes_remaining)

    # Point-in-time adapters -> engine's own estimators via the engine's own
    # code path (get_btc_context with injected fetchers, no caching).
    sources = tuple(make_spot_source(n, spot, float(decision_ts))
                    for n in SPOT_SOURCE_NAMES)
    klines_fn = make_klines_fn(kl_window, decision_ts)
    ctx = get_btc_context(strike=strike, minutes_remaining=t,
                          spot_sources=sources, klines_fn=klines_fn,
                          now=float(decision_ts), use_cache=False)
    if not getattr(ctx, "valid", False):
        raise ExcludedRow("context_invalid:" + str(ctx.reason))
    sigma = ctx.realized_vol_1m
    ret_5m = (ctx.returns or {}).get("5m")
    if sigma is None or sigma <= 0:
        raise ExcludedRow("context_invalid:volatilite_nulle")
    if ret_5m is None or math.isnan(ret_5m) or math.isinf(ret_5m):
        raise ExcludedRow("model_input_error:ret_5m_invalide")

    denom = sigma * math.sqrt(t)
    d = math.log(ctx.spot / strike) / denom
    mu_pre = (float(ret_5m) / 5.0) * t / denom
    mu_post = max(-MOMENTUM_CAP, min(MOMENTUM_CAP, mu_pre))
    clip_binding = 1 if abs(mu_pre) > MOMENTUM_CAP else 0

    # Production probability — imported callable, clamped output.
    try:
        p = probability_yes(ctx.spot, strike, sigma, t, ret_5m=ret_5m)
    except ModelInputError as e:  # pragma: no cover — inputs pre-validated
        raise ExcludedRow(f"model_input_error:{e}") from None
    p_raw = norm_cdf(d + mu_post)                    # BEFORE the clamp
    conf = confidence_from_quality(ctx.data_quality_score, t)
    if used_spot_proxy_strike:                       # strategy-level rule
        conf = min(conf, SPOT_PROXY_CONFIDENCE_CAP)

    # Baselines on identical inputs (all unclamped raw probabilities).
    convex = 0.5 * sigma * math.sqrt(t)
    p_gbm_honest = norm_cdf(d - convex)              # momentum off, convexity on
    p_no_momentum = norm_cdf(d)                      # both off (production sans mu)
    p_convexity_corrected = norm_cdf(d + mu_post - convex)

    # Staleness arms: same production function, spot lagged a whole minute.
    p_lag60 = _lag_arm(window, decision_ts - 60, strike, sigma, t, ret_5m)
    p_lag120 = _lag_arm(window, decision_ts - 120, strike, sigma, t, ret_5m)

    return {
        "event_id": expiry_ts,
        "decision_ts": decision_ts,
        "expiry_ts": expiry_ts,
        "minutes_remaining": minutes_remaining,
        "hour_of_day_utc": _utc_dt(decision_ts).hour,
        "date_utc": _iso_date(decision_ts),
        "spot": ctx.spot,
        "strike": strike,
        "sigma_hat_1m": sigma,
        "T_minutes": t,
        "d": d,
        "p_yes_raw": p_raw,
        "p_yes": p,
        "confidence": conf,
        "momentum_term_preclip": mu_pre,
        "momentum_term_postclip": mu_post,
        "clip_binding": clip_binding,
        "sign_5m_return": (1 if ret_5m > 0 else (-1 if ret_5m < 0 else 0)),
        "abs_r5": abs(ret_5m),
        "ret_5m": ret_5m,
        "p_gbm_honest": p_gbm_honest,
        "p_no_momentum": p_no_momentum,
        "p_convexity_corrected": p_convexity_corrected,
        "p_yes_lag60": p_lag60 if p_lag60 is not None else "",
        "p_yes_lag120": p_lag120 if p_lag120 is not None else "",
        "vol_tercile": "",     # assigned by the caller (needs this row's sigma)
        "used_spot_proxy_strike": 1 if used_spot_proxy_strike else 0,
        "data_gap_flag": 1 if data_gap_flag else 0,
        "y": None,   # filled by the outcome pass
    }


def _lag_arm(window: List[dict], lag_ts: int, strike: float, sigma: float,
             t: float, ret_5m: float) -> Optional[float]:
    """Production p_yes with the spot read from the candle at lag_ts instead
    of decision_ts (a whole-minute spot lag; 30 s is not representable at
    1-minute granularity). None when the lagged candle is absent."""
    for c in reversed(window):
        if c["ts"] == lag_ts:
            try:
                return probability_yes(c["close"], strike, sigma, t,
                                       ret_5m=ret_5m)
            except ModelInputError:  # pragma: no cover
                return None
        if c["ts"] < lag_ts:
            break
    return None


class ExcludedRow(Exception):
    """A decision point that cannot be constructed from present candles."""


# ── event/decision scheduling ────────────────────────────────────────────────

def iter_event_schedule(start_ts: int, end_open_ts: int):
    """Yield (event_open_ts, expiry_ts, [(minutes_remaining, decision_ts)...])
    for every aligned 15-minute window with open in [start_ts, end_open_ts].

    Decisions are yielded in CHRONOLOGICAL order (largest minutes_remaining
    first: t=15 at window open, ..., t=1 at open+840 s), which is the order a
    forward-only candle cursor can consume without ever rewinding.
    """
    for w in range(start_ts, end_open_ts + 1, EVENT_GRID_MIN * 60):
        expiry = w + EVENT_GRID_MIN * 60
        decisions = [(m, expiry - m * 60)
                     for m in sorted(DECISION_MINUTES, reverse=True)]
        yield w, expiry, decisions


# ── the replay ───────────────────────────────────────────────────────────────

def _write_dataset_atomic(rows: List[dict], out_csv: str) -> dict:
    """Atomically write, fsync, publish, and verify a replay dataset."""
    tmp = out_csv + ".tmp"
    try:
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
                with io.TextIOWrapper(gz, encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                    w.writeheader()
                    for row in rows:
                        w.writerow(row)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, out_csv)
        count = 0
        with gzip.open(out_csv, "rt", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            if next(reader, None) != COLUMNS:
                raise RuntimeError("replay artifact header mismatch")
            for row in reader:
                if len(row) != len(COLUMNS):
                    raise RuntimeError("replay artifact malformed row")
                count += 1
        if count != len(rows):
            raise RuntimeError(f"replay artifact row count mismatch: expected {len(rows)}, got {count}")
        digest = hashlib.sha256()
        with open(out_csv, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"uncompressed_rows": count, "sha256": digest.hexdigest()}
    except Exception:
        try:
            os.unlink(out_csv)
        except FileNotFoundError:
            pass
        raise
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

def run_replay(cache_path: str, start_ts: int, end_open_ts: int,
               out_csv: str, out_summary: str, out_card: str) -> dict:
    t0 = time.time()
    start_dt = _utc_dt(start_ts)
    end_open_dt = _utc_dt(end_open_ts)
    last_expiry = end_open_ts + EVENT_GRID_MIN * 60

    schedule = list(iter_event_schedule(start_ts, end_open_ts))
    events_attempted = len(schedule)
    rows_attempted = events_attempted * len(DECISION_MINUTES)
    excl: Dict[str, int] = {}          # row exclusion reason -> count
    excl_events: Dict[str, int] = {}   # event-level exclusion reason -> count
    ties = 0
    emitted: List[dict] = []

    # Pre-scan gaps over the input span (one pass), for data_gap_flag.
    gaps = missing_ranges(cache_path,
                          start_ts - LOOKBACK_CANDLES * STEP_S,
                          last_expiry)

    def gap_overlaps(decision_ts: int) -> bool:
        win_lo = decision_ts - (LOOKBACK_CANDLES - 1) * STEP_S
        for g in gaps:
            if g.end_ts < win_lo:
                continue
            if g.start_ts > decision_ts:
                break
            if g.end_ts >= win_lo:
                return True
        return False

    # Pass 1: features, in chronological decision order.
    cursor = CandleCursor(cache_path)
    terciler = CausalTerciler()
    n_emitted = 0
    for w, expiry, decisions in schedule:
        # Strike rule: replay analog of Kalshi floor_strike = reference price
        # at event open (close of the candle stamped at the open minute w).
        # If that candle is absent, production falls back to spot proxy for
        # KXBTC15M (strike_proxy_allowed) — mirrored here and flagged.
        strike: Optional[float] = None
        used_proxy = False
        for m, decision_ts in decisions:
            # advance the cursor to decision_ts (grid-aligned, forward only)
            if cursor.current_ts is None:
                cursor.next_minute()
            while cursor.current_ts is not None and cursor.current_ts < decision_ts:
                cursor.next_minute()
            window = list(cursor.past(LOOKBACK_CANDLES))
            guard_no_lookahead(window, decision_ts)   # runtime boundary
            if m == EVENT_GRID_MIN:                   # t=15 -> decision at w
                if window and window[-1]["ts"] == w:
                    strike = window[-1]["close"]
                    used_proxy = False
                else:
                    # open candle missing -> production's spot-proxy fallback
                    # (strike := spot at decision time) — flagged for the
                    # analyst; d == 0 at t == 15 by construction.
                    strike = None
                    used_proxy = True
            if strike is None:
                # spot-proxy path: strike is the decision-time spot
                if not window or window[-1]["ts"] != decision_ts:
                    _bump(excl, "candle_missing_at_decision_ts")
                    continue
                strike_eff = window[-1]["close"]
                used_eff = True
            else:
                strike_eff = strike
                used_eff = used_proxy
            try:
                row = compute_row(decision_ts, expiry, m, strike_eff, used_eff,
                                  window, gap_overlaps(decision_ts))
            except ExcludedRow as e:
                _bump(excl, str(e))
                continue
            except AssertionError:
                raise
            # Causal vol tercile: computed against rows with strictly earlier
            # decision_ts (the ones already added to the terciler), then this
            # row's sigma joins the past set.
            row["vol_tercile"] = terciler.tercile(row["sigma_hat_1m"])
            terciler.add(row["sigma_hat_1m"])
            emitted.append(row)
            n_emitted += 1

    # Pass 2: outcomes — close at expiry_ts from a second forward iterator
    # (never from the pass-1 buffer; no lookahead by construction).
    outcome_close: Dict[int, float] = {}
    cur2 = CandleCursor(cache_path)
    pending = {expiry for _, expiry, _ in schedule}
    while True:
        t = cur2.next_minute()
        if t is None or t > last_expiry:
            break
        if t in pending and cur2.past(1) and cur2.past(1)[-1]["ts"] == t:
            outcome_close[t] = cur2.past(1)[-1]["close"]

    # Attach outcomes; drop events whose expiry candle is missing.
    kept: List[dict] = []
    event_ids = {r["event_id"] for r in emitted}
    for ev_id in event_ids:
        if ev_id not in outcome_close:
            _bump(excl_events, "outcome_candle_missing")
    for r in emitted:
        if r["event_id"] not in outcome_close:
            _bump(excl, "outcome_candle_missing")
            continue
        close_at_expiry = outcome_close[r["event_id"]]
        if close_at_expiry > r["strike"]:
            r["y"] = 1
        elif close_at_expiry < r["strike"]:
            r["y"] = 0
        else:
            r["y"] = 0
            ties += 1
        kept.append(r)

    # Publish only after atomic write and post-write verification.
    artifact = _write_dataset_atomic(kept, out_csv)
    # Fidelity check (pre-reg P-Sat-1) on the emitted rows.
    fid = fidelity_over_rows(kept)

    runtime_s = time.time() - t0
    summary = build_summary(
        cache_path, start_ts, end_open_ts, events_attempted, rows_attempted,
        kept, excl, excl_events, ties, fid, runtime_s,
        out_csv, out_summary, out_card, artifact)
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    write_data_card(summary, out_card)
    return summary


def _bump(d: Dict[str, int], k: str) -> None:
    d[k] = d.get(k, 0) + 1


# ── fidelity (pre-registered prediction #4, P-Sat-1) ─────────────────────────

def fidelity_over_rows(rows: List[dict]) -> dict:
    """Max |p_yes - clamp(Phi(d + 0.5*sign(r5)))| over momentum-capped rows,
    plus the all-rows reconstruction check as a diagnostic. Uses production's
    own norm_cdf and clamp constants."""
    capped = [r for r in rows if r["clip_binding"] == 1]
    max_dev_capped = 0.0
    max_dev_all = 0.0
    for r in rows:
        expected = clamp_p(norm_cdf(r["d"] + 0.5 * r["sign_5m_return"]
                                    if r["clip_binding"] == 1
                                    else r["d"] + r["momentum_term_postclip"]))
        dev = abs(r["p_yes"] - expected)
        if dev > max_dev_all:
            max_dev_all = dev
        if r["clip_binding"] == 1 and dev > max_dev_capped:
            max_dev_capped = dev
    return {
        "n_rows": len(rows),
        "n_capped": len(capped),
        "max_abs_dev_capped": max_dev_capped,
        "max_abs_dev_all": max_dev_all,
        "threshold": 1e-12,
        "pass": max_dev_capped <= 1e-12,
    }


# ── summary + data card ──────────────────────────────────────────────────────

def build_summary(cache_path, start_ts, end_open_ts, events_attempted,
                  rows_attempted, kept, excl, excl_events, ties, fid,
                  runtime_s, out_csv, out_summary, out_card, artifact=None) -> dict:
    n_emitted = len(kept)
    n_events_emitted = len({r["event_id"] for r in kept})
    y_vals = [r["y"] for r in kept]
    base_rate_rows = (sum(y_vals) / len(y_vals)) if y_vals else None
    # event-level base rate (primary analysis unit, pre-reg section 1.0)
    by_event = {}
    for r in kept:
        by_event.setdefault(r["event_id"], r["y"])
    ev_y = list(by_event.values())
    base_rate_events = (sum(ev_y) / len(ev_y)) if ev_y else None

    clip_by_t: Dict[str, float] = {}
    for m in DECISION_MINUTES:
        sub = [r for r in kept if r["minutes_remaining"] == m]
        if sub:
            clip_by_t[str(m)] = sum(r["clip_binding"] for r in sub) / len(sub)
    clip_overall = (sum(r["clip_binding"] for r in kept) / n_emitted
                    if n_emitted else None)
    excl_rows_total = rows_attempted - n_emitted
    excl_events_total = events_attempted - n_events_emitted
    return {
        "run": {
            "start_open_ts": start_ts,
            "end_open_ts": end_open_ts,
            "start_utc": _utc_dt(start_ts).isoformat(),
            "end_open_utc": _utc_dt(end_open_ts).isoformat(),
            "model_version": MODEL_VERSION,
            "momentum_cap": MOMENTUM_CAP,
            "p_floor": P_FLOOR,
            "p_ceil": P_CEIL,
            "git_sha": _git_sha(),
            "runtime_s": round(runtime_s, 2),
            "cache_file": os.path.basename(cache_path),
            "python": sys.version.split()[0],
            "decision_minutes": list(DECISION_MINUTES),
        },
        "counts": {
            "events_attempted": events_attempted,
            "rows_attempted": rows_attempted,
            "rows_emitted": n_emitted,
            "events_emitted": n_events_emitted,
            "rows_excluded": excl_rows_total,
            "events_excluded": excl_events_total,
            "excluded_rows_by_reason": {k: v for k, v in sorted(excl.items())},
            "excluded_events_by_reason": {k: v for k, v in
                                          sorted(excl_events.items())},
            "ties_close_eq_strike": ties,
        },
        "outcome": {
            "base_rate_rows": base_rate_rows,
            "base_rate_events": base_rate_events,
            "n_yes_rows": sum(y_vals),
            "n_yes_events": sum(ev_y),
        },
        "clip": {
            "n_capped_rows": sum(r["clip_binding"] for r in kept),
            "rate_overall": clip_overall,
            "rate_by_minutes_remaining": clip_by_t,
        },
        "fidelity": fid,
        "strike": {
            "n_field_like": sum(1 for r in kept
                                if not r["used_spot_proxy_strike"]),
            "n_spot_proxy": sum(r["used_spot_proxy_strike"] for r in kept),
        },
        "artifact": artifact or {},
        "outputs": {
            "csv": os.path.basename(out_csv),
            "summary": os.path.basename(out_summary),
            "card": out_card,
        },
        "columns": COLUMNS,
    }


def write_data_card(s: dict, out_path: str) -> None:
    r = s["run"]
    c = s["counts"]
    o = s["outcome"]
    cl = s["clip"]
    fid = s["fidelity"]
    rows = s["columns"]
    excl_rows = "; ".join(f"{k}={v}" for k, v in c["excluded_rows_by_reason"].items()) or "none"
    excl_ev = "; ".join(f"{k}={v}" for k, v in c["excluded_events_by_reason"].items()) or "none"
    clip_t = "; ".join(f"t={k}: {v*100:.1f}%" for k, v in
                       sorted(cl["rate_by_minutes_remaining"].items(),
                              key=lambda kv: int(kv[0])))
    card = f"""# REPLAY DATA CARD — btc15m point-in-time replay, six-month confirmatory run

Status: **QA3b step 2 of 2 — FULL SIX MONTHS (February–July 2026).** Generated by
`research/replay_btc.py` and committed as the provenance record. February validation-run
results remain recorded in the section below; all figures above describe this full run.

## Headline numbers

| Quantity | Value |
|---|---|
| Events attempted (15-min windows, open in replay period) | {c['events_attempted']} |
| Events emitted | {c['events_emitted']} |
| Rows attempted (7 decision points per event) | {c['rows_attempted']} |
| Rows emitted | {c['rows_emitted']} |
| Rows excluded (by reason) | {c['rows_excluded']} — {excl_rows} |
| Events excluded (by reason) | {c['events_excluded']} — {excl_ev} |
| Ties (close at expiry == strike) | {c['ties_close_eq_strike']} |
| Base rate of y, row-level | {o['base_rate_rows']:.4f} ({o['n_yes_rows']}/{c['rows_emitted']}) |
| Base rate of y, event-level | {o['base_rate_events']:.4f} ({o['n_yes_events']}/{c['events_emitted']}) |
| Momentum-cap binding rate, pooled | **{cl['rate_overall']*100:.1f}%** ({cl['n_capped_rows']} rows) |
| Momentum-cap binding rate per horizon | {clip_t} |
| **Fidelity max-abs-deviation (P-Sat-1, capped rows)** | **{fid['max_abs_dev_capped']:.3e}** ({'PASS ≤ 1e-12' if fid['pass'] else 'FAIL > 1e-12'}) |
| All-rows reconstruction max deviation (diagnostic) | {fid['max_abs_dev_all']:.3e} |
| Model version | {r['model_version']} (MOMENTUM_CAP={r['momentum_cap']}, P_FLOOR={r['p_floor']}, P_CEIL={r['p_ceil']}) |
| Git SHA (engine) | `{r['git_sha']}` |
| Runtime | {r['runtime_s']} s |
| Cache | {r['cache_file']} (verified 260,640 candles, zero gaps) |

> **The fidelity deviation is the audit's data-integrity gate (pre-registered
> prediction #4 / P-Sat-1).** If it exceeds 1e-12 the replay does NOT
> reproduce production and the analysis must stop. Run
> `research/check_replay_fidelity.py` on the CSV to re-verify independently.

## Strike rule (exact convention used)

For each aligned 15-minute event window `[W, W+900s)` (:00/:15/:30/:45 UTC):

- **`strike` := close of the 1-minute candle stamped at `W`** (the candle
  `[W-60s, W)`, whose close is the price at the instant the window opens).
  This is the replay's point-in-time analog of the Kalshi KXBTC15M
  `floor_strike` field, which the skill `kalshi-btc-ticker-strike-decoding`
  verified to be *≈ spot at open* (the market resolves YES if BRTI at close
  ≥ BRTI at open; `floor_strike` is the only price in the market).
- **Outcome `y` := 1 if `close at expiry_ts > strike` else 0** (task-specified;
  exact ties counted separately — Kalshi's published rule is ≥, but ties are
  measure-zero on continuous prices; `ties_close_eq_strike` counts them).
- **Fallback:** if the candle at `W` is absent (data gap), the strike falls
  back to **spot at decision time** — mirroring production
  (`BtcModelStrategy.strike_proxy_allowed=True` → `strike_source="spot_proxy"`).
  Such rows carry `used_spot_proxy_strike=1`, have `d ≡ 0` at
  `minutes_remaining=15`, and confidence capped at 6; the analyst must be able
  to exclude them (they can). No cache gaps in the six-month cache ⇒ 0 such rows here.
- Because strike and decision-time spot come from the SAME series, `d ≡ 0`
  exactly at `minutes_remaining=15` (decision at window open). This is a
  replay-specific degeneracy (production has a live basis between the 3-source
  spot median and Kalshi's BRTI floor_strike): the t=15 slice of this dataset
  is `p = Φ(momentum)` by construction. The pre-registered primary analysis
  unit is t=5; treat t=15 as degenerate.

## Column definitions

| Column | Type | Definition |
|---|---|---|
| event_id | int | = expiry_ts (unix s); all 7 rows of an event share it and share `y` |
| decision_ts | int | unix s of the decision |
| expiry_ts | int | unix s of settlement (window open + 900 s) |
| minutes_remaining | int | T ∈ {{1,2,3,5,8,10,15}} |
| hour_of_day_utc | int | hour of decision_ts in UTC |
| date_utc | str | decision date (UTC) |
| spot | float | close of the candle at decision_ts (replay's single-source spot; production uses a 3-exchange median — same-series optimism, see below) |
| strike | float | per the strike rule above |
| sigma_hat_1m | float | engine's own estimator: `statistics.pstdev` of the 29 log-returns of the last ≤30 klines ≤ decision_ts (btc_context.py:428-430) |
| T_minutes | float | horizon in minutes (== minutes_remaining) |
| d | float | ln(spot/strike) / (sigma_hat_1m · √T) |
| p_yes_raw | float | Φ(d + momentum_term_postclip) BEFORE P_FLOOR/P_CEIL clamping |
| p_yes | float | **production `probability_yes` output** (clamped to [P_FLOOR, P_CEIL]) |
| confidence | int | production `confidence_from_quality` (0-10). **Data-availability score, NOT forecast uncertainty** (pre-reg §6.1). In this dataset it is 10 for every row (3 injected sources, zero dispersion, full klines) — a constant; do not analyse it as confidence |
| momentum_term_preclip | float | (ret_5m/5) · T / (sigma·√T) — the model's momentum drift term, unclipped |
| momentum_term_postclip | float | clamp(±MOMENTUM_CAP, preclip) |
| clip_binding | int | 1 if the ±0.5 momentum cap bound (postclip ≠ preclip) |
| sign_5m_return | int | sign(ret_5m) ∈ {{-1,0,1}} |
| abs_r5 | float | abs(ret_5m) |
| ret_5m | float | engine's own estimator: ln(C_t / C_{{t-5}}) (btc_context returns["5m"]) |
| p_gbm_honest | float | Φ(d − ½·sigma·√T) — momentum removed AND convexity restored (pre-reg B3), unclamped |
| p_no_momentum | float | Φ(d) — momentum removed, convexity NOT restored, unclamped |
| p_convexity_corrected | float | Φ(d + mu_postclip − ½·sigma·√T) — momentum kept, convexity added, unclamped |
| p_yes_lag60 | float | production `probability_yes` with spot = close at decision_ts − 60 s (all other features as of decision_ts), clamped. Empty when that candle is absent |
| p_yes_lag120 | float | same with a 120 s lag. Empty when absent |
| vol_tercile | int (1/2/3) or empty | **Causal** volatility regime: tercile of this row's sigma_hat_1m against the empirical ⅓/⅔ quantiles of sigma_hat_1m over all EMITTED rows with strictly earlier decision_ts (expanding window, numpy type-7 linear interpolation). Empty until ≥ 30 past rows exist. Full-sample quantiles would be lookahead and are forbidden |
| used_spot_proxy_strike | int | 1 if the strike rule fell back to spot (0 in February — zero gaps) |
| data_gap_flag | int | 1 if any minute in the row's 30-minute input window [decision_ts−29m, decision_ts] is missing from the cache (0 everywhere in February) |
| y | int | 1 if close at expiry_ts > strike else 0 (same for all rows of an event) |

**NO market-probability column — NOT MEASURABLE.** The replay has no
historical Kalshi quotes (the client cannot fetch historical bid/ask). Any
market-probability column would have to be fabricated. Edge-vs-market is a
separate live-quote workstream (pre-reg §6.2); this dataset measures the
model's forecast quality only.

## Event grid & scheduling

- Windows aligned to :00/:15/:30/:45 UTC, 15 minutes long.
- Decision points per event: `minutes_remaining ∈ {{1,2,3,5,8,10,15}}`, i.e.
  decision_ts = expiry − T·60 s ∈ [W, W+840 s]. 7 rows per event; all rows of
  an event share one outcome (the pre-registered √7 dependence — never treat
  rows as independent events).
- Every input of a row derives ONLY from candles with ts ≤ decision_ts,
  enforced by `guard_no_lookahead` (asserts at runtime; the mutation test
  proves it can fail). The outcome is read in a separate forward pass, never
  from the input buffer.

## Exclusion accounting (reconciliation)

- events_attempted = {c['events_attempted']} = 28 days × 96 windows; rows_attempted = {c['rows_attempted']}.
- rows_emitted = {c['rows_emitted']}; rows_excluded = {c['rows_excluded']}: {excl_rows}.
- events_emitted = {c['events_emitted']}; events_excluded = {c['events_excluded']}: {excl_ev}.
- The three February exclusions are the first event's t=15/t=10/t=8 decision
  points (00:00/00:05/00:07 UTC on 2026-02-01): fewer than the engine's
  MIN_KLINES=11 candles exist yet (the cache starts at 00:00). Excluded, never
  interpolated. The February validation run had 3 boundary exclusions; this full run additionally records the final event outcome gap.

## Staleness arm — impossibility notes

- **A 30-second lag is NOT representable at 1-minute granularity** and is not
  faked: the dataset carries whole-minute lags (p_yes_lag60/p_yes_lag120)
  only. Pre-reg §0.3 asked for 30/60/90 s; 60/120 s is the honest 1-minute
  grid equivalent. The 90 s production staleness limit (MAX_PRICE_AGE_S=90)
  sits between the two arms; live degradation is bracketed by them.
- The replay reads spot EXACTLY at decision_ts (age 0). Production permits up
  to 90 s of spot staleness and up to 3 min of kline staleness; neither is
  modelled in the baseline columns (only in the lag arms). Replay numbers are
  therefore optimistic relative to live, on top of the BRTI basis below.

## Optimistic-bias statement (required, non-negotiable)

Features (`spot`, `sigma`, `ret_5m`) and outcome (`close at expiry`) come from
the SAME Binance BTCUSDT series, while Kalshi settles KXBTC15M on the CF
Benchmarks **BRTI** index. The replay therefore removes the model-spot-vs-
settlement basis that exists live. **Replay calibration / Brier / ECE are an
OPTIMISTIC LOWER BOUND on live performance and must NEVER be quoted as live
performance.** Per the pre-registration (§0.2, §6.9), ~1-2% of events would
flip under true BRTI settlement (label noise attenuating the calibration
slope by ~0.02-0.04), and the replay's own Brier is a lower bound on live
Brier. The replay period was a net BTC downtrend (≈78,700 → ≈62,900), which
matters when interpreting directional bias.

## Determinism & reproduction

- Output is gzip with mtime=0 and fixed column order ⇒ two runs over the same
  window produce byte-identical files (tested).
- Run: `.venv/bin/python research/replay_btc.py --start 2026-02-01 --end 2026-02-28`
  from the repo root. The same command with `--end 2026-07-31` runs the full
  six-month replay (separate follow-up task — do NOT run it in this task).
- Probabilities: imported `btc_probability_model.probability_yes` (the
  callable entry point — `ModelOutput.probability_yes` in strategy_router.py
  is a dataclass field, not the callable). sigma/returns/quality: the engine's
  own `btc_context.get_btc_context` with injected point-in-time fetchers.
  No model defect was fixed; the baseline is the model exactly as it stands.
- Analysis scripts may read the CSV with `pandas.read_csv(..., compression="gzip")`.

## Column list (as written to the CSV)

{{rows}}
"""
    # render the column list (markdown table needs manual join)
    col_table = "| # | column |\n|---|--------|\n" + "".join(
        f"| {i} | {c} |\n" for i, c in enumerate(rows))
    card = card.replace("{{rows}}", col_table)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(card)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-02-01", help="first event-open day (UTC)")
    ap.add_argument("--end", default="2026-02-28", help="last event-open day (UTC)")
    ap.add_argument("--cache", default="research/data/btc_1m_binance.csv.gz")
    ap.add_argument("--out", default="research/data/replay_btc_15m_2026-02.csv.gz")
    ap.add_argument("--summary", default="research/data/replay_summary_2026-02.json")
    ap.add_argument("--card", default="research/REPLAY-DATA-CARD.md")
    args = ap.parse_args(argv)
    start_ts = parse_day(args.start)
    end_day_ts = parse_day(args.end)
    # last event open: 23:45 of the end day
    end_open_ts = end_day_ts + 23 * 3600 + 45 * 60
    summary = run_replay(args.cache, start_ts, end_open_ts,
                         args.out, args.summary, args.card)
    s = summary
    print(f"events: {s['counts']['events_attempted']} attempted -> "
          f"{s['counts']['events_emitted']} emitted")
    print(f"rows:   {s['counts']['rows_attempted']} attempted -> "
          f"{s['counts']['rows_emitted']} emitted "
          f"({s['counts']['rows_excluded']} excluded)")
    print(f"base rate (rows): {s['outcome']['base_rate_rows']:.4f} | "
          f"(events): {s['outcome']['base_rate_events']:.4f}")
    print(f"clip-binding rate: {s['clip']['rate_overall']*100:.1f}% overall")
    print(f"fidelity max-abs-dev (capped rows): "
          f"{s['fidelity']['max_abs_dev_capped']:.3e} "
          f"({'PASS' if s['fidelity']['pass'] else 'FAIL'})")
    print(f"card: {args.card}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
