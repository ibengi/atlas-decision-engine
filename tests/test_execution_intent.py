# -*- coding: utf-8 -*-
"""AIR-001 Wave 2 (DE-P0-003) — final-price economic invariant.

Permanent regression for the reproduced defect: before this wave, a
decision accepted at 48c was submitted at a fresh ask of 95c (past
MAX_ENTRY_CENTS, deeply negative edge) with zero rejections recorded.
Order submission now requires a ValidatedExecutionIntent produced by the
SAME shared economic gate (strategy_router.price_and_gate) over the
CURRENT book — any fresh-gate failure places NO order, visibly.
"""
import os
import sys
import tempfile
import unittest

_TMP0 = tempfile.mkdtemp(prefix="kalshi_intent_")
os.environ["DATA_DIR"] = _TMP0
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
import _bootstrap  # noqa: F401,E402

from strategy_router import (Decision, ModelOutput,  # noqa: E402
                             price_and_gate)
from execution_intent import (ValidatedExecutionIntent,  # noqa: E402
                              validate_fresh_execution)
from tests.test_pipeline_integration import (FakeClient,  # noqa: E402
                                             btcd_market, make_engine)

BOOK_A = {"yes_bid": 46, "yes_ask": 48, "no_bid": 52, "no_ask": 54,
          "spread": 2}


def accepted_decision(gates):
    model = ModelOutput(valid=True, reason=None, probability_yes=0.62,
                        confidence=9, features={"model_version": "vtest"})
    dec = Decision(ticker=btcd_market()["ticker"], strategy="btc_daily")
    dec = price_and_gate(dec, model, dict(BOOK_A), gates, btcd_market())
    assert dec.accepted and dec.side == "yes" and dec.entry_ask == 48
    return dec


def engine_and_decision(order_scenario="fill"):
    client = FakeClient(btcd_market(), order_scenario=order_scenario)
    eng, tmp = make_engine(client)
    # AIR-001 W6: _execute_decision is entered directly here (no cycle,
    # so _balance_gate never ran). The RiskProof requires a fetched
    # balance (balance_known) — provide the one the rig's capital uses.
    eng.last_balance = 500.0
    return client, eng, accepted_decision(eng.gates)


class TestFreshGateBlocks(unittest.TestCase):
    """Every mandated blocking condition, through the REAL execution
    path (`_execute_decision`), with the block counted as evidence."""

    def _run(self, client, eng, dec):
        report = {"rejections": {}}
        placed = eng._execute_decision(dec, report)
        return placed, report["rejections"], client.created_orders

    def test_adverse_price_move_blocks(self):
        client, eng, dec = engine_and_decision()
        client.market = btcd_market(yes_bid=93, yes_ask=95,
                                    no_bid=5, no_ask=99)
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual(placed, 0)
        self.assertEqual(orders, [])
        self.assertEqual(rejections.get("fresh_economic_gate"), 1)

    def test_edge_collapse_below_min_net_edge_blocks(self):
        # ask 58: gross = .62-.58 = .04 > 0 but net edge falls under the
        # MIN_NET_EDGE gate once fee+slippage+buffer apply.
        client, eng, dec = engine_and_decision()
        client.market = btcd_market(yes_bid=56, yes_ask=58,
                                    no_bid=42, no_ask=44)
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual((placed, orders), (0, []))
        self.assertEqual(rejections.get("fresh_economic_gate"), 1)

    def test_spread_deterioration_blocks(self):
        client, eng, dec = engine_and_decision()
        client.market = btcd_market(yes_bid=20, yes_ask=48,
                                    no_bid=52, no_ask=80)
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual((placed, orders), (0, []))
        self.assertEqual(rejections.get("fresh_economic_gate"), 1)

    def test_side_reversal_blocks(self):
        # Fresh book makes NO the best (and only) economic side: the
        # intent must refuse rather than silently flip the position.
        client, eng, dec = engine_and_decision()
        client.market = btcd_market(yes_bid=95, yes_ask=97,
                                    no_bid=1, no_ask=3)
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual((placed, orders), (0, []))
        self.assertEqual(rejections.get("fresh_economic_gate"), 1)

    def test_vanished_book_still_blocks(self):
        client, eng, dec = engine_and_decision()
        client.market = {"ticker": btcd_market()["ticker"],
                         "status": "open"}      # market row, empty book
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual((placed, orders), (0, []))
        self.assertEqual(rejections.get("stale_book"), 1)

    def test_unchanged_book_behavior_identical(self):
        client, eng, dec = engine_and_decision()
        placed, rejections, orders = self._run(client, eng, dec)
        self.assertEqual(placed, 1)
        self.assertEqual(orders[0]["price"], 48)     # the pipeline price
        self.assertEqual(orders[0]["side"], "yes")
        self.assertNotIn("fresh_economic_gate", rejections)
        intent = eng._last_execution_intent
        self.assertAlmostEqual(intent.net_edge, dec.net_edge)
        self.assertAlmostEqual(intent.net_ev, dec.net_ev)


