# -*- coding: utf-8 -*-
"""Read-back by client_order_id: resolving an ambiguous POST by ASKING.

After a POST whose answer never came back, the engine used to have no way
to find the order: the read-back path needs an `order_id`, and an
ambiguous POST is precisely the case where no `order_id` was received.
The only thing that survives is the DETERMINISTIC `client_order_id` the
engine chose before sending.

Kalshi's Trade API v2 exposes `GET /portfolio/orders` (listing, filtered
server-side by ticker/status, paginated by cursor) and returns each
order's `client_order_id`. There is NO server-side filter on that field,
so the lookup is: list server-side by ticker, match locally on the id.
That distinction is deliberate and load-bearing -- it is why
`list_orders` must raise rather than return `[]` when it cannot read: a
failed lookup must never be mistaken for "the order does not exist".

Outcomes and what each one is allowed to do:

    FOUND (exactly 1)  adopt order_id + status locally; NO new order
    NOT_FOUND          stay fail-closed; NO automatic repost
    MULTIPLE           halt every submission: a duplicate may exist
    MALFORMED          halt every submission: the answer is unusable
    UNAVAILABLE        stay fail-closed; NO repost; retry later

Mock broker throughout: no network, no DEMO order, no LIVE order.
"""
import os
import shutil
import sys
import tempfile
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


def order_row(order_id="ord-found-1", client_order_id=CID, status="resting",
              **extra):
    row = {"order_id": order_id, "client_order_id": client_order_id,
           "ticker": TICKER, "side": SIDE, "status": status,
           "fill_count": 0, "remaining_count": COUNT}
    row.update(extra)
    return row


class ClientListingTest(unittest.TestCase):
    """The transport layer: official listing endpoint, local match."""

    def _client(self):
        c = KalshiClient.__new__(KalshiClient)      # no network, no keys
        c._raw_logged = set()
        return c

    def test_lookup_uses_the_official_listing_endpoint_and_matches_locally(self):
        c = self._client()
        with patch.object(KalshiClient, "_req") as req:
            req.return_value = {"orders": [order_row(), order_row(
                order_id="ord-other", client_order_id="alpha_other")]}
            found = c.find_orders_by_client_order_id(CID, ticker=TICKER)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["order_id"], "ord-found-1")
        method, path = req.call_args[0][0], req.call_args[0][1]
        self.assertEqual((method, path), ("GET", "/portfolio/orders"))
        self.assertEqual(req.call_args.kwargs["params"]["ticker"], TICKER)

    def test_cursor_pagination_is_followed(self):
        c = self._client()
        pages = [
            {"orders": [order_row(order_id="p1", client_order_id="alpha_x")],
             "cursor": "next-1"},
            {"orders": [order_row(order_id="p2")], "cursor": ""},
        ]
        with patch.object(KalshiClient, "_req", side_effect=pages) as req:
            found = c.find_orders_by_client_order_id(CID, ticker=TICKER)

        self.assertEqual(req.call_count, 2)
        self.assertEqual(req.call_args_list[1].kwargs["params"]["cursor"],
                         "next-1")
        self.assertEqual([o["order_id"] for o in found], ["p2"])

    def test_no_match_returns_empty_but_a_read_failure_raises(self):
        c = self._client()
        with patch.object(KalshiClient, "_req",
                          return_value={"orders": []}):
            self.assertEqual(c.find_orders_by_client_order_id(CID), [])

        with patch.object(KalshiClient, "_req",
                          side_effect=KalshiAPIError(0, "reseau: timeout")):
            with self.assertRaises(KalshiAPIError):
                c.find_orders_by_client_order_id(CID)

    def test_malformed_listing_shapes_raise(self):
        c = self._client()
        for label, payload in [
            ("reponse non-objet", ["not", "a", "dict"]),
            ("orders non-liste", {"orders": {"nope": 1}}),
            ("entree non-objet", {"orders": ["just-a-string"]}),
        ]:
            with self.subTest(case=label):
                with patch.object(KalshiClient, "_req", return_value=payload):
                    with self.assertRaises(KalshiAPIError):
                        c.find_orders_by_client_order_id(CID)

    def test_empty_client_order_id_is_refused(self):
        c = self._client()
        with patch.object(KalshiClient, "_req") as req:
            with self.assertRaises(KalshiAPIError):
                c.find_orders_by_client_order_id("")
        self.assertEqual(req.call_count, 0)


