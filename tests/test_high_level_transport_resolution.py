"""M3 bridge: high-level pending rows require proven transport outcomes."""
import copy
from unittest.mock import patch

from test_engine_authority import AuthorityCase
from test_transport_lifecycle import MemoryBroker, FinalEvidence
from config import CFG, _p
from kalshi_client import KalshiAPIError
from order_manager import OrderManager
from persistence import JsonStore, PersistenceSentinel
from transport_intent import FILE, outcome_for_client_order


class HighLevelTransportResolution(AuthorityCase):
    ticker = "KXBTC15M-bridge"
    cid = "synthetic-bridge-order"

    def setUp(self):
        super().setUp()
        self.prove()
        self.client = MemoryBroker(self.authority)
        self.om = OrderManager(self.client)
        p = patch.object(CFG, "ALLOW_ORDER_SUBMISSION", False)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.om._record_intent(self.ticker, self.cid, 1, 25, side="yes"))

    def send(self, mode="timeout_absent", count="1"):
        self.client.mode = mode
        try:
            self.client._req("POST", "/portfolio/events/orders", json={
                "client_order_id": self.cid, "ticker": self.ticker,
                "side": "bid", "count": count, "price": "0.2500",
                "time_in_force": "good_till_canceled"})
        except KalshiAPIError:
            pass

    def final_evidence(self, outcome):
        self.client.transport_evidence_provider = FinalEvidence(self.ledger.identity, outcome)

    def durable_pending(self):
        return JsonStore.load(_p(self.om.PENDING_FILE), {})

    def test_unknown_transport_retains_high_level_intent(self):
        self.send()
        result = self.om.resolve_pending_intents()
        self.assertEqual(result[self.ticker], "TRANSPORT_PENDING")
        self.assertIn(self.ticker, self.om.pending_intents)
        self.assertIn(self.ticker, self.durable_pending())
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "UNKNOWN")

    def test_partial_presence_does_not_erase_unknown_transport(self):
        self.send()
        self.client.orders = [{"client_order_id": self.cid, "ticker": self.ticker,
                               "order_id": "partial-response", "side": "bid"}]
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "TRANSPORT_PENDING")
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "UNKNOWN")
        self.assertIn(self.ticker, self.durable_pending())
        self.assertEqual(self.om.open_orders, {})

    def test_authenticated_absence_closes_high_level_pending(self):
        self.send()
        self.final_evidence("CONFIRMED_NOT_APPLIED")
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "CLOSED_ABSENT")
        self.assertEqual(self.om.pending_intents, {})
        self.assertEqual(self.durable_pending(), {})
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "CONFIRMED_NOT_APPLIED")
        self.assertEqual(len(self.client.calls), 1)

    def test_authenticated_terminal_failure_closes_high_level_pending(self):
        self.send("rejection")
        self.final_evidence("TERMINAL_FAILED")
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "CLOSED_ABSENT")
        self.assertEqual(self.durable_pending(), {})
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "TERMINAL_FAILED")

    def test_restart_resolves_confirmed_absence_without_a_second_send(self):
        self.send()
        self.final_evidence("CONFIRMED_NOT_APPLIED")
        restarted = OrderManager(self.client)
        restarted.reconcile_startup(self.tlog, self.pos)
        self.assertEqual(restarted.pending_intents, {})
        self.assertEqual(self.durable_pending(), {})
        self.assertEqual(len(self.client.calls), 1)

    def test_confirmed_presence_adopts_only_after_transport_resolution(self):
        self.client.visible = False
        self.send("timeout_present")
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "TRANSPORT_PENDING")
        self.client.visible = True
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "FOUND")
        self.assertIn("order-" + self.cid, self.om.open_orders)
        self.assertEqual(self.om.open_orders["order-" + self.cid]["side"], "yes")
        self.assertEqual(self.durable_pending(), {})
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "CONFIRMED_APPLIED")

    def test_missing_low_level_row_never_turns_empty_counts_into_absence(self):
        with patch.object(CFG, "AMBIGUOUS_NOT_FOUND_INTERVAL_S", 0):
            for _ in range(5):
                self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "NOT_FOUND_PENDING")
        self.assertEqual(self.om.pending_intents[self.ticker]["not_found_count"], 5)
        self.assertIn(self.ticker, self.durable_pending())
        self.assertIsNone(outcome_for_client_order(self.cid))

    def test_legacy_high_level_intent_can_adopt_exact_independent_presence(self):
        self.client.orders = [{"client_order_id": self.cid, "ticker": self.ticker,
            "order_id": "legacy-present", "side": "bid", "initial_count": "1",
            "price": "0.2500", "status": "resting"}]
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "FOUND")
        self.assertIn("legacy-present", self.om.open_orders)
        self.assertEqual(self.durable_pending(), {})
        self.assertEqual(self.client.calls, [])

    def test_legacy_partial_presence_retains_pending(self):
        self.client.orders = [{"client_order_id": self.cid, "ticker": self.ticker,
                               "order_id": "legacy-partial", "side": "bid"}]
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "MALFORMED")
        self.assertIn(self.ticker, self.durable_pending())
        self.assertEqual(self.om.open_orders, {})

    def test_transport_terms_must_match_the_high_level_intent(self):
        self.send("success", count="2")
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "CONFIRMED_APPLIED")
        self.assertEqual(self.om.resolve_pending_intents()[self.ticker], "MALFORMED")
        self.assertIn(self.ticker, self.durable_pending())
        self.assertEqual(self.om.open_orders, {})

    def test_failed_high_level_closure_keeps_both_durable_evidence_and_pending(self):
        self.send()
        self.final_evidence("CONFIRMED_NOT_APPLIED")
        from transport_intent import reconcile_transport_intents
        reconcile_transport_intents(self.client)
        before = copy.deepcopy(self.om.pending_intents)
        with patch.object(JsonStore, "save", return_value=False):
            outcome = self.om.resolve_intent(self.ticker, self.om.pending_intents[self.ticker])
        self.assertEqual(outcome, "UNAVAILABLE")
        self.assertEqual(self.om.pending_intents, before)
        PersistenceSentinel.reset()  # inspect durable bytes after the failed write
        self.assertEqual(self.durable_pending(), before)
        self.assertEqual(outcome_for_client_order(self.cid)["state"], "CONFIRMED_NOT_APPLIED")

    def test_proven_first_closure_permits_second_distinct_intent(self):
        self.send()
        self.final_evidence("CONFIRMED_NOT_APPLIED")
        self.om.resolve_pending_intents()
        first = copy.deepcopy(outcome_for_client_order(self.cid))
        second_cid, second_ticker = "synthetic-second", "KXBTC15M-second"
        self.assertTrue(self.om._record_intent(second_ticker, second_cid, 1, 25, side="yes"))
        self.client.mode = "success"
        self.client._req("POST", "/portfolio/events/orders", json={
            "client_order_id": second_cid, "ticker": second_ticker,
            "side": "bid", "count": "1", "price": "0.2500",
            "time_in_force": "good_till_canceled"})
        self.assertEqual(self.om.resolve_pending_intents()[second_ticker], "FOUND")
        self.assertEqual(outcome_for_client_order(self.cid), first)
        self.assertEqual(outcome_for_client_order(second_cid)["state"], "CONFIRMED_APPLIED")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.durable_pending(), {})
