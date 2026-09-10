# -*- coding: utf-8 -*-
"""A17 / A09 -- unreadable intent state is never an empty intent state.

VIOLATED INVARIANT (Astra, on 3af848e)
    "There is nothing in flight" and "I cannot tell whether anything is in
    flight" arrived at every gate as the same empty dict, so a corrupt,
    truncated or malformed ``pending_intents.json`` skipped recovery and
    cleared the engine to submit.

ROOT CAUSE
    ``JsonStore.load`` answers the caller's default for absent, unreadable,
    unparsable and all-backups-bad alike, and never trips the persistence
    sentinel on a READ. ``OrderManager.__init__`` then filtered malformed
    rows out silently, so a row recording a real in-flight order whose
    ``client_order_id`` had been truncated simply disappeared.

ARCHITECTURAL CORRECTION
    ``JsonStore.load_reporting`` returns *why* it answered.
    ``OrderManager`` raises ``intent_recovery_required``, preserves the
    malformed rows as evidence, trips the sentinel, and refuses every
    submission until the state is reconciled. The A09 read-back binds the
    whole intent and is re-confirmed at the linearization point.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_astra_a09_a12_execution import TICKER, _TransportTripwire  # noqa: E402
from _astra import AstraCase                                   # noqa: E402

from config import _p                                          # noqa: E402
from execution_result import ExecutionResult                   # noqa: E402
from order_manager import OrderManager                         # noqa: E402
from persistence import JsonStore, PersistenceSentinel         # noqa: E402
from position_manager import PositionManager                   # noqa: E402
from trade_logger import TradeLogger                           # noqa: E402


class UnreadableIntentsBlockEverySubmission(AstraCase):

    def write_raw_intents(self, text):
        with open(_p(OrderManager.PENDING_FILE), "w", encoding="utf-8") as fh:
            fh.write(text)

    def restart(self):
        """A genuine restart: a brand-new OrderManager reading the disk."""
        client = _TransportTripwire()
        tlog = TradeLogger()
        pos = PositionManager(client, tlog)
        return client, OrderManager(client)

    def submit(self, om):
        return om.place_and_track(TICKER, "yes", 1, 40)

    #: Two layers refuse an unreadable intent state, and either is a pass:
    #: the CRITICAL-file sentinel (unreadable pending_intents.json is a
    #: persistence failure) and the A17 gate in place_and_track. The
    #: invariant under test is that NOTHING reaches transport; which layer
    #: caught it is an implementation detail, and the test below isolates
    #: the A17 gate on its own so neither layer can be the only one working.
    BLOCK_REASONS = ("intent_recovery_required", "persistence_failure")

    def assert_blocked(self, client, result):
        self.assertEqual(client.create_calls, 0,
                         "a submission reached broker transport while the "
                         "intent state was unknown")
        self.assertIsInstance(result, ExecutionResult)
        self.assertEqual(result.state, "rejected")
        self.assertTrue(
            any(r in str(result.status) for r in self.BLOCK_REASONS),
            f"blocked for an unrelated reason: {result.status!r}")

    def test_the_a17_gate_blocks_even_with_a_healthy_sentinel(self):
        """Isolates the gate: the sentinel is cleared after load, so the
        only thing left standing between this call and the broker is the
        unreadable-intent gate itself."""
        self.write_raw_intents("{ broken")
        client, om = self.restart()
        PersistenceSentinel.reset()
        self.assertTrue(PersistenceSentinel.healthy())
        self.assertFalse(om.intent_state_trustworthy())
        result = self.submit(om)
        self.assertEqual(client.create_calls, 0)
        self.assertIn("intent_recovery_required", str(result.status))

    # ── the corruption modes ────────────────────────────────────────────
    def test_invalid_json_blocks_and_is_not_an_empty_state(self):
        self.write_raw_intents("{ this is not json")
        client, om = self.restart()
        self.assertTrue(om.intent_recovery_required)
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_a_truncated_tail_blocks(self):
        self.write_raw_intents(
            '{"' + TICKER + '": {"client_order_id": "alpha_abc", "count": 1')
        client, om = self.restart()
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_a_malformed_row_is_preserved_as_evidence_not_dropped(self):
        self.write_raw_intents(json.dumps({
            TICKER: {"client_order_id": "alpha_ok", "count": 1, "price": 40,
                     "resolution": None},
            "KXBTC15M-BROKEN": {"count": 1},          # no client_order_id
        }))
        client, om = self.restart()
        self.assertTrue(om.malformed_intents,
                        "a malformed intent row was silently discarded")
        self.assertEqual(om.malformed_intents[0]["ticker"], "KXBTC15M-BROKEN")
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_a_row_that_is_not_an_object_blocks(self):
        self.write_raw_intents(json.dumps({TICKER: "not-an-object"}))
        client, om = self.restart()
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_a_top_level_array_blocks_instead_of_crashing(self):
        self.write_raw_intents(json.dumps([{"client_order_id": "x"}]))
        client, om = self.restart()          # must not raise
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_an_unknown_schema_row_blocks(self):
        self.write_raw_intents(json.dumps({
            TICKER: {"schema": "v9", "unknown_field": 1}}))
        client, om = self.restart()
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    def test_the_corruption_trips_the_persistence_sentinel(self):
        self.write_raw_intents("{ broken")
        self.restart()
        self.assertFalse(PersistenceSentinel.healthy(),
                         "an unreadable CRITICAL file left the sentinel green")

    def test_the_block_survives_a_second_restart(self):
        self.write_raw_intents("{ broken")
        self.restart()
        PersistenceSentinel.reset()
        client, om = self.restart()
        self.assertFalse(om.intent_state_trustworthy())
        self.assert_blocked(client, self.submit(om))

    # ── controls: the healthy paths still work ──────────────────────────
    def test_a_genuinely_absent_file_is_not_a_corruption(self):
        path = _p(OrderManager.PENDING_FILE)
        if os.path.exists(path):
            os.unlink(path)
        client, om = self.restart()
        self.assertTrue(om.intent_state_trustworthy())
        self.assertEqual(om.pending_intents, {})

    def test_a_clean_intent_file_loads_and_submits(self):
        self.write_raw_intents(json.dumps({}))
        client, om = self.restart()
        self.assertTrue(om.intent_state_trustworthy())
        result = self.submit(om)
        self.assertEqual(client.create_calls, 1,
                         "the control could not submit: the block is not "
                         "specific to corruption")
        self.assertIsInstance(result, ExecutionResult)

    def test_an_intact_unresolved_intent_still_blocks_that_ticker(self):
        self.write_raw_intents(json.dumps({
            TICKER: {"client_order_id": "alpha_x", "count": 1, "price": 40,
                     "resolution": None}}))
        client, om = self.restart()
        self.assertTrue(om.intent_state_trustworthy())
        result = self.submit(om)
        self.assertEqual(client.create_calls, 0)
        self.assertIsInstance(result, ExecutionResult)
