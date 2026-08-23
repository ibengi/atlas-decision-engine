# -*- coding: utf-8 -*-
"""AIR-001 Wave 3 (DE-P0-004/DE-P1-001/DE-P1-005) — typed Kalshi boundary.

Permanent regression for the reproduced defect: a broker position in the
current fixed-point schema was parsed to quantity 0 by tolerant
fallbacks, silently skipped, and the matching LOCAL position was ghosted
and DELETED (open_risk collapsed to 0). Fixtures below use the current
documented Kalshi field shapes and prove exact Decimal preservation,
no-guess schema policy, and honest reconciliation classification.
"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

_TMP0 = tempfile.mkdtemp(prefix="kalshi_contracts_")
os.environ["DATA_DIR"] = _TMP0
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
import _bootstrap  # noqa: F401,E402

from exchange.kalshi_contracts import (  # noqa: E402
    SchemaIncompatible, classify_positions, parse_balance, parse_fill,
    parse_order, parse_position, parse_settlement)
from config import CFG  # noqa: E402
from position_manager import PositionManager  # noqa: E402


def local_pos(ticker, side="yes", count=3, avg=48, state="open"):
    return {"trade_id": f"t-{ticker}-{side}", "ticker": ticker,
            "side": side, "count_initial": count, "count": count,
            "avg_price": avg, "fees": 0.1, "opened_at": "2026-08-19",
            "order_ids": [], "fill_ids": [], "state": state,
            "strategy": "s", "market_score": None, "entry_edge": None,
            "entry_ev": None}


class TestDecimalExactParsing(unittest.TestCase):
    def test_position_current_fixed_point_schema(self):
        p = parse_position({"ticker": "T1", "position_fp": "3",
                            "market_exposure_dollars": "1.44",
                            "total_traded_dollars": "12.345",
                            "fees_paid_dollars": "0.07"})
        self.assertEqual(p.quantity.count, Decimal("3"))
        self.assertEqual(p.quantity.source_field, "position_fp")
        self.assertEqual(p.market_exposure.dollars, Decimal("1.44"))
        self.assertEqual(p.total_traded.dollars, Decimal("12.345"))
        self.assertEqual(p.fees_paid.dollars, Decimal("0.07"))
        self.assertEqual(p.side, "yes")

    def test_position_legacy_integer_schema(self):
        p = parse_position({"ticker": "T1", "position": -5,
                            "market_exposure": 144, "fees_paid": 7})
        self.assertEqual(p.quantity.count, Decimal(-5))
        self.assertEqual(p.side, "no")
        self.assertEqual(p.market_exposure.dollars, Decimal("1.44"))
        self.assertEqual(p.fees_paid.dollars, Decimal("0.07"))

    def test_no_silent_zero_when_field_name_changes(self):
        with self.assertRaises(SchemaIncompatible):
            parse_position({"ticker": "T1",
                            "contracts_held_v9": "3"})   # unmapped name

    def test_float_money_is_refused_not_absorbed(self):
        with self.assertRaises(SchemaIncompatible):
            parse_position({"ticker": "T1", "position_fp": 3.0})

    def test_order_fill_settlement_balance_fixtures(self):
        o = parse_order({"order_id": "o1", "ticker": "T1",
                         "status": "resting", "requested_price": 48,
                         "avg_fill_price": "48.5", "count_fp": "2.5",
                         "remaining_count_fp": "1.5",
                         "client_order_id": "alpha_x"})
        self.assertEqual(o.count.count, Decimal("2.5"))
        self.assertEqual(o.remaining_count.count, Decimal("1.5"))
        self.assertEqual(o.avg_fill_price.cents, Decimal("48.5"))
        f = parse_fill({"fill_id": "f1", "order_id": "o1", "count": 2,
                        "yes_price": 48, "fee_cost": "0.035"})
        self.assertEqual(f.fee.dollars, Decimal("0.035"))
        self.assertEqual(f.price.cents, Decimal(48))
        s = parse_settlement({"ticker": "T1", "result": "yes",
                              "revenue_dollars": "2.00",
                              "fees_paid_dollars": "0.04"})
        self.assertEqual(s.revenue.dollars, Decimal("2.00"))
        unknown = parse_settlement({"ticker": "T1"})
        self.assertIsNone(unknown.result)        # UNKNOWN stays UNKNOWN
        b = parse_balance({"balance": 9326})
        self.assertEqual(b.balance.dollars, Decimal("93.26"))
        b2 = parse_balance({"balance_dollars": "93.26"})
        self.assertEqual(b2.balance.dollars, Decimal("93.26"))
        with self.assertRaises(SchemaIncompatible):
            parse_balance({"portfolio_value_v9": "93.26"})


class TestClassification(unittest.TestCase):
    def test_matched_broker_only_local_only_contradicted(self):
        broker = [
            {"ticker": "A", "position": 3, "market_exposure": 144},
            {"ticker": "B", "position_fp": "2",
             "market_exposure_dollars": "0.90"},
            {"ticker": "C", "position": 4, "market_exposure": 200},
        ]
        local = [local_pos("A", count=3), local_pos("C", count=2),
                 local_pos("D", count=1)]
        result = classify_positions(broker, local)
        kinds = {e.get("ticker"): e["classification"]
                 for e in result["entries"]}
        self.assertEqual(kinds["A"], "MATCHED")
        self.assertEqual(kinds["B"], "BROKER_ONLY")
        self.assertEqual(kinds["C"], "CONTRADICTED")
        self.assertEqual(kinds["D"], "LOCAL_ONLY")
        self.assertFalse(result["trading_blocked"])
        self.assertEqual(
            Decimal(result["conservative_broker_only_exposure_dollars"]),
            Decimal("0.90"))

    def test_unknown_schema_blocks_and_counts(self):
        broker = [{"ticker": "A", "weird_field_v9": "3"}]
        result = classify_positions(broker, [local_pos("A")])
        self.assertTrue(result["trading_blocked"])
        self.assertIn("EXCHANGE_SCHEMA_INCOMPATIBLE",
                      result["blocked_reason"])
        self.assertEqual(result["counts"]["UNKNOWN_SCHEMA"], 1)

    def test_broker_only_without_exposure_counted_conservatively(self):
        result = classify_positions(
            [{"ticker": "X", "position_fp": "2"}], [])
        entry = result["entries"][0]
        self.assertEqual(entry["classification"], "BROKER_ONLY")
        self.assertIn("ESTIMATE_CONSERVATIVE", entry["exposure_basis"])
        self.assertEqual(Decimal(entry["exposure_dollars"]),
                         Decimal("1.98"))          # 2 × 99c upper bound


class TestReconcileRegression(unittest.TestCase):
    """The exact reproduced DE-P0-004 scenario, now fail-closed."""

    def _pm(self, broker_rows):
        CFG.DATA_DIR = tempfile.mkdtemp(prefix="kalshi_recon_")

        class C:
            def get_positions(self):
                return broker_rows
        pm = PositionManager(C(), None)
        pm.positions["t1"] = local_pos("KXBTC15M-X-50", count=3)
        return pm

    def test_fixed_point_row_no_longer_invisible_or_destructive(self):
        pm = self._pm([{"ticker": "KXBTC15M-X-50", "position_fp": "3",
                        "market_exposure_dollars": "1.44",
                        "fees_paid_dollars": "0.10"}])
        report = pm.reconcile_with_broker()
        # the broker position is SEEN and matches the local one
        self.assertIn("KXBTC15M-X-50", report["matched"])
        self.assertEqual(report["ghost"], [])
        self.assertIsNotNone(pm.positions.get("t1"))
        self.assertGreater(pm.open_risk(), 0)
        self.assertFalse(pm.exchange_schema_incompatible)

    def test_uninterpretable_row_blocks_and_never_deletes(self):
        pm = self._pm([{"ticker": "KXBTC15M-X-50",
                        "contracts_held_v9": "3"}])
        report = pm.reconcile_with_broker()
        self.assertTrue(report["trading_blocked"])
        self.assertTrue(pm.exchange_schema_incompatible)
        # the local position SURVIVES a parser failure, always
        self.assertIsNotNone(pm.positions.get("t1"))
        self.assertEqual(report["ghost"], [])

    def test_no_invented_50c_cost_basis(self):
        pm = self._pm([{"ticker": "KXNEW-1", "position_fp": "2",
                        "market_exposure_dollars": "0.30"}])
        pm.reconcile_with_broker()
        rebuilt = pm.positions["brk-KXNEW-1-yes"]
        self.assertEqual(rebuilt["avg_price"], 15)   # 0.30$/2 = 15c real
        self.assertEqual(rebuilt["cost_basis_source"],
                         "market_exposure_dollars")

    def test_legacy_rows_behave_as_before(self):
        pm = self._pm([{"ticker": "KXBTC15M-X-50", "position": 3,
                        "market_exposure": 144, "realized_pnl": 0,
                        "fees_paid": 2}])
        report = pm.reconcile_with_broker()
        self.assertIn("KXBTC15M-X-50", report["matched"])
        self.assertFalse(report["trading_blocked"])


if __name__ == "__main__":
    unittest.main()
