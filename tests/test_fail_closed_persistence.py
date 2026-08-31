# -*- coding: utf-8 -*-
"""Blocker 2 — a critical persistence failure halts trading fail-closed.

JsonStore.save() fails soft by design (ERROR + False) and historically no
caller checked it: on a read-only or full disk the engine kept submitting
orders while silently persisting nothing, so every restart guarantee (the
2026-07-25 dedup guard above all) was believed to hold when it did not.

These tests pin the new contract: the FIRST failed write of a critical
state file trips PersistenceSentinel, and from that moment create_order()
is unreachable until the process restarts on a healthy disk.
"""
import errno
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
import persistence  # noqa: E402
from persistence import JsonStore, PersistenceSentinel, verify_state_root  # noqa: E402


def _client(order_id="ord-fc"):
    c = MagicMock()
    c.env = "demo"
    c.last_http_status = 201
    body = {"order_id": order_id, "status": "executed",
            "fill_count": 1, "remaining_count": 0, "ts_ms": 1}
    c.create_order.return_value = dict(body)
    c.get_order.return_value = dict(body)
    c.get_fills.return_value = [{"fill_id": "f1", "count": 1, "price": 40}]
    return c


class SentinelTestCase(unittest.TestCase):
    """Fresh isolated DATA_DIR + clean sentinel for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_failclosed_")
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSentinelTrips(SentinelTestCase):

    def _assert_fail_closed(self, om, cli):
        """After a tripped sentinel: no create_order, explicit block."""
        cli.create_order.reset_mock()
        res = om.place_and_track("KXFC-NEXT", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:persistence_failure")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_readonly_filesystem_blocks_next_order(self):
        cli = _client()
        om = bot.OrderManager(cli)
        self.assertTrue(PersistenceSentinel.healthy())
        with patch.object(persistence.os, "replace",
                          side_effect=OSError(errno.EROFS,
                                              "Read-only file system")):
            om.flush()   # critical write fails -> latch
        self.assertFalse(PersistenceSentinel.healthy())
        self._assert_fail_closed(om, cli)

    def test_enospc_blocks_next_order(self):
        cli = _client()
        om = bot.OrderManager(cli)
        with patch.object(persistence.os, "replace",
                          side_effect=OSError(errno.ENOSPC,
                                              "No space left on device")):
            om.flush()
        self.assertFalse(PersistenceSentinel.healthy())
        self._assert_fail_closed(om, cli)

    def test_serialization_failure_blocks_next_order(self):
        cli = _client()
        om = bot.OrderManager(cli)
        om.open_orders["bad"] = {"meta": object()}   # not JSON-serializable
        om.flush()
        self.assertFalse(PersistenceSentinel.healthy())
        om.open_orders.pop("bad")
        self._assert_fail_closed(om, cli)

    def test_accepted_order_stays_protected_after_trip(self):
        """The order accepted just before the failure keeps its dedup lock:
        re-submitting its ticker stays blocked (by the sentinel gate, and
        beneath it by the in-memory guard) with zero broker calls."""
        cli = _client()
        om = bot.OrderManager(cli)
        res = om.place_and_track("KXFC-HELD", "yes", 1, 40)
        self.assertEqual(res.state, "filled")
        with patch.object(persistence.os, "replace",
                          side_effect=OSError(errno.EROFS, "ro")):
            om.flush()
        cli.create_order.reset_mock()
        res2 = om.place_and_track("KXFC-HELD", "yes", 1, 40)
        self.assertIn(res2.status, ("blocked:persistence_failure",
                                    "blocked:duplicate_submission_guard"))
        self.assertEqual(cli.create_order.call_count, 0)
        self.assertIn("KXFC-HELD", om.session_submitted)

    def test_non_critical_save_failure_does_not_trip(self):
        """Observability files stay fail-soft: their loss is not dangerous
        and must not halt trading."""
        with patch.object(persistence.os, "replace",
                          side_effect=OSError(errno.EROFS, "ro")):
            ok = JsonStore.save(os.path.join(self.tmp, "cycle_report.json"),
                                {"x": 1})
        self.assertFalse(ok)
        self.assertTrue(PersistenceSentinel.healthy())


class TestEngineGate(SentinelTestCase):

    def test_post_balance_gates_fail_closed_on_persistence(self):
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.posmgr = MagicMock()
        eng.posmgr.reconcile_halt = None
        eng.posmgr.open_count.return_value = 0
        eng.risk = MagicMock()
        PersistenceSentinel.record_failure("/x/positions_state.json", "test")
        ok, guard = eng._post_balance_gates()
        self.assertFalse(ok)
        self.assertEqual(guard, "persistence_failure")
        eng.risk.can_trade.assert_not_called()


class TestStateContinuity(SentinelTestCase):
    """REQUIRE_PERSISTENT_STATE: a wiped/fresh disk must not silently
    resume trading as healthy."""

    def setUp(self):
        super().setUp()
        self._old_req = bot.CFG.REQUIRE_PERSISTENT_STATE
        self._old_fresh = bot.CFG.ALLOW_FRESH_STATE
        self.addCleanup(self._restore_flags)

    def _restore_flags(self):
        bot.CFG.REQUIRE_PERSISTENT_STATE = self._old_req
        bot.CFG.ALLOW_FRESH_STATE = self._old_fresh

    def test_missing_marker_trips_fail_closed(self):
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        bot.CFG.ALLOW_FRESH_STATE = False
        cli = _client()
        om = bot.OrderManager(cli)          # __init__ runs verify_state_root
        self.assertFalse(PersistenceSentinel.healthy())
        res = om.place_and_track("KXFC-WIPED", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:persistence_failure")
        self.assertEqual(cli.create_order.call_count, 0)

    def test_fresh_state_ack_initializes_marker(self):
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        bot.CFG.ALLOW_FRESH_STATE = True
        self.assertTrue(verify_state_root())
        self.assertTrue(PersistenceSentinel.healthy())
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "state_epoch.json")))

    def test_existing_marker_passes_without_ack(self):
        bot.CFG.REQUIRE_PERSISTENT_STATE = True
        bot.CFG.ALLOW_FRESH_STATE = True
        self.assertTrue(verify_state_root())      # initialize
        bot.CFG.ALLOW_FRESH_STATE = False
        self.assertTrue(verify_state_root())      # continuity: still healthy
        self.assertTrue(PersistenceSentinel.healthy())

    def test_demo_defaults_unchanged(self):
        """Both flags off (today's DEMO config): no marker requirement."""
        bot.CFG.REQUIRE_PERSISTENT_STATE = False
        self.assertTrue(verify_state_root())
        self.assertTrue(PersistenceSentinel.healthy())


if __name__ == "__main__":
    unittest.main()
