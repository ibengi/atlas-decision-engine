# -*- coding: utf-8 -*-
"""The money path may not book a settlement from the first answer after
close. These tests pin the protocol in PositionManager.check_settlements:
finalized status confirms at once; otherwise the same result must be seen
twice >= 30 min apart, the first >= 30 min after close; a pending position
stays open, survives a restart, and is booked exactly once.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
import position_manager as pmod  # noqa: E402

MIN_LAG = pmod.SETTLE_MIN_LAG_S
CONFIRM = pmod.SETTLE_CONFIRM_MIN_S
T0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)          # market close


def position(trade_id="t-1", side="yes", count=10, avg_price=50, fees=0.07):
    return {"trade_id": trade_id, "ticker": "KXBTCD-26SEP0208-T77000",
            "side": side, "count": count, "avg_price": avg_price,
            "fees": fees, "opened_at": (T0 - timedelta(hours=5)).isoformat(),
            "state": "open", "order_ids": [], "fill_ids": [],
            "count_initial": count}


def market(result, status="closed", close=T0):
    return {"ticker": "KXBTCD-26SEP0208-T77000", "result": result,
            "status": status, "close_time": close.isoformat()}


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.tlog = MagicMock()
        self.tlog.settle_trade.return_value = {"trade_id": "t-1"}
        with patch.object(pmod.JsonStore, "save"):
            self.pm = bot.PositionManager(self.client, self.tlog)
        self.pm.positions = {"t-1": position()}
        self._save = patch.object(pmod.JsonStore, "save")
        self.saves = self._save.start()
        self.addCleanup(self._save.stop)

    def poll(self, result, at, status="closed"):
        self.client.get_market.return_value = market(result, status)
        return self.pm.check_settlements(now=at)


class FinalizedStatusTest(_Base):
    def test_a_finalized_status_books_at_once(self):
        for st in ("settled", "finalized"):
            self.setUp()
            got = self.poll("yes", T0 + timedelta(minutes=1), status=st)
            self.assertEqual(len(got), 1)
            self.assertNotIn("t-1", self.pm.positions)
            self.tlog.settle_trade.assert_called_once()


class FirstPollTest(_Base):
    def test_a_result_minutes_after_close_is_not_booked(self):
        got = self.poll("no", T0 + timedelta(minutes=5))
        self.assertEqual(got, [])
        self.assertIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_not_called()
        self.assertNotIn("settle_obs", self.pm.positions["t-1"])  # too early to count

    def test_a_result_after_min_lag_is_recorded_not_booked(self):
        got = self.poll("no", T0 + timedelta(seconds=MIN_LAG + 1))
        self.assertEqual(got, [])
        obs = self.pm.positions["t-1"]["settle_obs"]
        self.assertEqual(obs["result"], "no")
        self.assertGreaterEqual(obs["lag_s"], MIN_LAG)
        self.tlog.settle_trade.assert_not_called()

    def test_the_observation_is_persisted_immediately(self):
        self.poll("no", T0 + timedelta(seconds=MIN_LAG + 1))
        self.assertTrue(self.saves.called, "pending observation not flushed")
        json.dumps(self.pm.positions)                     # must serialise


class ConfirmationTest(_Base):
    def test_two_agreeing_reads_far_enough_apart_book_once(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        got = self.poll("no", first + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(len(got), 1)
        self.assertNotIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_called_once()
        args = self.tlog.settle_trade.call_args[0]
        self.assertEqual(args[1], "no")
        self.assertFalse(args[2])                          # side yes lost

    def test_a_second_read_inside_the_window_keeps_waiting(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        got = self.poll("no", first + timedelta(minutes=10))
        self.assertEqual(got, [])
        self.assertIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_not_called()

    def test_a_too_early_first_read_never_counts_as_the_first_observation(self):
        # The defect exactly: polled 5 min after close, then again 31 min
        # later with the same answer. The early read must not seed the pair.
        self.poll("no", T0 + timedelta(minutes=5))
        got = self.poll("no", T0 + timedelta(minutes=5, seconds=CONFIRM + 1))
        self.assertEqual(got, [])
        self.assertIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_not_called()

    def test_disagreeing_reads_reject_and_restart_the_clock(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        second = first + timedelta(seconds=CONFIRM + 1)
        with self.assertLogs("POSITION", level="INFO") as cm:
            got = self.poll("yes", second)
        self.assertEqual(got, [])
        self.assertTrue(any("SETTLEMENT_REJECTED_INCONSISTENT" in l for l in cm.output))
        self.assertEqual(self.pm.positions["t-1"]["settle_obs"]["result"], "yes")
        # a matching third read after a full window confirms the NEW value
        got = self.poll("yes", second + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "yes")

    def test_void_is_gated_exactly_like_yes_and_no(self):
        got = self.poll("void", T0 + timedelta(minutes=5))
        self.assertEqual(got, [])
        self.tlog.settle_trade.assert_not_called()
        got = self.poll("void", T0 + timedelta(minutes=5), status="settled")
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "void")

    def test_an_unknown_close_time_can_only_confirm_by_status(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        m = market("no"); m.pop("close_time")
        self.client.get_market.return_value = m
        self.pm.check_settlements(now=first)
        self.pm.check_settlements(now=first + timedelta(seconds=CONFIRM + 1))
        self.assertIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_not_called()


class RestartAndIdempotenceTest(_Base):
    def test_a_pending_observation_survives_a_restart(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        snapshot = json.loads(json.dumps(self.pm.positions))     # what disk holds
        with patch.object(pmod.JsonStore, "save"):
            pm2 = bot.PositionManager(self.client, self.tlog)
        pm2.positions = snapshot
        self.client.get_market.return_value = market("no")
        got = pm2.check_settlements(now=first + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(len(got), 1)
        self.tlog.settle_trade.assert_called_once()

    def test_a_confirmed_settlement_is_never_booked_twice(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        at = first + timedelta(seconds=CONFIRM + 1)
        self.poll("no", at)
        self.poll("no", at + timedelta(minutes=1))
        self.poll("no", at + timedelta(hours=1), status="settled")
        self.tlog.settle_trade.assert_called_once()

    def test_pnl_is_booked_from_the_position_not_the_observation(self):
        self.pm.positions = {"t-1": position(side="no", count=4, avg_price=20,
                                             fees=0.03)}
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("no", first)
        self.poll("no", first + timedelta(seconds=CONFIRM + 1))
        args = self.tlog.settle_trade.call_args[0]
        # side no, result no -> won; gross = 4*1 - 4*0.20 = 3.2; net = 3.17
        self.assertTrue(args[2])
        self.assertAlmostEqual(args[3], 3.2)
        self.assertAlmostEqual(args[4], 3.17)


class AuditTrailTest(_Base):
    def test_every_protocol_event_is_logged_by_name(self):
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        with self.assertLogs("POSITION", level="INFO") as cm:
            self.poll("no", T0 + timedelta(minutes=5))               # too early
            self.poll("no", first)                                    # observed
            self.poll("yes", first + timedelta(seconds=CONFIRM + 1))  # inconsistent
            self.poll("yes", first + timedelta(seconds=2 * CONFIRM + 2))  # confirmed
        out = "\n".join(cm.output)
        for ev in ("SETTLEMENT_OBSERVED", "SETTLEMENT_PENDING_CONFIRMATION",
                   "SETTLEMENT_REJECTED_INCONSISTENT", "SETTLEMENT_CONFIRMED"):
            self.assertIn(ev, out, ev)


class UntouchedPathsTest(_Base):
    def test_an_open_market_with_no_result_is_left_alone(self):
        got = self.poll("", T0 + timedelta(hours=2), status="open")
        self.assertEqual(got, [])
        self.assertIn("t-1", self.pm.positions)
        self.assertNotIn("settle_obs", self.pm.positions["t-1"])

    def test_unreadable_result_under_finalized_status_still_voids(self):
        got = self.poll("", T0 + timedelta(hours=2), status="settled")
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "void_unreadable")


if __name__ == "__main__":
    unittest.main()
