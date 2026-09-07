#!/usr/bin/env python3
"""Safety-preserving Railway bootstrap for PROD read-only observation.

This wrapper does not change order permissions, broker-write authorization,
model approval, risk thresholds, or CAPITAL behavior. It has two narrow jobs:

1. publish a fresh read-only dashboard snapshot at startup;
2. let PROD READ_ONLY continue the scanner/model/shadow pipeline when the
   *only* global blocker is ``equity_drawdown``.

The drawdown remains visible as a CAPITAL blocker. In CAPITAL mode it remains
fully blocking. Every other global guard remains fail-closed in every mode.
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


def _post_balance_gates_observation_aware(self):
    """Preserve all global gates, except observation may pass drawdown.

    ``equity_drawdown`` is an execution/capital guard. In PROD READ_ONLY there
    is no broker mutation path to authorize, so stopping before the scanner
    destroys model evidence without adding money-path safety. We therefore
    record the guard and continue observation only.

    CAPITAL mode, DEMO, and every other blocker keep the original result.
    """
    ok, guard = _original_post_balance_gates(self)
    is_prod_read_only = (
        getattr(self.client, "env", None) != "demo" and prod_is_read_only()
    )
    if not ok and guard == "equity_drawdown" and is_prod_read_only:
        self._read_only_capital_guard = guard
        bot.log.warning(
            "[READ_ONLY_OBSERVATION] capital_guard=equity_drawdown "
            "scanner_continues=true broker_writes=false"
        )
        return True, None
    return ok, guard


def _finish_cycle_with_capital_guard(self, n, res, execution_path="sequential"):
    """Carry an observed CAPITAL blocker into cycle/dashboard evidence."""
    guard = getattr(self, "_read_only_capital_guard", None)
    if guard:
        report = res.get("report") if isinstance(res, dict) else None
        if isinstance(report, dict):
            report["capital_blocking_guard"] = guard
            report["capital_eligible"] = False
    result = _original_finish_cycle(self, n, res, execution_path)
    if guard:
        try:
            state = JsonStore.load(_p("dashboard_state.json"), {}) or {}
            state["read_only"] = True
            state["capital_blocking_guard"] = guard
            state["capital_eligible"] = False
            JsonStore.save(_p("dashboard_state.json"), state)
        except Exception as exc:  # observability must never block the engine
            bot.log.warning("[READ_ONLY_OBSERVATION] dashboard non mis a jour: %s", exc)
        finally:
            self._read_only_capital_guard = None
    return result


def main():
    bot.banner = _banner_with_runtime_snapshot
    ExecutionEngine._post_balance_gates = _post_balance_gates_observation_aware
    ExecutionEngine._finish_cycle = _finish_cycle_with_capital_guard
    # Keep Railway startup explicit and fail-closed: the wrapped application
    # still receives the repository's normal READ_ONLY production flag.
    sys.argv = [sys.argv[0], "--loop", "--live-read-only"]
    bot.main()


if __name__ == "__main__":
    main()
