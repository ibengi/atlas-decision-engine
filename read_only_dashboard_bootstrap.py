#!/usr/bin/env python3
"""Observability-only Railway bootstrap for PROD read-only mode.

This wrapper does not change trading gates, risk state, order permissions, or
broker-write authorization. It lets ``kalshi_alpha_bot.main`` perform its
normal production intent/credential checks, then refreshes dashboard_state.json
immediately after the normal startup banner has successfully read the broker.

The purpose is narrow: a process blocked by an early global guard can otherwise
leave a stale dashboard snapshot from an older DEMO process indefinitely.
"""

import sys

import kalshi_alpha_bot as bot
from config import _p
from persistence import JsonStore
from trade_logger import now_iso


_original_banner = bot.banner


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
            "candidates": [],
        })
        bot.log.info(
            "[DASHBOARD_STARTUP_SNAPSHOT] env=%s balance=%s read_only=true",
            getattr(client, "env", None), bal)
    except Exception as exc:  # observability must never block the engine
        bot.log.warning("[DASHBOARD_STARTUP_SNAPSHOT] non ecrit: %s", exc)


def main():
    bot.banner = _banner_with_runtime_snapshot
    # Keep Railway startup explicit and fail-closed: the wrapped application
    # still receives the repository's normal READ_ONLY production flag.
    sys.argv = [sys.argv[0], "--loop", "--live-read-only"]
    bot.main()


if __name__ == "__main__":
    main()
