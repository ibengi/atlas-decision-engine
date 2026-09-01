# -*- coding: utf-8 -*-
"""Closing an ambiguous intent: when is "not found" actually an absence?

An ambiguous POST leaves one question open: does the order exist? The
read-back answers it by `client_order_id`. FOUND and MULTIPLE are
decisive. NOT_FOUND is the treacherous one -- it is the answer that looks
like permission to try again, and it is precisely the answer a lagging
read replica gives about an order that DOES exist.

Policy pinned here (NOT_FOUND_CONFIRMED_2x_60s_FULL_PAGINATION):

  * an empty read counts as evidence only if the listing was COMPLETE
    (pagination exhausted); a truncated listing is an absence fabricated
    by pagination and is refused as evidence
  * N = AMBIGUOUS_NOT_FOUND_CONFIRMATIONS (2) such readings are required,
    spaced by at least AMBIGUOUS_NOT_FOUND_INTERVAL_S (60 s); a reading
    taken too soon carries no new information and is NOT counted
  * until the count is reached the intent stays OPEN, and an open intent
    blocks submission on that ticker INDEPENDENTLY of the duplicate
    guard's TTL -- the TTL measures elapsed time, never proof
  * an order appearing late (between two readings, or after a restart)
    wins: it is adopted, and no order is ever created a second time
  * anything inconsistent (multiple matches, unusable listing, truncated
    listing, unreadable broker) stays fail-closed

Mock broker throughout: no network, no DEMO order, no LIVE order.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from config import CFG  # noqa: E402
from kalshi_client import KalshiAPIError, KalshiClient  # noqa: E402
from persistence import JsonStore, PersistenceSentinel, _p  # noqa: E402

TICKER = "KXBTCD-CANARY-T1"
SIDE, COUNT, PRICE = "yes", 1, 40
CID = bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE)


def order_row(order_id="ord-late-1"):
    return {"order_id": order_id, "client_order_id": CID, "ticker": TICKER,
            "side": SIDE, "status": "resting", "fill_count": 0,
            "remaining_count": COUNT}


class _PolicyBase(unittest.TestCase):
    CONFIRMATIONS = 2
    INTERVAL = 60.0

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_notfound_")
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(self.tmp)
        self._saved = getattr(self, "_saved", None) or {
            k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "ALLOW_FRESH_STATE",
            "SUBMIT_DEDUP_TTL_S", "ORDER_TTL_SECONDS",
            "AMBIGUOUS_NOT_FOUND_CONFIRMATIONS",
            "AMBIGUOUS_NOT_FOUND_INTERVAL_S")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1
        CFG.AMBIGUOUS_NOT_FOUND_CONFIRMATIONS = self.CONFIRMATIONS
        CFG.AMBIGUOUS_NOT_FOUND_INTERVAL_S = self.INTERVAL
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        for d in getattr(self, "_tmps", [self.tmp]):
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _client(matches=()):
        c = MagicMock()
        c.env = "demo"
        c.last_http_status = 201
        c.create_order.side_effect = KalshiAPIError(0, "reseau: ReadTimeout")
        c.get_positions.return_value = []
        c.find_orders_by_client_order_id.return_value = list(matches)
        return c

    def _submit(self, om, price=PRICE):
        return om.place_and_track(TICKER, SIDE, COUNT, price)

    def _intent(self, om=None):
        if om is not None:
            return om.pending_intents.get(TICKER)
        return JsonStore.load(_p(bot.OrderManager.PENDING_FILE), {}).get(TICKER)

    def _age_the_last_reading(self, om, seconds):
        """Make the last NOT_FOUND reading look `seconds` old, on disk and
        in memory -- the honest way to travel forward in time."""
        om.pending_intents[TICKER]["last_not_found_at"] -= seconds
        om._flush_pending_intents()


class ConfirmationCountTest(_PolicyBase):
    """How many readings, and how far apart."""

    def test_one_reading_does_not_close_the_intent(self):
        om = bot.OrderManager(self._client())
        res = self._submit(om)

        self.assertTrue(res.status.endswith("not_found_pending"))
        intent = self._intent(om)
        self.assertEqual(intent["not_found_count"], 1)
        self.assertEqual(intent["resolution"], "NOT_FOUND_PENDING")
        self.assertIsNotNone(self._intent(), "intent not persisted")

    def test_a_second_reading_too_soon_is_not_counted(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)

        om.resolve_pending_intents()      # immediately: no new information

        self.assertEqual(self._intent(om)["not_found_count"], 1,
                         "a reading taken too soon was counted as evidence")
        self.assertEqual(self._intent(om)["resolution"], "NOT_FOUND_PENDING")

    def test_a_second_reading_after_the_interval_closes_the_intent(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self._age_the_last_reading(om, self.INTERVAL + 1)

        outcomes = om.resolve_pending_intents()

        self.assertEqual(outcomes[TICKER], "CLOSED_ABSENT")
        self.assertIsNone(self._intent(om), "closed intent still open")
        self.assertIsNone(self._intent(), "closed intent still on disk")

    def test_the_required_count_is_configurable_and_respected(self):
        CFG.AMBIGUOUS_NOT_FOUND_CONFIRMATIONS = 3
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)

        for expected in (2, 3):
            self._age_the_last_reading(om, self.INTERVAL + 1)
            outcome = om.resolve_pending_intents().get(TICKER)
            if expected < 3:
                self.assertEqual(outcome, "NOT_FOUND_PENDING")
                self.assertEqual(self._intent(om)["not_found_count"], expected)
        self.assertEqual(outcome, "CLOSED_ABSENT")

    def test_an_unavailable_reading_is_not_a_confirmation(self):
        """A lookup that failed proves nothing and must not advance the
        count toward closing the intent."""
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self.assertEqual(self._intent(om)["not_found_count"], 1)

        client.find_orders_by_client_order_id.side_effect = KalshiAPIError(
            0, "reseau: timeout")
        self._age_the_last_reading(om, self.INTERVAL + 1)
        om.resolve_pending_intents()

        self.assertEqual(self._intent(om)["not_found_count"], 1,
                         "a failed lookup counted as evidence of absence")
        self.assertEqual(self._intent(om)["resolution"], "UNAVAILABLE")
        self.assertIsNotNone(self._intent(om), "intent closed without proof")


class PaginationCompletenessTest(_PolicyBase):
    """A truncated listing is an absence fabricated by pagination."""

    def _client_with_pages(self, pages):
        c = KalshiClient.__new__(KalshiClient)
        c._raw_logged = set()
        return c, pages

    def test_truncated_listing_raises_instead_of_reporting_absence(self):
        c, pages = self._client_with_pages(None)
        # every page still advertises a next cursor: the listing never ends
        endless = {"orders": [], "cursor": "more"}
        with patch.object(KalshiClient, "_req", return_value=endless) as req:
            with self.assertRaises(KalshiAPIError) as ctx:
                c.find_orders_by_client_order_id(CID, ticker=TICKER)

        self.assertIn("tronque", str(ctx.exception))
        self.assertEqual(req.call_count, 10, "max_pages not honoured")

    def test_a_complete_listing_is_required_before_counting_a_reading(self):
        """End to end: a truncated listing yields UNAVAILABLE, never a
        NOT_FOUND confirmation."""
        client = self._client()
        client.find_orders_by_client_order_id.side_effect = KalshiAPIError(
            0, "listing d'ordres tronque: 10 pages lues")
        om = bot.OrderManager(client)

        res = self._submit(om)

        intent = self._intent(om)
        self.assertEqual(intent.get("not_found_count", 0), 0,
                         "a truncated listing counted as absence evidence")
        self.assertEqual(intent["resolution"], "UNAVAILABLE")
        self.assertFalse(res.status.endswith("closed_absent"))

    def test_a_match_on_the_last_page_is_found_not_absent(self):
        c = KalshiClient.__new__(KalshiClient)
        c._raw_logged = set()
        pages = [{"orders": [], "cursor": "p2"},
                 {"orders": [], "cursor": "p3"},
                 {"orders": [order_row()], "cursor": ""}]
        with patch.object(KalshiClient, "_req", side_effect=pages):
            found = c.find_orders_by_client_order_id(CID, ticker=TICKER)

        self.assertEqual(len(found), 1,
                         "stopped paginating before reaching the order")


class LateAppearingOrderTest(_PolicyBase):
    """The order that shows up after the first empty reading."""

    def test_an_order_appearing_before_closure_is_adopted(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self.assertEqual(self._intent(om)["not_found_count"], 1)

        # the order becomes visible before the confirming reading
        client.find_orders_by_client_order_id.return_value = [order_row()]
        self._age_the_last_reading(om, self.INTERVAL + 1)
        outcomes = om.resolve_pending_intents()

        self.assertEqual(outcomes[TICKER], "FOUND")
        self.assertIn("ord-late-1", om.open_orders, "late order not adopted")
        self.assertEqual(client.create_order.call_count, 1,
                         "a second order was created")

    def test_a_late_order_after_closure_is_caught_by_the_guard_not_a_repost(self):
        """Worst case: the order appears AFTER the intent was closed. The
        duplicate guard still holds the ticker, so no second order is
        created inside the TTL."""
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self._age_the_last_reading(om, self.INTERVAL + 1)
        self.assertEqual(om.resolve_pending_intents()[TICKER],
                         "CLOSED_ABSENT")

        client2 = self._client(matches=[order_row()])
        client2.create_order.side_effect = None
        client2.create_order.return_value = order_row(order_id="ord-new")
        om2 = bot.OrderManager(client2)
        res = self._submit(om2, price=PRICE + 7)

        self.assertEqual(res.status, "blocked:duplicate_submission_guard")
        self.assertEqual(client2.create_order.call_count, 0)


class TtlCannotSilentlyRepostTest(_PolicyBase):
    """The TTL measures elapsed time; it is not proof of anything."""

    def test_guard_ttl_expiry_does_not_unlock_an_open_intent(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self.assertEqual(self._intent(om)["resolution"], "NOT_FOUND_PENDING")

        # the whole 6h guard TTL elapses; the intent was never resolved
        CFG.SUBMIT_DEDUP_TTL_S = 0.0001
        time.sleep(0.01)
        client2 = self._client()
        client2.create_order.side_effect = None
        client2.create_order.return_value = order_row(order_id="ord-new")
        om2 = bot.OrderManager(client2)
        self.assertNotIn(TICKER, om2.session_submitted,
                         "guard should have expired: that is the premise")

        res = self._submit(om2, price=PRICE + 3)

        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(client2.create_order.call_count, 0,
                         "TTL expiry silently authorised a repost")

    def test_after_closure_the_ticker_returns_to_normal_guard_rules(self):
        """Closure is not a trap: once the policy has concluded, the
        ordinary TTL governs again."""
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self._age_the_last_reading(om, self.INTERVAL + 1)
        om.resolve_pending_intents()

        CFG.SUBMIT_DEDUP_TTL_S = 0.0001
        time.sleep(0.01)
        client2 = self._client()
        client2.create_order.side_effect = None
        client2.create_order.return_value = {
            "order_id": "ord-new", "status": "executed", "fill_count": 1,
            "remaining_count": 0, "ts_ms": 1}
        client2.get_order.return_value = client2.create_order.return_value
        client2.get_fills.return_value = [{"fill_id": "f", "count": 1,
                                           "price": PRICE, "yes_price": PRICE}]
        om2 = bot.OrderManager(client2)
        res = self._submit(om2, price=PRICE + 3)

        self.assertEqual(client2.create_order.call_count, 1,
                         "a closed intent must not block forever")
        self.assertEqual(res.state, "filled")


class RestartDuringTheWindowTest(_PolicyBase):
    """Restarts inside the confirmation window."""

    def test_restart_keeps_the_count_and_does_not_restart_it(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        self._age_the_last_reading(om, self.INTERVAL + 1)
        del om

        om2 = bot.OrderManager(self._client())
        self.assertEqual(om2.pending_intents[TICKER]["not_found_count"], 1,
                         "the count did not survive the restart")
        outcome = om2.resolve_pending_intents()[TICKER]
        self.assertEqual(outcome, "CLOSED_ABSENT",
                         "the surviving count did not carry the decision")

    def test_restart_inside_the_window_blocks_any_submission(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        del om

        client2 = self._client()
        client2.create_order.side_effect = None
        om2 = bot.OrderManager(client2)
        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())
        res = self._submit(om2, price=PRICE + 11)

        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(client2.create_order.call_count, 0)

    def test_restart_then_a_late_order_is_adopted_not_duplicated(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)
        del om

        client2 = self._client(matches=[order_row()])
        client2.create_order.side_effect = None
        om2 = bot.OrderManager(client2)
        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())

        self.assertIn("ord-late-1", om2.open_orders)
        self.assertEqual(client2.create_order.call_count, 0)


class InconsistencyStaysFailClosedTest(_PolicyBase):
    """Anything the policy cannot interpret refuses rather than concludes."""

    def test_multiple_matches_during_confirmation_halt_everything(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)

        client.find_orders_by_client_order_id.return_value = [
            order_row("dup-1"), order_row("dup-2")]
        self._age_the_last_reading(om, self.INTERVAL + 1)
        outcome = om.resolve_pending_intents()[TICKER]

        self.assertEqual(outcome, "MULTIPLE")
        self.assertEqual(om.resolution_halt["status"],
                         "MULTIPLE_ORDERS_SAME_CLIENT_ID")
        other = om.place_and_track("KXANY-OTHER", SIDE, COUNT, PRICE)
        self.assertEqual(other.status, "blocked:ambiguous_resolution_halt")

    def test_malformed_reply_during_confirmation_halts_everything(self):
        client = self._client()
        om = bot.OrderManager(client)
        self._submit(om)

        client.find_orders_by_client_order_id.side_effect = KalshiAPIError(
            0, "listing d'ordres incoherent: orders non-liste")
        self._age_the_last_reading(om, self.INTERVAL + 1)
        om.resolve_pending_intents()

        self.assertEqual(om.resolution_halt["status"],
                         "MALFORMED_ORDER_LISTING")

    def test_a_non_list_lookup_result_is_malformed_not_absence(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = "not-a-list"
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertTrue(res.status.endswith("malformed"))
        self.assertEqual(om.resolution_halt["status"],
                         "MALFORMED_ORDER_LISTING")


if __name__ == "__main__":
    unittest.main()
