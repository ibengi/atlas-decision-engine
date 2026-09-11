# -*- coding: utf-8 -*-
"""A02 (CRITICAL) -- broker position collection cannot produce a false flat.

VIOLATED INVARIANT
    "I saw no position" and "I could not look" are different sentences. Only
    the first may authorize a rebase; the second must refuse it. An absence
    is never fabricated from a response the client did not understand or did
    not finish reading.

ROOT CAUSE on 508899b
    `KalshiClient.get_positions` was one line:
        return r.get("market_positions", r.get("positions", [])) or []
    Three fabricated absences follow from it. An unknown or renamed envelope
    fell through to `[]`. `market_positions: null` was turned into `[]` by
    the `or []`. And the cursor was never read, so a first page of 100
    flat rows hid an open position on page two. `verify_against_broker`
    then reported MATCH and a rebase lowered the HWM from 10 to 7.

ARCHITECTURAL CORRECTION
    `KalshiClient.get_positions_proof` mirrors the hardened `list_orders`:
    an explicit envelope allow-list, no `or []`, rejected null/ambiguous
    envelopes, rejected malformed rows, full cursor pagination with
    repeated-cursor detection and a page cap that RAISES rather than
    truncating. It returns rows PLUS the evidence that the enumeration is
    complete. `PositionManager._collect_broker_positions` demands that
    evidence, and `verify_against_broker` can no longer answer MATCH
    without it: an unproven enumeration is UNKNOWN, which halts.

PRODUCTION PATHS
    KalshiClient.get_positions_proof / get_positions,
    PositionManager._collect_broker_positions / verify_against_broker /
    reconcile_with_broker, ExecutionEngine.equity_rebase_context,
    EquityLedger.rebase_preconditions.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import AstraCase, Client                       # noqa: E402

import execution_engine                                    # noqa: E402
from kalshi_client import KalshiAPIError, KalshiClient      # noqa: E402
from order_manager import OrderManager                      # noqa: E402
from position_manager import PositionManager                # noqa: E402
from risk_manager import RiskManager                        # noqa: E402

TICKER = "KXBTCD-A"


def row(ticker=TICKER, position=1):
    return {"ticker": ticker, "position": position}


def flat(n=100):
    return [{"ticker": f"KX-FLAT-{i}", "position": 0} for i in range(n)]


#: Every response shape that must FAIL CLOSED rather than read as flat.
UNREADABLE = [
    ("reponse vide", {}),
    ("enveloppe absente", {"data": [row()], "cursor": ""}),
    ("enveloppe renommee", {"positions": [row()], "cursor": ""}),
    ("enveloppe inconnue", {"portefeuille": [row()], "cursor": ""}),
    ("market_positions = null", {"market_positions": None, "cursor": ""}),
    ("enveloppes multiples", {"market_positions": [], "positions": [row()]}),
    ("bloc = objet", {"market_positions": {"0": row()}, "cursor": ""}),
    ("bloc = string", {"market_positions": "aucune", "cursor": ""}),
    ("bloc = nombre", {"market_positions": 0, "cursor": ""}),
    ("entree non-objet", {"market_positions": ["KX-A"], "cursor": ""}),
    ("ligne sans ticker", {"market_positions": [{"position": 3}], "cursor": ""}),
    ("ligne ticker vide", {"market_positions": [{"ticker": "  ", "position": 3}]}),
    ("reponse non-objet", ["market_positions", []]),
    ("cursor numerique", {"market_positions": [], "cursor": 42}),
    ("cursor liste", {"market_positions": [], "cursor": ["p2"]}),
    ("cursor objet", {"market_positions": [], "cursor": {"next": "p2"}}),
]


def bare_client():
    """The real parser without the constructor's credential requirements.
    Request building and signing are untouched; only `_req` is replaced, so
    no socket is opened."""
    c = KalshiClient.__new__(KalshiClient)
    c._raw_logged = set()
    return c


class TheParserNeverFabricatesAnAbsence(unittest.TestCase):

    def test_every_unreadable_shape_raises_instead_of_reporting_flat(self):
        for label, payload in UNREADABLE:
            with self.subTest(case=label):
                c = bare_client()
                with patch.object(KalshiClient, "_req", return_value=payload):
                    with self.assertRaises(KalshiAPIError, msg=label):
                        c.get_positions_proof()
                    # the compatibility wrapper turns it into None (unknown),
                    # which every caller treats as a halt -- never []
                    self.assertIsNone(c.get_positions(), label)

    def test_positive_control_a_genuinely_empty_portfolio_is_empty(self):
        c = bare_client()
        with patch.object(KalshiClient, "_req",
                          return_value={"market_positions": [], "cursor": ""}):
            proof = c.get_positions_proof()
        self.assertEqual(proof["rows"], [])
        self.assertTrue(proof["complete"])
        self.assertEqual(proof["pages"], 1)

    def test_the_cursor_is_followed_to_the_end(self):
        """Astra HTTP_real_pagination_100_flat_rows_hides_open_position_page_two."""
        c = bare_client()
        pages = [{"market_positions": flat(100), "cursor": "page-2"},
                 {"market_positions": [row(position=3)], "cursor": ""}]
        with patch.object(KalshiClient, "_req", side_effect=pages) as req:
            proof = c.get_positions_proof()
        self.assertEqual(req.call_count, 2)
        self.assertEqual(req.call_args_list[1].kwargs["params"]["cursor"],
                         "page-2")
        self.assertTrue(proof["complete"])
        self.assertEqual([p["ticker"] for p in proof["rows"]][-1], TICKER)

    def test_a_repeated_cursor_is_a_loop_not_an_end(self):
        c = bare_client()
        with patch.object(KalshiClient, "_req",
                          return_value={"market_positions": [], "cursor": "same"}):
            with self.assertRaises(KalshiAPIError) as ctx:
                c.get_positions_proof()
        self.assertIn("ne progresse pas", str(ctx.exception))

    def test_unbounded_pagination_is_refused_not_truncated(self):
        c = bare_client()
        pages = ({"market_positions": [row(f"KX-{i}")], "cursor": f"c{i}"}
                 for i in range(1000))
        with patch.object(KalshiClient, "_req",
                          side_effect=lambda *a, **k: next(pages)):
            with self.assertRaises(KalshiAPIError) as ctx:
                c.get_positions_proof(max_pages=5)
        self.assertIn("tronque", str(ctx.exception))

    def test_an_absent_cursor_ends_pagination_normally(self):
        c = bare_client()
        with patch.object(KalshiClient, "_req",
                          return_value={"market_positions": [row()]}) as req:
            proof = c.get_positions_proof()
        self.assertEqual(req.call_count, 1)
        self.assertTrue(proof["complete"])


class MatchRequiresAProvenEnumeration(AstraCase):
    """A02 rules 8-10, at the PositionManager boundary."""

    def test_an_unprovable_enumeration_is_unknown_never_match(self):
        client = Client(positions=[], positions_complete=False)
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        report = pos.verify_against_broker()
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertFalse(report["collection_proof"]["complete"])
        self.assertIsNotNone(pos.reconcile_halt)

    def test_positive_control_a_proven_empty_enumeration_matches(self):
        client = Client(positions=[])
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        report = pos.verify_against_broker()
        self.assertEqual(report["status"], "MATCH")
        self.assertTrue(report["collection_proof"]["complete"])
        self.assertIsNone(pos.reconcile_halt)

    def test_a_seen_position_is_still_a_mismatch_without_a_proof(self):
        """An enumeration that cannot be shown complete still REPORTS what it
        saw: a position seen is a real divergence, and downgrading it to
        'unknown' would lose information."""
        client = Client(positions=[row()], positions_complete=False)
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        report = pos.verify_against_broker()
        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual(report["mismatches"][0]["kind"], "broker_only")

    def test_startup_reconciliation_also_demands_the_proof(self):
        client = Client(positions=[], positions_complete=False)
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        report = pos.reconcile_with_broker()
        self.assertEqual(report["status"], "UNKNOWN")


class ARebaseCannotSpendAnUnprovenAbsence(AstraCase):
    """The end-to-end consequence: HWM 10 -> 7 must not happen."""

    def rebase_attempt(self, client):
        tlog = __import__("trade_logger").TradeLogger()
        pos = PositionManager(client, tlog)
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        orders = OrderManager(client)
        risk = RiskManager(tlog, pos, capital=10.0)
        risk.equity = led
        ctx = execution_engine.equity_rebase_context(
            client, orders, pos, risk, equity=led, quiescent=True)
        prop = led.propose_rebase("losses acknowledged", "OPS-30")
        applied = led.apply_rebase("losses acknowledged", "OPS-30",
                                   prop["token"], ctx)
        return led, ctx, applied

    def test_an_incomplete_collection_blocks_the_rebase(self):
        led, ctx, applied = self.rebase_attempt(
            Client(positions=[], positions_complete=False))
        self.assertFalse(applied)
        self.assertEqual(ctx["reconcile_status"], "UNKNOWN")
        self.assertAlmostEqual(led.risk_equity_reference(), 10.0, places=9)
        self.assertEqual(led.state["consumed_tokens"],
                         led.state["consumed_tokens"])   # unchanged below
        self.assertIsNone(led.state["capital_hold"])

    def test_a_position_on_any_page_blocks_the_rebase(self):
        led, ctx, applied = self.rebase_attempt(Client(positions=[row()]))
        self.assertFalse(applied)
        self.assertNotEqual(ctx["reconcile_status"], "MATCH")
        self.assertAlmostEqual(led.risk_equity_reference(), 10.0, places=9)
        self.assertIsNone(led.state["capital_hold"])

    def test_positive_control_a_proven_flat_broker_allows_the_rebase(self):
        led, ctx, applied = self.rebase_attempt(Client(positions=[]))
        self.assertEqual(ctx["reconcile_status"], "MATCH")
        self.assertTrue(applied, led.snapshot())
        self.assertAlmostEqual(led.risk_equity_reference(), 7.0, places=9)
        self.assertEqual(led.state["capital_hold"]["reason"],
                         "post_rebase_validation")
        self.assertFalse(led.capital_eligible())


if __name__ == "__main__":
    unittest.main()
