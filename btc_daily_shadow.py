"""
btc_daily_shadow.py — T7-K. Pre-liquidity shadow evidence for BtcDailyStrategy.

THE SEPARATION THIS MODULE EXISTS TO ENFORCE
    Trading eligibility and model observability are different questions. A
    KXBTCD market with no order book can never be traded — and never is. It
    can still be *predicted* and it still *settles*, so it is a valid
    calibration subject.

    This module evaluates exactly those markets, for evidence only. It cannot
    make anything tradeable: it imports no risk, order, or position component,
    never constructs a `Decision`, and its result type has no `accepted`
    field for anything downstream to read.

WHY THIS IS POSSIBLE AT ALL
    `_BtcAboveStrikeBase.evaluate(market, book, minutes_remaining)` mentions
    `book` only in its signature — it derives the strike from the market and
    spot/volatility from the BTC context, and reads no order-book field. An
    empty book therefore blocks execution without blocking prediction.

POPULATION (all seven must hold; see T7-K Phase 2)
    open · inside the existing scanner window · classifies as
    btc_above_strike_daily · routes to a strategy with a probability source ·
    model inputs valid · rejected for exactly no_liquidity · therefore
    ineligible for execution.

DEDUPLICATION
    The same ~80 markets recur every cycle. Recording per cycle would
    manufacture thousands of correlated rows and make N meaningless. Instead
    each ticker yields at most ONE observation per pre-registered
    time-to-expiry checkpoint, per model version. The dedup key contains no
    timestamp, so retries and process restarts cannot duplicate a row.

OFF BY DEFAULT
    `BTC_DAILY_SHADOW_ENABLED` (default false), matching this repository's
    convention that new paths ship inert. With the flag off, `run()` returns
    immediately and no market is evaluated. Rollback is unsetting the
    variable — no redeploy.
"""

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from market_classifier import classify
from market_scanner import _has_liquidity, liquidity_diag

log = logging.getLogger("SHADOW_DAILY")

SHADOW_VERSION = "t7k-shadow-1"
SHADOW_ORIGIN = "shadow_pre_liquidity"
DAILY_MARKET_TYPE = "btc_above_strike_daily"

#: Pre-registered time-to-expiry checkpoints, minutes, descending.
#:
#: Chosen from the model's ACTUAL declared domain, not by convention. The
#: daily strategy's non-extended domain is max_minutes=1560 (26h) and the
#: scanner admits 5..1440 minutes, so every checkpoint below sits inside both
#: — each observation is therefore `horizon_mode="normal"`, free of the
#: extended-horizon confidence cap, and calibrates the model where it is
#: primarily used. Deliberately excluded: anything above 1440 (unreachable
#: through the scanner) and anything below 60 (inside the 5-minute floor's
#: noise, and KXBTCD is not a minute-scale instrument).
CHECKPOINTS = ((1440, "24h"), (720, "12h"), (360, "6h"),
               (180, "3h"), (60, "1h"))


