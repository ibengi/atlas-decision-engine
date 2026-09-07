#!/usr/bin/env python3
"""Safety-preserving Railway bootstrap for PROD read-only observation.

This wrapper does not change order permissions, broker-write authorization,
model approval, risk thresholds, or CAPITAL behavior. It has two narrow jobs:

1. publish a fresh read-only dashboard snapshot at startup;
2. let PROD READ_ONLY continue the scanner/model/shadow pipeline when the
   *only* global blocker is ``equity_drawdown``.

The drawdown remains visible as a CAPITAL blocker: it is written into the
durable per-cycle evidence row (``would_block_capital``), into the cycle
report (``capital_blocking_guard`` / ``capital_eligible``) and into the
dashboard snapshot. In CAPITAL mode and in DEMO it remains fully blocking.
Every other global guard remains fail-closed in every mode.

Sizing is untouched: a blown drawdown still halves the position size through
``DD_THROTTLE_PCT`` and ``drawdown_size_factor``, exactly as before.

The wrapper patches three ``ExecutionEngine`` methods at process start
(``install``). The per-cycle flag it needs lives on the engine instance under
``_CAPITAL_GUARD_ATTR`` and is reset at the START of every gate evaluation and
in a ``finally`` at the END of every finalization, so it can never survive an
exception into the next cycle.
"""

import sys

import kalshi_alpha_bot as bot
from config import _p, prod_is_read_only
from execution_engine import ExecutionEngine
from persistence import JsonStore
from trade_logger import now_iso


_original_banner = bot.banner
_original_post_balance_gates = ExecutionEngine._post_balance_gates
_original_finish_cycle = ExecutionEngine._finish_cycle
_original_record_cycle_evidence = ExecutionEngine._record_cycle_evidence

#: The one guard READ_ONLY observation may continue through. Deliberately a
#: single name, not a set: this wrapper relaxes nothing else.
OBSERVATION_ONLY_GUARD = "equity_drawdown"
_CAPITAL_GUARD_ATTR = "_read_only_capital_guard"


def _capital_guard(engine):
    return getattr(engine, _CAPITAL_GUARD_ATTR, None)


def _clear_capital_guard(engine):
    setattr(engine, _CAPITAL_GUARD_ATTR, None)


def _banner_with_runtime_snapshot(client, capital):
    """Run the normal banner, then publish current runtime truth for the UI.

    This executes only after ``kalshi_alpha_bot.main`` has completed its normal
    production-mode and credential validation and constructed the real client.
    A second balance GET is intentionally read-only. Any dashboard write failure
    is non-fatal and cannot influence the engine.
    """
    _original_banner(client, capital)
    try:
        bal = client.get_balance()
        effective = min(float(capital), bal) if bal is not None else None
        JsonStore.save(_p("dashboard_state.json"), {
            "ts": now_iso(),
            "version": bot.ENGINE_VERSION,
            "env": getattr(client, "env", None),
            "cycle": 0,
            "balance": bal,
            "capital": effective,
            "configured_capital": float(capital),
            "read_only": True,
            "startup_snapshot": True,
            "capital_blocking_guard": None,
            "candidates": [],
        })
        bot.log.info(
            "[DASHBOARD_STARTUP_SNAPSHOT] env=%s balance=%s read_only=true",
            getattr(client, "env", None), bal)
    except Exception as exc:  # observability must never block the engine
        bot.log.warning("[DASHBOARD_STARTUP_SNAPSHOT] non ecrit: %s", exc)


def _is_prod_read_only(engine) -> bool:
    """"Not demo", never "is prod", the write boundary's own formulation."""
    return (getattr(getattr(engine, "client", None), "env", None) != "demo"
            and prod_is_read_only())


def _post_balance_gates_observation_aware(self):
    """Preserve all global gates, except observation may pass drawdown.

    ``equity_drawdown`` is a capital guard. In PROD READ_ONLY there is no
    broker mutation path to authorize, so stopping before the scanner destroys
    model evidence without adding money-path safety. The guard is recorded on
    the engine for this cycle and observation continues.

    CAPITAL mode, DEMO, and every other blocker keep the original result.
    The per-cycle flag is reset FIRST, so nothing a previous cycle left behind
    (an exception between the gate and finalization, for instance) can be
    mistaken for this cycle's verdict.
    """
    _clear_capital_guard(self)
    ok, guard = _original_post_balance_gates(self)
    if ok:
        return ok, guard
    if guard == OBSERVATION_ONLY_GUARD and _is_prod_read_only(self):
        setattr(self, _CAPITAL_GUARD_ATTR, guard)
        bot.log.warning(
            "[READ_ONLY_OBSERVATION] capital_guard=%s scanner_continues=true "
            "broker_writes=false", guard)
        return True, None
    return ok, guard


def _record_cycle_evidence_with_capital_guard(self, n, execution_path,
                                              blocking_global_guard=None,
                                              detail="", pipeline=None,
                                              **kwargs):
    """Put the observed CAPITAL blocker into the DURABLE evidence row.

    The row is the machine-readable record of the cycle; a log line is not.
    An explicit ``would_block_capital`` from a caller wins over the flag, so
    an engine that already carries the fact itself stays authoritative.
    """
    guard = kwargs.pop("would_block_capital", None) or _capital_guard(self)
    return _original_record_cycle_evidence(
        self, n, execution_path, blocking_global_guard, detail=detail,
        pipeline=pipeline, would_block_capital=guard, **kwargs)


def _finish_cycle_with_capital_guard(self, n, res, *args, **kwargs):
    """Carry an observed CAPITAL blocker into cycle/dashboard evidence.

    Positional and keyword arguments after ``res`` are passed through
    untouched, so this wrapper is indifferent to the engine's exact
    finalization signature. The flag is cleared in ``finally`` whether the
    original finalization returns or raises.
    """
    guard = _capital_guard(self)
    try:
        if guard:
            report = res.get("report") if isinstance(res, dict) else None
            if isinstance(report, dict):
                report["capital_blocking_guard"] = guard
                report["capital_eligible"] = False
        result = _original_finish_cycle(self, n, res, *args, **kwargs)
        if guard:
            try:
                state = JsonStore.load(_p("dashboard_state.json"), {}) or {}
                state["read_only"] = True
                state["capital_blocking_guard"] = guard
                state["capital_eligible"] = False
                JsonStore.save(_p("dashboard_state.json"), state)
            except Exception as exc:  # observability must never block the engine
                bot.log.warning(
                    "[READ_ONLY_OBSERVATION] dashboard non mis a jour: %s", exc)
        return result
    finally:
        _clear_capital_guard(self)


def install():
    """Patch the engine. Idempotent; tests pair it with ``uninstall``."""
    bot.banner = _banner_with_runtime_snapshot
    ExecutionEngine._post_balance_gates = _post_balance_gates_observation_aware
    ExecutionEngine._record_cycle_evidence = _record_cycle_evidence_with_capital_guard
    ExecutionEngine._finish_cycle = _finish_cycle_with_capital_guard


def uninstall():
    bot.banner = _original_banner
    ExecutionEngine._post_balance_gates = _original_post_balance_gates
    ExecutionEngine._record_cycle_evidence = _original_record_cycle_evidence
    ExecutionEngine._finish_cycle = _original_finish_cycle


def main():
    install()
    # Keep Railway startup explicit and fail-closed: the wrapped application
    # still receives the repository's normal READ_ONLY production flag.
    sys.argv = [sys.argv[0], "--loop", "--live-read-only"]
    bot.main()


if __name__ == "__main__":
    main()
