# -*- coding: utf-8 -*-
"""Order-listing schema: an unreadable answer is never an empty one.

`list_orders` used to unwrap the response with

    orders = r.get("orders", r.get("data", []))

whose `[]` default is a silent lie: if the real envelope carried another
name, a FULL response read as an EMPTY, COMPLETE listing -- in other
words, as an absence. And an absence is the one answer that must never
be fabricated, because two of them close an ambiguous intent and release
the ticker.

This is not a hypothetical shape mismatch: the sibling endpoint proves
Kalshi does not always pluralise the obvious way (`get_positions`
unwraps `market_positions`, not `positions`). An envelope this client
does not recognise is therefore an INCOMPREHENSIBLE response, not an
empty one, and it must fail closed.

Every failure below must reach the caller as MALFORMED or UNAVAILABLE --
never NOT_FOUND -- and must not let a second order be created.

Mock transport only: no network, no DEMO order, no LIVE order.
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
from persistence import PersistenceSentinel  # noqa: E402

TICKER = "KXBTCD-CANARY-T1"
SIDE, COUNT, PRICE = "yes", 1, 40
CID = bot.OrderManager._client_order_id(TICKER, SIDE, COUNT, PRICE)


def order_row(order_id="ord-1", client_order_id=CID):
    return {"order_id": order_id, "client_order_id": client_order_id,
            "ticker": TICKER, "side": SIDE, "status": "resting",
            "fill_count": 0, "remaining_count": COUNT}


#: Every response shape that must FAIL CLOSED rather than read as empty.
MALFORMED_RESPONSES = [
    ("reponse vide", {}),
    ("cle orders absente", {"data": [order_row()], "cursor": ""}),
    ("enveloppe inconnue", {"resultats": [order_row()], "cursor": ""}),
    ("enveloppe plurielle inattendue", {"market_orders": [order_row()]}),
    ("orders = null", {"orders": None, "cursor": ""}),
    ("orders = objet", {"orders": {"0": order_row()}, "cursor": ""}),
    ("orders = string", {"orders": "aucun", "cursor": ""}),
    ("orders = nombre", {"orders": 0, "cursor": ""}),
    ("entree non-objet", {"orders": ["ord-1"], "cursor": ""}),
    ("reponse non-objet", ["orders", []]),
    ("cursor numerique", {"orders": [], "cursor": 42}),
    ("cursor liste", {"orders": [], "cursor": ["p2"]}),
    ("cursor objet", {"orders": [], "cursor": {"next": "p2"}}),
]


class ClientSchemaTest(unittest.TestCase):
    """The parser itself."""

    def _client(self):
        c = KalshiClient.__new__(KalshiClient)
        c._raw_logged = set()
        return c

    def test_every_unreadable_shape_raises_instead_of_reporting_empty(self):
        for label, payload in MALFORMED_RESPONSES:
            with self.subTest(case=label):
                c = self._client()
                with patch.object(KalshiClient, "_req", return_value=payload):
                    with self.assertRaises(KalshiAPIError, msg=label):
                        c.list_orders(ticker=TICKER)

    def test_a_genuinely_empty_listing_is_accepted_as_empty(self):
        """The one shape that legitimately means 'no orders'."""
        c = self._client()
        with patch.object(KalshiClient, "_req",
                          return_value={"orders": [], "cursor": ""}):
            self.assertEqual(c.list_orders(ticker=TICKER), [])
            self.assertEqual(c.find_orders_by_client_order_id(CID), [])

    def test_a_normal_listing_parses(self):
        c = self._client()
        payload = {"orders": [order_row("a"), order_row("b", "alpha_other")],
                   "cursor": ""}
        with patch.object(KalshiClient, "_req", return_value=payload):
            self.assertEqual(len(c.list_orders(ticker=TICKER)), 2)
            self.assertEqual(
                [o["order_id"]
                 for o in c.find_orders_by_client_order_id(CID)], ["a"])

    def test_an_absent_cursor_ends_pagination_normally(self):
        """Absence of `cursor` is a legitimate terminal condition; only a
        cursor of the WRONG TYPE is a schema failure."""
        c = self._client()
        with patch.object(KalshiClient, "_req",
                          return_value={"orders": [order_row()]}) as req:
            self.assertEqual(len(c.list_orders(ticker=TICKER)), 1)
        self.assertEqual(req.call_count, 1)

    def test_a_null_cursor_ends_pagination_normally(self):
        c = self._client()
        with patch.object(KalshiClient, "_req",
                          return_value={"orders": [], "cursor": None}):
            self.assertEqual(c.list_orders(ticker=TICKER), [])

    def test_multi_page_pagination_collects_every_page(self):
        c = self._client()
        pages = [
            {"orders": [order_row("p1", "alpha_x")], "cursor": "c2"},
            {"orders": [order_row("p2", "alpha_y")], "cursor": "c3"},
            {"orders": [order_row("p3")], "cursor": ""},
        ]
        with patch.object(KalshiClient, "_req", side_effect=pages) as req:
            rows = c.list_orders(ticker=TICKER)
        self.assertEqual([o["order_id"] for o in rows], ["p1", "p2", "p3"])
        self.assertEqual(req.call_count, 3)
        self.assertEqual(req.call_args_list[2].kwargs["params"]["cursor"], "c3")

    def test_an_explicitly_empty_last_page_is_allowed(self):
        """A final page carrying no rows is legal, not a schema failure."""
        c = self._client()
        pages = [{"orders": [order_row("p1")], "cursor": "c2"},
                 {"orders": [], "cursor": ""}]
        with patch.object(KalshiClient, "_req", side_effect=pages):
            rows = c.list_orders(ticker=TICKER)
        self.assertEqual([o["order_id"] for o in rows], ["p1"])

    def test_a_non_progressing_cursor_raises(self):
        """The same cursor returned twice is an endless listing, not a
        completed one."""
        c = self._client()
        pages = [{"orders": [], "cursor": "same"},
                 {"orders": [], "cursor": "same"}]
        with patch.object(KalshiClient, "_req", side_effect=pages) as req:
            with self.assertRaises(KalshiAPIError) as ctx:
                c.list_orders(ticker=TICKER)
        self.assertIn("progresse", str(ctx.exception))
        self.assertEqual(req.call_count, 2, "the loop did not stop early")

    def test_a_wrong_typed_cursor_raises_on_the_page_that_carried_it(self):
        """Pinned separately, because a mutation that drops the cursor type
        check is otherwise MASKED: `str(42)` is a usable cursor string, the
        next page repeats, and the non-progressing guard fires instead. A
        second defence catching the fault is not the same as the fault
        being detected where it happens -- and with a real broker the pages
        would NOT repeat, so nothing would fire at all.

        The listing here progresses normally, so only the type check can
        stop it on page 1."""
        for bad in (42, ["p2"], {"next": "p2"}, 3.5, True):
            with self.subTest(cursor=bad):
                c = self._client()
                pages = [{"orders": [], "cursor": bad},
                         {"orders": [order_row("late")], "cursor": ""}]
                with patch.object(KalshiClient, "_req",
                                  side_effect=pages) as req:
                    with self.assertRaises(KalshiAPIError) as ctx:
                        c.list_orders(ticker=TICKER)
                self.assertIn("cursor", str(ctx.exception))
                self.assertIn("incoherent", str(ctx.exception))
                self.assertEqual(req.call_count, 1,
                                 "pagination continued on an unusable cursor")

    def test_truncation_at_max_pages_still_raises(self):
        """Distinct cursors that never end: the completeness guard fires."""
        c = self._client()
        pages = [{"orders": [], "cursor": f"c{i}"} for i in range(1, 40)]
        with patch.object(KalshiClient, "_req", side_effect=pages) as req:
            with self.assertRaises(KalshiAPIError) as ctx:
                c.list_orders(ticker=TICKER)
        self.assertIn("tronque", str(ctx.exception))
        self.assertEqual(req.call_count, 10)

    def test_schema_errors_are_labelled_incoherent_not_absent(self):
        """The label matters: `resolve_intent` maps 'incoherent' to
        MALFORMED. A schema error that missed this word would degrade to
        UNAVAILABLE -- still fail-closed, but the wrong diagnosis."""
        c = self._client()
        for label, payload in MALFORMED_RESPONSES:
            with self.subTest(case=label):
                with patch.object(KalshiClient, "_req", return_value=payload):
                    try:
                        c.list_orders(ticker=TICKER)
                        self.fail(f"{label} did not raise")
                    except KalshiAPIError as e:
                        self.assertIn("incoherent", str(e))


class ResolutionOutcomeTest(unittest.TestCase):
    """A schema failure must never surface as NOT_FOUND."""

    #: Captured ONCE. A second capture would record the values this class
    #: already installed as if they were the originals, and restore them
    #: globally -- leaking MAX_CONTRACTS_PER_ORDER=1 into every later test.
    _saved = None
    _OVERRIDES = ("DATA_DIR", "REQUIRE_PERSISTENT_STATE",
                  "ALLOW_ORDER_SUBMISSION", "MAX_CONTRACTS_PER_ORDER",
                  "ALLOW_FRESH_STATE", "SUBMIT_DEDUP_TTL_S",
                  "ORDER_TTL_SECONDS")

    def setUp(self):
        if ResolutionOutcomeTest._saved is None:
            ResolutionOutcomeTest._saved = {
                k: getattr(CFG, k) for k in self._OVERRIDES}
        self._tmps = []
        CFG.REQUIRE_PERSISTENT_STATE = True
        CFG.ALLOW_FRESH_STATE = True
        CFG.ALLOW_ORDER_SUBMISSION = True
        CFG.MAX_CONTRACTS_PER_ORDER = "1"
        CFG.SUBMIT_DEDUP_TTL_S = 6 * 3600.0
        CFG.ORDER_TTL_SECONDS = 1
        self._fresh_state()
        self.addCleanup(self._cleanup)

    def _fresh_state(self):
        """A clean DATA_DIR for a sub-case, WITHOUT re-running setUp."""
        self.tmp = tempfile.mkdtemp(prefix="atlas_schema_")
        self._tmps.append(self.tmp)
        CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()

    def _cleanup(self):
        PersistenceSentinel.reset()
        for k, v in ResolutionOutcomeTest._saved.items():
            setattr(CFG, k, v)
        for t in self._tmps:
            shutil.rmtree(t, ignore_errors=True)

    def _om_with_response(self, payload):
        """A real KalshiClient method over a mocked transport, wired into a
        real OrderManager: the whole path, only the socket replaced."""
        client = MagicMock()
        client.env = "demo"
        client.last_http_status = 201
        client.create_order.side_effect = KalshiAPIError(0, "reseau: timeout")
        client.get_positions.return_value = []
        probe = KalshiClient.__new__(KalshiClient)
        probe._raw_logged = set()

        def _lookup(cid, ticker=None):
            with patch.object(KalshiClient, "_req", return_value=payload):
                return probe.find_orders_by_client_order_id(cid, ticker=ticker)

        client.find_orders_by_client_order_id.side_effect = _lookup
        return bot.OrderManager(client), client

    def test_unknown_envelope_yields_malformed_never_not_found(self):
        om, client = self._om_with_response(
            {"resultats": [order_row()], "cursor": ""})

        res = om.place_and_track(TICKER, SIDE, COUNT, PRICE)

        intent = om.pending_intents[TICKER]
        self.assertEqual(intent["resolution"], "MALFORMED")
        self.assertNotEqual(intent["resolution"], "NOT_FOUND_PENDING")
        self.assertEqual(intent.get("not_found_count", 0), 0,
                         "a schema failure counted as evidence of absence")
        self.assertEqual(om.resolution_halt["status"],
                         "MALFORMED_ORDER_LISTING")
        self.assertTrue(res.status.endswith("malformed"))

    def test_a_real_empty_listing_still_yields_not_found(self):
        """The control: the fix must not turn a genuine absence into a
        malformed report."""
        om, _client = self._om_with_response({"orders": [], "cursor": ""})

        om.place_and_track(TICKER, SIDE, COUNT, PRICE)

        intent = om.pending_intents[TICKER]
        self.assertEqual(intent["resolution"], "NOT_FOUND_PENDING")
        self.assertEqual(intent["not_found_count"], 1)
        self.assertIsNone(om.resolution_halt)

    def test_a_real_match_is_still_found(self):
        om, client = self._om_with_response(
            {"orders": [order_row("ord-real")], "cursor": ""})

        res = om.place_and_track(TICKER, SIDE, COUNT, PRICE)

        self.assertIn("ord-real", om.open_orders)
        self.assertEqual(res.status, "adopted_after_ambiguous")
        self.assertEqual(client.create_order.call_count, 1)

    def test_the_fail_closed_state_survives_a_restart(self):
        """Restart harness: a process reboot must not launder an
        unreadable listing into a fresh, unencumbered ticker."""
        payload = {"resultats": [order_row()], "cursor": ""}
        om, _client = self._om_with_response(payload)
        om.place_and_track(TICKER, SIDE, COUNT, PRICE)
        self.assertEqual(om.pending_intents[TICKER]["resolution"], "MALFORMED")
        cid_before = om.pending_intents[TICKER]["client_order_id"]

        # New process, same DATA_DIR, same unreadable broker.
        om2, client2 = self._om_with_response(payload)

        self.assertIn(TICKER, om2.pending_intents,
                      "the ambiguous intent did not survive the restart")
        self.assertEqual(om2.pending_intents[TICKER]["client_order_id"],
                         cid_before)
        self.assertEqual(
            om2.pending_intents[TICKER].get("not_found_count", 0), 0,
            "a restart turned an unreadable listing into absence evidence")

        client2.create_order.reset_mock()
        client2.create_order.side_effect = None
        client2.create_order.return_value = order_row("ord-after-restart")
        res = om2.place_and_track(TICKER, SIDE, COUNT, PRICE)

        self.assertEqual(client2.create_order.call_count, 0,
                         "a restart allowed a second order")
        self.assertEqual(res.state, "rejected")
        # Pinned: the refusal comes from the UNRESOLVED INTENT, not from
        # the dedup guard happening to still hold the same parameters.
        self.assertEqual(res.status, "blocked:ambiguous_intent_unresolved")
        other = om2.place_and_track(TICKER, SIDE, COUNT, PRICE + 11)
        self.assertEqual(other.status, "blocked:ambiguous_intent_unresolved")
        self.assertEqual(client2.create_order.call_count, 0)
        self.assertEqual(om2.pending_intents[TICKER]["resolution"],
                         "MALFORMED",
                         "re-reading the same bad listing changed the verdict")

    def test_no_parsing_failure_can_produce_a_second_order(self):
        for label, payload in MALFORMED_RESPONSES:
            with self.subTest(case=label):
                self._fresh_state()
                om, client = self._om_with_response(payload)
                om.place_and_track(TICKER, SIDE, COUNT, PRICE)
                self.assertEqual(client.create_order.call_count, 1,
                                 f"{label}: more than the initial POST")

                client.create_order.reset_mock()
                client.create_order.side_effect = None
                client.create_order.return_value = order_row("ord-new")
                res = om.place_and_track(TICKER, SIDE, COUNT, PRICE + 7)

                self.assertEqual(client.create_order.call_count, 0,
                                 f"{label}: a parsing failure allowed a order")
                self.assertEqual(client.cancel_order.call_count, 0)
                self.assertTrue(res.state == "rejected")


if __name__ == "__main__":
    unittest.main()
