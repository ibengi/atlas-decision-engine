"""Exact deployed-source function witnesses. No broker/config/engine imports.

Copies only the named AST declarations from strategy_router, unchanged, so its
probability path can be exercised without importing any broker-related module.
All spot and candle providers are local deterministic functions. Socket creation
and all socket connect/send operations are denied by an audit hook.
"""
import ast
import dataclasses
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import typing

ROOT = Path(__file__).resolve().parent
SOURCE = Path(os.environ["LI06_SOURCE_DIR"])
SHA = "5c4e7897a0b99065f3a23bc4ed834b44dba9580c"
NETWORK_ATTEMPTS = []


def deny_network(event, args):
    if event.startswith("socket."):
        NETWORK_ATTEMPTS.append(event)
        raise RuntimeError("Synthetic audit: sockets forbidden")


sys.addaudithook(deny_network)
os.environ["BTC_CONTEXT_CYCLE_CACHE"] = "0"
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bc = load("btc_context")
load("btc_probability_model")
declarations = {
    "ModelOutput", "Strategy", "_strike_with_source", "_BtcAboveStrikeBase",
    "BtcModelStrategy",
}
tree = ast.parse((SOURCE / "strategy_router.py").read_text())
nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
         and n.name in declarations]
assert len(nodes) == len(declarations)
ns = dict(dataclass=dataclasses.dataclass, field=dataclasses.field,
          asdict=dataclasses.asdict, Optional=typing.Optional,
          Callable=typing.Callable)
exec(compile(ast.Module(body=nodes, type_ignores=[]),
             "exact-deployed-strategy-router-slice", "exec"), ns)

NOW = 1_800_000_000.0


def spots(now):
    return tuple((lambda name=name: {"source": name, "price": 65000.0,
                                     "ts": now})
                 for name in ("synthetic-a", "synthetic-b", "synthetic-c"))


def candles(end, spacing=60):
    return [{"ts": end - (29 - i) * spacing, "open": 65000.0,
             "high": 65040.0, "low": 64980.0,
             "close": 65000.0 + (25.0 if i % 2 else -15.0), "volume": 5.0}
            for i in range(30)]


def serving(rows):
    return lambda limit=30: (rows, {"http_status": 200, "error": None})


def down(limit=30):
    return None, {"http_status": 451, "error": "synthetic HTTP 451"}


def result(name, providers, now=NOW, seed=False):
    bc.clear_cache()
    bc._last_good_klines.update(kl=None, ts=0.0, provider=None)
    if seed:
        bc.fetch_klines_with_fallback(
            providers=[("kraken", serving(candles(NOW - 60)))], now=NOW)
    original = bc.fetch_klines_with_fallback
    calls = []

    def call(pname, fn):
        def invoke(limit):
            calls.append(pname)
            return fn(limit)
        return invoke

    injected = [(pname, call(pname, fn)) for pname, fn in providers]
    bc.fetch_klines_with_fallback = lambda **kw: original(
        providers=injected, now=kw.get("now", now))
    try:
        ctx = bc.get_btc_context(strike=65000.0, minutes_remaining=10.0,
                                 spot_sources=spots(now), use_cache=False,
                                 now=now)
    finally:
        bc.fetch_klines_with_fallback = original
    output = ns["BtcModelStrategy"](context_provider=lambda **kw: ctx).evaluate(
        {"floor_strike": 65000.0}, {}, 10.0)
    return {"case": name, "context_valid": ctx.valid, "reason": ctx.reason,
            "quality": ctx.data_quality_score, "flags": ctx.quality_flags,
            "model_valid": output.valid, "model_reason": output.reason,
            "probability_yes": output.probability_yes,
            "model_features": output.features, "synthetic_providers_called": calls}


CASES = [
    result("fresh_binance_positive_control", [("binance", serving(candles(NOW - 60)))]),
    result("binance_451_kraken_fallback", [("binance", down), ("kraken", serving(candles(NOW - 60)))]),
    result("complete_outage_no_cache", [("binance", down), ("kraken", down), ("coinbase", down)]),
    result("complete_outage_3min_cache", [("binance", down), ("kraken", down)], NOW + 180, True),
    result("complete_outage_expired_cache", [("binance", down), ("kraken", down)], NOW + 601, True),
    result("future_candles_365days", [("kraken", serving(candles(NOW + 365 * 86400)))]),
    result("hourly_candles_treated_as_1min", [("kraken", serving(candles(NOW - 60, spacing=3600)))]),
    result("stale_first_source_prevents_healthy_fallback", [("binance", serving(candles(NOW - 3600))), ("kraken", serving(candles(NOW - 60)))]),
]
assert CASES[0]["model_valid"] and CASES[1]["model_valid"]
assert not CASES[2]["model_valid"]
assert CASES[3]["model_valid"] is (os.environ["LI06_EXPECT_AFTER"] != "1")
assert not CASES[4]["model_valid"]
assert CASES[5]["model_valid"] is (os.environ["LI06_EXPECT_AFTER"] != "1")
assert CASES[6]["model_valid"] is (os.environ["LI06_EXPECT_AFTER"] != "1")
assert CASES[7]["synthetic_providers_called"] == (["binance", "kraken"] if os.environ["LI06_EXPECT_AFTER"] == "1" else ["binance"])
assert not NETWORK_ATTEMPTS
assert not any(name in sys.modules for name in
               ("config", "kalshi_client", "execution_engine", "order_manager"))
out = {"deployed_sha": SHA, "tests": len(CASES), "cases": CASES,
       "network_attempts": NETWORK_ATTEMPTS, "broker_writes": 0,
       "real_provider_requests": 0, "real_orders_submitted": 0,
       "source_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(SOURCE.glob("*.py"))},
       "qualification": "SYNTHETIC_AFTER" if os.environ["LI06_EXPECT_AFTER"] == "1" else "BASELINE_UNSAFE_WITNESSES",
       "scope": "Exact source; synthetic observations, not runtime provider qualification"}
target = Path(os.environ["LI06_RESULT_PATH"])
target.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
print(json.dumps(out, indent=2, sort_keys=True))