class _ResolverBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_readback_")
        self._tmps = getattr(self, "_tmps", [])
        self._tmps.append(self.tmp)
        # Voir note d'hygiene dans test_single_shot_canary: capture unique.
        self._saved = getattr(self, "_saved", None) or {
            k: getattr(CFG, k) for k in (
            "DATA_DIR", "REQUIRE_PERSISTENT_STATE", "ALLOW_ORDER_SUBMISSION",
            "MAX_CONTRACTS_PER_ORDER", "ALLOW_FRESH_STATE",
            "SUBMIT_DEDUP_TTL_S", "ORDER_TTL_SECONDS")}
        CFG.DATA_DIR = self.tmp
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in self._saved.items():
            setattr(CFG, k, v)
        for d in getattr(self, "_tmps", [self.tmp]):
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _client(lookup=None):
        c = MagicMock()
        c.env = "demo"
        c.last_http_status = 201
        c.create_order.side_effect = KalshiAPIError(0, "reseau: ReadTimeout")
        c.get_positions.return_value = []
        if lookup is not None:
            c.find_orders_by_client_order_id.side_effect = lookup
        else:
            c.find_orders_by_client_order_id.return_value = []
        return c

    def _submit(self, om, price=PRICE):
        return om.place_and_track(TICKER, SIDE, COUNT, price)

    def _intents_on_disk(self):
        return JsonStore.load(_p(bot.OrderManager.PENDING_FILE), {})


