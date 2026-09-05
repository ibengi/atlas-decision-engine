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
import trade_logger  # noqa: E402
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
        # No prior settlement unless a test says so: a MagicMock attribute is
        # truthy, and an unconfigured settled_row would make EVERY settlement
        # look like a duplicate.
        self.tlog.settled_row.return_value = None
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


class DuplicateSettlementTest(_Base):
    """B1: a settlement happens once. A crash between writing the trade
    journal and removing the position must not re-book it, and must not
    move its date into a later day's risk accounting."""

    def _settle_once(self):
        got = self.poll("no", T0 + timedelta(minutes=1), status="settled")
        self.assertEqual(len(got), 1)
        return got[0]

    def setUp(self):
        super().setUp()
        # a real TradeLogger so the journal semantics are exercised
        self.tlog = bot.TradeLogger.__new__(bot.TradeLogger)
        self.tlog.path = os.devnull
        self.tlog.flush = lambda: None
        self.tlog.trades = [{
            "trade_id": "t-1", "ticker": "KXBTCD-26SEP0208-T77000",
            "timestamp": (T0 - timedelta(hours=5)).isoformat(),
            "avg_fill_price": 50, "filled_count": 10, "fees": 0.07,
            "state": "open", "result": None, "won": None,
            "gross_pnl": None, "net_pnl": None}]
        self.pm.tlog = self.tlog

    def test_a_second_settlement_is_a_no_op(self):
        self._settle_once()
        row = dict(self.tlog.trades[0])
        # the crash: journal persisted, positions.json did not
        self.pm.positions = {"t-1": position()}
        again = self.poll("no", T0 + timedelta(days=1), status="settled")
        self.assertEqual(again, [], "a duplicate settlement was reported as realized")
        self.assertEqual(self.tlog.trades[0], row, "the settled row was rewritten")

    def test_settled_at_is_never_rewritten(self):
        self._settle_once()
        first = self.tlog.trades[0]["settled_at"]
        with patch.object(trade_logger, "now_iso",
                          lambda: "2030-01-01T00:00:00+00:00"):
            self.tlog.settle_trade("t-1", "no", False, -5.0, -5.07)
        self.assertEqual(self.tlog.trades[0]["settled_at"], first)

    def test_realized_pnl_is_not_duplicated(self):
        self._settle_once()
        self.pm.positions = {"t-1": position()}
        self.poll("no", T0 + timedelta(days=1), status="settled")
        settled = [t for t in self.tlog.trades if t.get("state") == "settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(sum(t["net_pnl"] for t in settled),
                         self.tlog.trades[0]["net_pnl"])

    def test_daily_loss_accounting_stays_on_the_original_date(self):
        # The defect's real cost: a loss booked yesterday re-counting against
        # today's MAX_DAILY_LOSS because settled_at moved.
        with patch.object(trade_logger, "now_iso",
                          lambda: "2026-09-01T12:00:00+00:00"):
            self.tlog.settle_trade("t-1", "no", False, -5.0, -5.07)
        with patch.object(trade_logger, "now_iso",
                          lambda: "2026-09-02T09:00:00+00:00"):
            self.tlog.settle_trade("t-1", "no", False, -5.0, -5.07)
        self.assertTrue(self.tlog.trades[0]["settled_at"].startswith("2026-09-01"))

    def test_the_slot_is_still_released_after_a_duplicate(self):
        # A zombie position must not hold a MAX_OPEN_POSITIONS slot forever.
        self._settle_once()
        self.pm.positions = {"t-1": position()}
        self.poll("no", T0 + timedelta(days=1), status="settled")
        self.assertNotIn("t-1", self.pm.positions)

    def test_the_duplicate_is_logged_by_name(self):
        self._settle_once()
        self.pm.positions = {"t-1": position()}
        with self.assertLogs("POSITION", level="INFO") as cm:
            self.poll("no", T0 + timedelta(days=1), status="settled")
        self.assertTrue(any("DUPLICATE_SETTLEMENT_IGNORED" in l for l in cm.output))

    def test_an_unsettled_trade_still_settles_normally(self):
        row = self._settle_once()
        self.assertEqual(row["result"], "no")
        self.assertEqual(self.tlog.trades[0]["state"], "settled")


class MaxAgeTest(_Base):
    """B2: a stale position whose market reports a usable result must be
    booked from that result, not swept as expired_stale."""

    def _stale(self, days=40):
        p = position()
        p["opened_at"] = (T0 - timedelta(days=days)).isoformat()
        self.pm.positions = {"t-1": p}

    def test_a_stale_winner_is_not_swept_as_expired_stale(self):
        self._stale()
        got = self.poll("yes", T0 + timedelta(minutes=1), status="settled")
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "yes")
        self.assertTrue(self.tlog.settle_trade.call_args[0][2])   # won
        self.assertAlmostEqual(self.tlog.settle_trade.call_args[0][3], 5.0)

    def test_a_stale_loser_books_the_real_loss(self):
        self._stale()
        got = self.poll("no", T0 + timedelta(minutes=1), status="settled")
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "no")
        self.assertFalse(self.tlog.settle_trade.call_args[0][2])

    def test_a_stale_void_books_void_not_expired_stale(self):
        self._stale()
        self.poll("void", T0 + timedelta(minutes=1), status="settled")
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "void")

    def test_a_stale_readable_result_without_a_final_status_still_waits(self):
        self._stale()
        got = self.poll("yes", T0 + timedelta(minutes=1))       # status=closed
        self.assertEqual(got, [], "age bypassed the confirmation protocol")
        self.assertIn("t-1", self.pm.positions)
        self.tlog.settle_trade.assert_not_called()

    def test_a_stale_readable_result_confirms_through_the_normal_protocol(self):
        self._stale()
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("yes", first)
        got = self.poll("yes", first + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "yes")

    def test_stale_with_conflicting_observations_never_books(self):
        self._stale()
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("yes", first)
        got = self.poll("no", first + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(got, [])
        self.tlog.settle_trade.assert_not_called()

    def test_a_stale_unreadable_result_is_still_swept(self):
        self._stale()
        got = self.poll("", T0 + timedelta(minutes=1), status="closed")
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "expired_stale")

    def test_a_stale_position_on_a_still_open_market_fails_closed(self):
        self._stale()
        got = self.poll("", T0 + timedelta(minutes=1), status="open")
        self.assertEqual(got, [])
        self.assertIn("t-1", self.pm.positions)

    def test_an_api_timeout_on_a_stale_position_still_sweeps(self):
        self._stale()
        self.client.get_market.return_value = None
        got = self.pm.check_settlements(now=T0 + timedelta(minutes=1))
        self.assertEqual(len(got), 1)
        self.assertEqual(self.tlog.settle_trade.call_args[0][1], "expired_stale")

    def test_a_fresh_position_is_never_swept(self):
        self._stale(days=1)
        self.client.get_market.return_value = None
        self.assertEqual(self.pm.check_settlements(now=T0), [])
        self.assertIn("t-1", self.pm.positions)

    def test_a_stale_pending_observation_survives_a_restart(self):
        self._stale()
        first = T0 + timedelta(seconds=MIN_LAG + 1)
        self.poll("yes", first)
        snapshot = json.loads(json.dumps(self.pm.positions))
        with patch.object(pmod.JsonStore, "save"):
            pm2 = bot.PositionManager(self.client, self.tlog)
        pm2.positions = snapshot
        self.client.get_market.return_value = market("yes")
        got = pm2.check_settlements(now=first + timedelta(seconds=CONFIRM + 1))
        self.assertEqual(len(got), 1)
