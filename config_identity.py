# -*- coding: utf-8 -*-
"""AIR-001: content identities for the configuration that governs trading.

Two stable hashes over the exact values in effect at call time:
- strategy_config_hash(): every knob that shapes WHICH trades are taken
  (economic gates, pricing buffers, fees);
- risk_config_hash(): every knob that bounds HOW MUCH is at risk.

They bind ValidatedExecutionIntent (Wave 2), RiskProof (Wave 6) and the
LiveCertificationManifest (Wave 8) to the real configuration — a config
change changes the hash, so stale consent or stale certification can
never silently cover new behavior. Values are read from CFG at call time
(post-import patches included), serialized canonically, hashed SHA-256.
"""
import hashlib
import json

from config import CFG

STRATEGY_KNOBS = (
    "MIN_MODEL_CONFIDENCE", "MIN_GROSS_EDGE", "MIN_NET_EDGE", "MIN_NET_EV",
    "MAX_ACCEPTABLE_SPREAD", "MAX_ENTRY_CENTS", "SLIPPAGE_BUFFER_CENTS",
    "FEE_RATE", "MIN_MARKET_SCORE", "MIN_FILL_PROXY",
)

RISK_KNOBS = (
    "MAX_DAILY_LOSS", "MAX_DAILY_LOSS_PCT", "MAX_CONSECUTIVE_LOSSES",
    "CONSECUTIVE_LOSS_COOLDOWN_S", "MAX_TRADES_CYCLE", "MAX_POS_PCT",
    "RISK_BUDGET_PCT", "DD_THROTTLE_PCT", "MAX_OPEN_POSITIONS",
    "MAX_PORTFOLIO_RISK_PCT", "MAX_CORRELATION_GROUP_PCT",
    "PORTFOLIO_DRAWDOWN_THROTTLE_PCT", "DRAWDOWN_THROTTLE_FACTOR",
    "MAX_CATEGORY_RISK_PCT", "MAX_SINGLE_MARKET_RISK_PCT",
    "MAX_EQUITY_DRAWDOWN_PCT", "KELLY_ENABLED", "KELLY_FRACTION",
    "KELLY_MAX_POS_PCT", "KELLY_MIN_BET",
)


def _hash_knobs(names):
    values = {}
    for name in names:
        # A missing knob is recorded as missing — never defaulted to a
        # value, so two builds with different knob sets hash differently.
        values[name] = repr(getattr(CFG, name, "<ABSENT>"))
    raw = json.dumps(values, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def strategy_config_hash() -> str:
    return _hash_knobs(STRATEGY_KNOBS)


def risk_config_hash() -> str:
    return _hash_knobs(RISK_KNOBS)
