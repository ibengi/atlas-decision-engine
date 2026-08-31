# -*- coding: utf-8 -*-
"""Blocker 5 — MAX_CONTRACTS_PER_ORDER: an explicit, fail-safe hard cap.

Sizing by percentage (MAX_POSITION_PCT) is not a cap: it scales with
capital. The hard cap is clamped after ALL sizing (legacy and Kelly) and
re-verified immediately before the broker POST as defense-in-depth.
Invalid, zero or negative configuration collapses to 1 — a cap can never
become unlimited through misconfiguration.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import hard_contract_cap  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402
from position_sizer import PositionSizer  # noqa: E402


class CapConfigTest(unittest.TestCase):

    def setUp(self):
        self._old = bot.CFG.MAX_CONTRACTS_PER_ORDER
        self.addCleanup(lambda: setattr(bot.CFG, "MAX_CONTRACTS_PER_ORDER",
                                        self._old))

    def test_valid_values_pass_through(self):
        for v in (1, 7, "42"):
            bot.CFG.MAX_CONTRACTS_PER_ORDER = v
            self.assertEqual(hard_contract_cap(), int(v))

    def test_invalid_zero_negative_fail_safe_to_one(self):
        for v in (0, -3, "junk", None, "", "1.5"):
            bot.CFG.MAX_CONTRACTS_PER_ORDER = v
            self.assertEqual(hard_contract_cap(), 1,
                             f"config {v!r} must fail safe to cap=1")


class SizerClampTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: getattr(bot.CFG, k) for k in
                       ("MAX_CONTRACTS_PER_ORDER", "MAX_POS_PCT",
                        "KELLY_ENABLED", "RISK_BUDGET_PCT")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            setattr(bot.CFG, k, v)

    def test_legacy_sizing_cannot_exceed_cap(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 2
        bot.CFG.MAX_POS_PCT = 100.0          # percentage cap wide open
        bot.CFG.RISK_BUDGET_PCT = 100.0
        bot.CFG.KELLY_ENABLED = False
        n = PositionSizer.contracts(100000.0, 5, "2%", 9, 0.0, 0.0)
        self.assertEqual(n, 2)

    def test_kelly_sizing_cannot_exceed_cap(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 3
        bot.CFG.MAX_POS_PCT = 100.0
        bot.CFG.RISK_BUDGET_PCT = 100.0
        bot.CFG.KELLY_ENABLED = True
        n = PositionSizer.contracts(100000.0, 10, "2%", 9, 0.0, 0.0,
                                    probability=0.9, side="yes")
        self.assertEqual(n, 3)

    def test_cap_one_for_live_canary(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 1
        bot.CFG.MAX_POS_PCT = 100.0
        bot.CFG.RISK_BUDGET_PCT = 100.0
        bot.CFG.KELLY_ENABLED = False
        n = PositionSizer.contracts(100000.0, 5, "2%", 9, 0.0, 0.0)
        self.assertEqual(n, 1)

    def test_zero_sized_orders_stay_zero(self):
        """The clamp is an upper bound only — it never inflates a 0."""
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 5
        bot.CFG.KELLY_ENABLED = False
        n = PositionSizer.contracts(0.0, 5, "2%", 9, 0.0, 0.0)
        self.assertEqual(n, 0)


class OrderManagerDefenseTest(unittest.TestCase):
    """Defense-in-depth: even if sizing is bypassed or buggy, a count above
    the cap is BLOCKED (not clamped) immediately before the broker POST."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_cap_")
        self._old_dir = bot.CFG.DATA_DIR
        self._old_cap = bot.CFG.MAX_CONTRACTS_PER_ORDER
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._old_dir
        bot.CFG.MAX_CONTRACTS_PER_ORDER = self._old_cap
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_count_above_cap_blocked_before_post(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 1
        cli = MagicMock()
        cli.env = "demo"
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAP", "yes", 2, 40)
        self.assertEqual(res.status, "blocked:contract_cap_exceeded")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_count_at_cap_reaches_broker(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = 1
        cli = MagicMock()
        cli.env = "demo"
        cli.last_http_status = 201
        body = {"order_id": "ord-c", "status": "executed",
                "fill_count": 1, "remaining_count": 0, "ts_ms": 1}
        cli.create_order.return_value = dict(body)
        cli.get_order.return_value = dict(body)
        cli.get_fills.return_value = [{"fill_id": "f1", "count": 1,
                                       "price": 40}]
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAP1", "yes", 1, 40)
        self.assertEqual(res.state, "filled")
        self.assertEqual(cli.create_order.call_count, 1)


if __name__ == "__main__":
    unittest.main()
