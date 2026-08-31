# -*- coding: utf-8 -*-
"""Blocker 5 — MAX_CONTRACTS_PER_ORDER: explicit hard cap, FAIL-CLOSED.

Sizing by percentage (MAX_POSITION_PCT) is not a cap: it scales with
capital. The hard cap is validated by contract_cap_config() and enforced
after ALL sizing (legacy and Kelly) plus immediately before the broker
POST as defense-in-depth.

Safety semantics (approved spec):
  - valid positive integer  -> that cap
  - missing, non-LIVE       -> documented safe default (CONTRACT_CAP_DEFAULT)
  - missing, LIVE-capable   -> submissions DISABLED (explicit value required)
  - invalid string          -> submissions DISABLED
  - zero / negative         -> submissions DISABLED
  - unreasonable (>max)     -> rejected by validation, submissions DISABLED
An invalid operator configuration is never silently reinterpreted (never
collapsed to 1, never to a default).
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
from config import CONTRACT_CAP_DEFAULT, CONTRACT_CAP_MAX, contract_cap_config  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402
from position_sizer import PositionSizer  # noqa: E402


class _CapConfigBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(bot.CFG, k) for k in
                       ("MAX_CONTRACTS_PER_ORDER", "REQUIRE_PERSISTENT_STATE")}
        self.addCleanup(lambda: [setattr(bot.CFG, k, v)
                                 for k, v in self._saved.items()])


class CapConfigTest(_CapConfigBase):

    def test_valid_values_pass_through(self):
        for v in ("1", "2", 7, " 42 "):
            bot.CFG.MAX_CONTRACTS_PER_ORDER = v
            cap, err = contract_cap_config(live_capable=False)
            self.assertEqual((cap, err), (int(str(v).strip()), None))

    def test_missing_non_live_uses_documented_default(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = None
        cap, err = contract_cap_config(live_capable=False)
        self.assertEqual((cap, err), (CONTRACT_CAP_DEFAULT, None))

    def test_missing_live_capable_disables_submissions(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = None
        cap, err = contract_cap_config(live_capable=True)
        self.assertIsNone(cap)
        self.assertIn("explicite", err)

    def test_live_capable_defaults_to_require_persistent_state_flag(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = None
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        cap, err = contract_cap_config()
        self.assertIsNone(cap)

    def test_invalid_zero_negative_unreasonable_disable_submissions(self):
        for v in ("junk", "", "1.5", 0, "0", -1, "-1",
                  CONTRACT_CAP_MAX + 1, "99999"):
            bot.CFG.MAX_CONTRACTS_PER_ORDER = v
            cap, err = contract_cap_config(live_capable=False)
            if str(v).strip() == "":
                # empty string = missing -> non-LIVE default, tested above
                continue
            self.assertIsNone(cap, f"{v!r} must disable submissions")
            self.assertTrue(err, f"{v!r} must carry a visible error")
            self.assertNotEqual(cap, 1,
                                "invalid config must NOT be reinterpreted as 1")


class SizerFailClosedTest(_CapConfigBase):

    def setUp(self):
        super().setUp()
        extra = {k: getattr(bot.CFG, k) for k in
                 ("MAX_POS_PCT", "KELLY_ENABLED", "RISK_BUDGET_PCT")}
        self._saved.update(extra)

    def test_legacy_sizing_cannot_exceed_cap(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "2"
        bot.CFG.MAX_POS_PCT = 100.0
        bot.CFG.RISK_BUDGET_PCT = 100.0
        bot.CFG.KELLY_ENABLED = False
        self.assertEqual(PositionSizer.contracts(100000.0, 5, "2%", 9,
                                                 0.0, 0.0), 2)

    def test_kelly_sizing_cannot_exceed_cap(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "3"
        bot.CFG.MAX_POS_PCT = 100.0
        bot.CFG.RISK_BUDGET_PCT = 100.0
        bot.CFG.KELLY_ENABLED = True
        self.assertEqual(PositionSizer.contracts(100000.0, 10, "2%", 9,
                                                 0.0, 0.0,
                                                 probability=0.9,
                                                 side="yes"), 3)

    def test_invalid_cap_sizes_zero_never_one(self):
        for v in ("junk", 0, -5):
            bot.CFG.MAX_CONTRACTS_PER_ORDER = v
            bot.CFG.KELLY_ENABLED = False
            n = PositionSizer.contracts(100000.0, 5, "2%", 9, 0.0, 0.0)
            self.assertEqual(n, 0, f"cap {v!r}: sizer must size 0, not clamp")

    def test_zero_sized_orders_stay_zero(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "5"
        bot.CFG.KELLY_ENABLED = False
        self.assertEqual(PositionSizer.contracts(0.0, 5, "2%", 9, 0.0, 0.0), 0)


class OrderManagerFailClosedTest(unittest.TestCase):
    """The last line of defense: with an invalid/absent-required cap, no
    create_order() call can ever happen; with a valid cap, an over-cap
    count is BLOCKED (not clamped) pre-POST."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_cap_")
        self._saved = {k: getattr(bot.CFG, k) for k in
                       ("DATA_DIR", "MAX_CONTRACTS_PER_ORDER",
                        "REQUIRE_PERSISTENT_STATE", "ALLOW_FRESH_STATE")}
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(bot.CFG, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _client():
        cli = MagicMock()
        cli.env = "demo"
        cli.last_http_status = 201
        body = {"order_id": "ord-c", "status": "executed",
                "fill_count": 1, "remaining_count": 0, "ts_ms": 1}
        cli.create_order.return_value = dict(body)
        cli.get_order.return_value = dict(body)
        cli.get_fills.return_value = [{"fill_id": "f1", "count": 1,
                                       "price": 40}]
        return cli

    def _assert_disabled(self, cap_value, live=False):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = cap_value
        bot.CFG.REQUIRE_PERSISTENT_STATE = live
        bot.CFG.ALLOW_FRESH_STATE = live      # continuity satisfied: the
        cli = self._client()                  # cap error must be the blocker
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAP", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:contract_cap_invalid")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(cli.create_order.call_count, 0,
                         f"cap={cap_value!r}: create_order must never run")

    def test_cap_zero_blocks_all_submissions(self):
        self._assert_disabled("0")

    def test_cap_negative_blocks_all_submissions(self):
        self._assert_disabled("-1")

    def test_cap_invalid_string_blocks_all_submissions(self):
        self._assert_disabled("junk")

    def test_missing_cap_in_live_capable_config_blocks(self):
        self._assert_disabled(None, live=True)

    def test_cap_one_allows_single_contract(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"
        cli = self._client()
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAP1", "yes", 1, 40)
        self.assertEqual(res.state, "filled")
        self.assertEqual(cli.create_order.call_count, 1)

    def test_cap_two_allows_two_contracts(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "2"
        cli = self._client()
        body = {"order_id": "ord-c", "status": "executed",
                "fill_count": 2, "remaining_count": 0, "ts_ms": 1}
        cli.create_order.return_value = dict(body)
        cli.get_order.return_value = dict(body)
        cli.get_fills.return_value = [{"fill_id": "f1", "count": 2,
                                       "price": 40}]
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAP2", "yes", 2, 40)
        self.assertEqual(cli.create_order.call_count, 1)
        self.assertEqual(res.filled, 2)

    def test_sizing_bug_exceeding_cap_rejected_pre_post(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"
        cli = self._client()
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXCAPBUG", "yes", 2, 40)
        self.assertEqual(res.status, "blocked:contract_cap_exceeded")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_engine_gate_reports_config_error(self):
        bot.CFG.MAX_CONTRACTS_PER_ORDER = "0"
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = MagicMock()
        eng.posmgr.reconcile_halt = None
        eng.risk = MagicMock()
        ok, guard = eng._post_balance_gates()
        self.assertFalse(ok)
        self.assertEqual(guard, "contract_cap_invalid")
        eng.risk.can_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