def _env_b(name, *, default):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_i(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def checkpoint_for(minutes_remaining) -> Optional[tuple]:
    """The single checkpoint a market is observed at, or None.

    A market at m minutes belongs to the SMALLEST checkpoint >= m, so as it
    ages it crosses 24h -> 12h -> 6h -> 3h -> 1h, firing each exactly once.
    Above the largest checkpoint, or at/below zero, there is no observation.
    """
    try:
        m = float(minutes_remaining)
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None
    best = None
    for mins, label in CHECKPOINTS:
        if m <= mins and (best is None or mins < best[0]):
            best = (mins, label)
    return best


def observation_key(ticker, model_version, checkpoint_label) -> str:
    """Stable identity of one observation. Contains NO timestamp, so the same
    market at the same checkpoint is the same observation forever."""
    return f"{ticker}|{model_version}|{checkpoint_label}"


def shadow_record_id(obs_key: str) -> str:
    return hashlib.sha1(f"shadow|{obs_key}".encode("utf-8")).hexdigest()[:16]


@dataclass
class ShadowObservation:
    """Result of one shadow evaluation.

    Deliberately NOT a `Decision` and deliberately without an `accepted`
    field: there is no attribute here for the candidate list, risk engine, or
    order manager to consume, so a shadow prediction cannot be mistaken for a
    tradeable one by any code that reads it.
    """
    ticker: str
    observation_key: str
    checkpoint_label: str
    checkpoint_minutes: int
    minutes_remaining: float
    model_version: Optional[str]
    probability_yes: Optional[float]
    confidence: Optional[int]
    recorded: bool
    skip_reason: Optional[str] = None


class BtcDailyShadowEvaluator:
    """Evaluates no-liquidity KXBTCD markets for evidence only.

    Imports no risk, order, or position component — the isolation is
    structural, not conventional.
    """

    def __init__(self, router, store, now_fn=None, enabled=None):
        self.router = router
        self.store = store
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.enabled = (_env_b("BTC_DAILY_SHADOW_ENABLED", default=False)
                        if enabled is None else bool(enabled))
        self.max_per_cycle = _env_i("SHADOW_DAILY_MAX_PER_CYCLE", 200)
        # Restart semantics: the dedup index is rebuilt from the store, so a
        # redeploy or crash cannot re-record an observation already on disk.
        try:
            self._seen = store.observation_keys()
        except Exception as e:                            # noqa: BLE001
            log.debug(f"[SHADOW_DAILY] dedup index unavailable: {e}")
            self._seen = set()

    def _counters(self):
        return {"shadow_daily_considered": 0, "shadow_daily_predicted": 0,
                "shadow_daily_deduplicated": 0, "shadow_daily_invalid": 0,
                "shadow_daily_out_of_scope": 0,
                "shadow_daily_no_checkpoint": 0,
                "shadow_daily_write_failed": 0,
                "shadow_daily_capped": 0}

    def run(self, markets, cycle_id=None) -> dict:
        """Evaluate a no-liquidity population. Returns counters only.

        Never raises. Never returns anything the trading path consumes.
        """
        S = self._counters()
        if not self.enabled or not markets:
            return S
        try:
            return self._run(markets, cycle_id, S)
        except Exception as e:                            # noqa: BLE001
            log.warning(f"[SHADOW_DAILY] aborted: {e}")
            return S

    def _run(self, markets, cycle_id, S) -> dict:
        strat = self.router.route(DAILY_MARKET_TYPE) if self.router else None
        if strat is None or not strat.has_probability_source():
            S["shadow_daily_out_of_scope"] = len(markets)
            return S
        model_ok = 0
        for m in markets:
            if model_ok >= self.max_per_cycle:
                S["shadow_daily_capped"] += 1
                continue
            S["shadow_daily_considered"] += 1
            obs = self._observe(m, strat, cycle_id, S)
            if obs is not None and obs.recorded:
                model_ok += 1
        return S

    def _observe(self, m, strat, cycle_id, S) -> Optional[ShadowObservation]:
        # (1) open, and (6) genuinely illiquid. Both re-checked here rather
        # than trusted from the caller: this population must never silently
        # widen to markets that are tradeable.
        status = str(m.get("status") or "").lower()
        if status and status not in ("open", "active"):
            S["shadow_daily_out_of_scope"] += 1
            return None
        if _has_liquidity(m):
            S["shadow_daily_out_of_scope"] += 1
            return None

        # (2) inside the existing scanner window — never widened here.
        mins = m.get("_minutes_remaining")
        cp = checkpoint_for(mins)
        if cp is None:
            S["shadow_daily_no_checkpoint"] += 1
            return None
        cp_minutes, cp_label = cp

        # (3) and (4) classification and routing.
        c = classify(m)
        if c.market_type != DAILY_MARKET_TYPE:
            S["shadow_daily_out_of_scope"] += 1
            return None

        # (5) model inputs valid — the model itself is the judge. `book=None`
        # is passed deliberately: the daily model reads no order-book field,
        # and supplying a synthetic book would be fabricating market data.
        model = strat.evaluate(m, None, mins)
        if not model.valid or model.probability_yes is None:
            S["shadow_daily_invalid"] += 1
            return None

        feats = model.features or {}
        mv = feats.get("model_version")
        key = observation_key(m.get("ticker"), mv, cp_label)
        if key in self._seen:
            S["shadow_daily_deduplicated"] += 1
            return ShadowObservation(
                ticker=m.get("ticker"), observation_key=key,
                checkpoint_label=cp_label, checkpoint_minutes=cp_minutes,
                minutes_remaining=mins, model_version=mv,
                probability_yes=model.probability_yes,
                confidence=model.confidence, recorded=False,
                skip_reason="duplicate_observation")

        diag = liquidity_diag(m)
        rid = self.store.record(
            # A dict shaped like a decision, NOT a Decision. `accepted` is
            # hard-false and the rejection reason is fixed: this row can only
            # ever describe a market the engine refused to trade.
            decision={"ticker": m.get("ticker"), "strategy": strat.name,
                      "market_type": c.market_type, "decision_id": None,
                      "accepted": False, "rejection_reason": "no_liquidity",
                      "net_edge": None, "gross_edge": None, "net_ev": None,
                      "entry_ask": None, "market_probability": None},
            model_output=model.to_dict(), market=m, book=None,
            minutes_remaining=mins, cycle_id=cycle_id,
            record_id=shadow_record_id(key),
            extra={"origin": SHADOW_ORIGIN,
                   "shadow_version": SHADOW_VERSION,
                   "observation_key": key,
                   "observation_checkpoint": cp_label,
                   "observation_checkpoint_minutes": cp_minutes,
                   "execution_eligible": False,
                   "liquidity_diagnostic": diag})
        if rid is None:
            S["shadow_daily_write_failed"] += 1
            return ShadowObservation(
                ticker=m.get("ticker"), observation_key=key,
                checkpoint_label=cp_label, checkpoint_minutes=cp_minutes,
                minutes_remaining=mins, model_version=mv,
                probability_yes=model.probability_yes,
                confidence=model.confidence, recorded=False,
                skip_reason="write_failed")

        self._seen.add(key)
        S["shadow_daily_predicted"] += 1
        return ShadowObservation(
            ticker=m.get("ticker"), observation_key=key,
            checkpoint_label=cp_label, checkpoint_minutes=cp_minutes,
            minutes_remaining=mins, model_version=mv,
            probability_yes=model.probability_yes,
            confidence=model.confidence, recorded=True)