class TestValidateFreshExecution(unittest.TestCase):
    def setUp(self):
        _, self.eng, self.dec = engine_and_decision()

    def test_stale_fresh_book_blocks(self):
        intent, why = validate_fresh_execution(
            self.dec, btcd_market(), dict(BOOK_A), self.eng.gates,
            fetched_at=0.0, now=100.0, max_age_s=10.0)
        self.assertIsNone(intent)
        self.assertTrue(why.startswith("stale_fresh_book"))

    def test_missing_model_probability_blocks(self):
        self.dec.model_output = {}
        intent, why = validate_fresh_execution(
            self.dec, btcd_market(), dict(BOOK_A), self.eng.gates,
            fetched_at=1.0, now=1.0)
        self.assertIsNone(intent)
        self.assertEqual(why, "model_probability_unavailable")

    def test_intent_carries_full_binding(self):
        intent, why = validate_fresh_execution(
            self.dec, btcd_market(), dict(BOOK_A), self.eng.gates,
            fetched_at=1.0, now=1.0)
        self.assertEqual(why, "")
        self.assertIsInstance(intent, ValidatedExecutionIntent)
        for field in ("market_id", "side", "model_probability",
                      "fresh_requested_price", "fresh_spread",
                      "fee_estimate", "slippage_estimate", "gross_edge",
                      "net_edge", "net_ev", "validated_at",
                      "market_data_timestamp", "market_data_age",
                      "engine_commit", "strategy_config_hash",
                      "risk_config_hash"):
            self.assertIsNotNone(getattr(intent, field), field)
        self.assertEqual(len(intent.content_hash()), 64)

    def test_property_sweep_gate_consistency(self):
        """Boundary property: across every fresh ask 1..99, an intent
        exists exactly when the shared gate accepts the SAME side at
        that price — never otherwise, never by a different formula."""
        for ask in range(1, 100):
            book = {"yes_bid": max(1, ask - 2), "yes_ask": ask,
                    "no_bid": 100 - ask, "no_ask": min(99, 102 - ask),
                    "spread": 2}
            intent, why = validate_fresh_execution(
                self.dec, btcd_market(), dict(book), self.eng.gates,
                fetched_at=1.0, now=1.0)
            model = ModelOutput(valid=True, reason=None,
                                probability_yes=0.62, confidence=9,
                                features={})
            ref = price_and_gate(
                Decision(ticker=self.dec.ticker, strategy="btc_daily"),
                model, dict(book), self.eng.gates, btcd_market())
            expected = ref.accepted and ref.side == self.dec.side
            self.assertEqual(intent is not None, expected,
                             f"ask={ask} why={why} "
                             f"ref={ref.rejection_reason}")
            if intent is not None:
                self.assertEqual(intent.fresh_requested_price,
                                 ref.entry_ask, f"ask={ask}")
                self.assertAlmostEqual(intent.net_edge, ref.net_edge,
                                       msg=f"ask={ask}")


if __name__ == "__main__":
    unittest.main()