class AmbiguousPostResolutionTest(_ResolverBase):
    """The five outcomes, each on the real submission path."""

    def test_found_adopts_the_existing_order_without_creating_one(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = [order_row()]
        om = bot.OrderManager(client)

        res = self._submit(om)

        client.find_orders_by_client_order_id.assert_called_once()
        self.assertEqual(client.find_orders_by_client_order_id.call_args[0][0],
                         CID, "lookup did not use the deterministic id")
        self.assertEqual(client.create_order.call_count, 1,
                         "exactly one POST attempt, never a second")
        self.assertIn("ord-found-1", om.open_orders, "order not adopted")
        self.assertEqual(om.open_orders["ord-found-1"]
                         ["adopted_from_client_order_id"], CID)
        self.assertEqual(res.order_id, "ord-found-1")
        self.assertEqual(res.status, "adopted_after_ambiguous")
        self.assertIsNone(om.resolution_halt)
        # adoption is durable
        self.assertIn("ord-found-1", JsonStore.load(_p(CFG.ORDERS_FILE), {}))
        # l'intention est close: l'ordre vit desormais dans open_orders
        self.assertNotIn(TICKER, om.pending_intents)

    def test_not_found_stays_fail_closed_and_never_reposts(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = []
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertEqual(client.create_order.call_count, 1)
        self.assertEqual(om.open_orders, {}, "nothing adopted on NOT_FOUND")
        # Une seule lecture vide ne CLOTURE rien: l'absence reste non
        # concluante tant que la politique n'a pas ses confirmations.
        self.assertTrue(res.status.endswith("not_found_pending"))
        self.assertIn(TICKER, om.session_submitted, "guard released")
        self.assertEqual(self._intents_on_disk()[TICKER]["resolution"],
                         "NOT_FOUND_PENDING")

        # a later cycle at a moved price must NOT create the order
        client2 = self._client()
        client2.create_order.side_effect = None
        client2.create_order.return_value = order_row(order_id="ord-new")
        res2 = self._submit(bot.OrderManager(client2), price=PRICE + 9)
        self.assertEqual(client2.create_order.call_count, 0)
        self.assertEqual(res2.status, "blocked:ambiguous_intent_unresolved")

    def test_multiple_matches_halt_every_submission(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = [
            order_row(order_id="dup-1"), order_row(order_id="dup-2")]
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertTrue(res.status.endswith("multiple"))
        self.assertEqual(om.resolution_halt["status"],
                         "MULTIPLE_ORDERS_SAME_CLIENT_ID")
        self.assertEqual(om.open_orders, {}, "adopted despite ambiguity")
        # the halt is GLOBAL: a different ticker is refused too
        other = om.place_and_track("KXOTHER-TICKER", SIDE, COUNT, PRICE)
        self.assertEqual(other.status, "blocked:ambiguous_resolution_halt")
        self.assertEqual(client.create_order.call_count, 1)

    def test_malformed_listing_halts_every_submission(self):
        client = self._client(
            lookup=KalshiAPIError(0, "listing d'ordres incoherent: bla"))
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertTrue(res.status.endswith("malformed"))
        self.assertEqual(om.resolution_halt["status"],
                         "MALFORMED_ORDER_LISTING")
        other = om.place_and_track("KXOTHER-TICKER", SIDE, COUNT, PRICE)
        self.assertEqual(other.status, "blocked:ambiguous_resolution_halt")

    def test_lookup_timeout_is_unavailable_not_absence(self):
        """The critical distinction: a failed lookup must never read as
        'the order does not exist'."""
        client = self._client(lookup=KalshiAPIError(0, "reseau: ReadTimeout"))
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertTrue(res.status.endswith("unavailable"))
        self.assertEqual(om.open_orders, {}, "adopted without proof")
        self.assertIsNone(om.resolution_halt, "a timeout is retryable")
        self.assertIn(TICKER, om.session_submitted)
        self.assertEqual(self._intents_on_disk()[TICKER]["resolution"],
                         "UNAVAILABLE")
        self.assertEqual(client.create_order.call_count, 1)

    def test_a_matching_order_without_order_id_is_malformed(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = [
            {"client_order_id": CID, "status": "resting"}]
        om = bot.OrderManager(client)

        res = self._submit(om)

        self.assertTrue(res.status.endswith("malformed"))
        self.assertEqual(om.open_orders, {})
        self.assertEqual(om.resolution_halt["status"],
                         "MALFORMED_ORDER_LISTING")


class RestartResolutionTest(_ResolverBase):
    """After a restart, the same question is asked again from the guard."""

    def _crash_after_ambiguous_post(self):
        client = self._client(lookup=KalshiAPIError(0, "reseau: timeout"))
        om = bot.OrderManager(client)
        self._submit(om)
        self.assertEqual(client.create_order.call_count, 1)
        self.assertEqual(self._intents_on_disk()[TICKER]["client_order_id"],
                         CID)
        del om

    def test_restart_replays_the_resolution_and_adopts(self):
        self._crash_after_ambiguous_post()

        client2 = self._client()
        client2.find_orders_by_client_order_id.return_value = [order_row()]
        om2 = bot.OrderManager(client2)
        self.assertIn(TICKER, om2.pending_intents, "intent lost on restart")

        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())

        self.assertEqual(
            client2.find_orders_by_client_order_id.call_args[0][0], CID,
            "restart did not re-ask with the same client_order_id")
        self.assertIn("ord-found-1", om2.open_orders)
        self.assertEqual(client2.create_order.call_count, 0,
                         "restart created an order")

    def test_restart_without_a_match_stays_locked_and_creates_nothing(self):
        self._crash_after_ambiguous_post()

        client2 = self._client()
        client2.find_orders_by_client_order_id.return_value = []
        client2.create_order.side_effect = None
        client2.create_order.return_value = order_row(order_id="ord-new")
        om2 = bot.OrderManager(client2)
        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())

        res = self._submit(om2, price=PRICE + 5)
        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(client2.create_order.call_count, 0)

    def test_restart_after_multiple_match_keeps_the_global_halt(self):
        self._crash_after_ambiguous_post()

        client2 = self._client()
        client2.find_orders_by_client_order_id.return_value = [
            order_row(order_id="dup-1"), order_row(order_id="dup-2")]
        client2.create_order.side_effect = None
        om2 = bot.OrderManager(client2)
        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())

        self.assertEqual(om2.resolution_halt["status"],
                         "MULTIPLE_ORDERS_SAME_CLIENT_ID")
        res = om2.place_and_track("KXANY-OTHER", SIDE, COUNT, PRICE)
        self.assertEqual(res.status, "blocked:ambiguous_resolution_halt")
        self.assertEqual(client2.create_order.call_count, 0)

    def test_an_adopted_intent_is_not_re_resolved_forever(self):
        client = self._client()
        client.find_orders_by_client_order_id.return_value = [order_row()]
        om = bot.OrderManager(client)
        self._submit(om)
        del om

        client2 = self._client()
        client2.find_orders_by_client_order_id.return_value = [order_row()]
        om2 = bot.OrderManager(client2)
        om2.reconcile_startup(MagicMock(trades=[]), MagicMock())

        self.assertEqual(client2.find_orders_by_client_order_id.call_count, 0,
                         "an already-adopted intent was re-queried")


class NoRepostBeforeResolutionTest(_ResolverBase):
    """No path creates a second order for the same intent, ever."""

    def test_ten_cycles_after_each_outcome_never_create_a_second_order(self):
        outcomes = {
            "found": [order_row()],
            "not_found": [],
            "multiple": [order_row(order_id="d1"), order_row(order_id="d2")],
        }
        for label, matches in outcomes.items():
            with self.subTest(case=label):
                self.setUp()
                first = self._client()
                first.find_orders_by_client_order_id.return_value = matches
                self._submit(bot.OrderManager(first))
                self.assertEqual(first.create_order.call_count, 1)

                later = []
                for i in range(10):
                    c = self._client()
                    c.create_order.side_effect = None
                    c.create_order.return_value = order_row(
                        order_id=f"ord-cycle-{i}")
                    c.find_orders_by_client_order_id.return_value = matches
                    self._submit(bot.OrderManager(c), price=PRICE + i + 1)
                    later.append(c)

                self.assertEqual(
                    sum(c.create_order.call_count for c in later), 0,
                    f"{label}: a second order was created")
                self._cleanup()

    def test_resolution_is_read_only(self):
        """Resolving must never write to the broker."""
        client = self._client()
        client.find_orders_by_client_order_id.return_value = [order_row()]
        om = bot.OrderManager(client)

        self._submit(om)

        self.assertEqual(client.create_order.call_count, 1)
        self.assertEqual(client.cancel_order.call_count, 0)


if __name__ == "__main__":
    unittest.main()
