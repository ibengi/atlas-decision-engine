# -*- coding: utf-8 -*-
"""Regression: broker positions carry quantities as `position_fp` strings.

Incident 2026-08-31 18:47:59Z (deployment 1b650679, first restored boot of
the Phase-2B cutover): the DEMO /portfolio/positions payload exposes the
signed quantity ONLY as `position_fp` ("-6.00" fixed-point string). Both
broker-quantity parsers read `position`/`quantity`/`count`, got 0 for
every entry, concluded the broker was flat, and the STARTUP reconciliation
destructively removed all three freshly restored positions as ghosts
("Ghost cleanup: removed 3 stale position(s)") while the same log line
showed the broker holding them. The periodic verifier would then have
reported a false MATCH (empty vs empty).

These tests replay the REAL captured payload shape against both parsers.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from position_manager import PositionManager  # noqa: E402

# Field-for-field shape of the captured [RAW:positions] market_positions
# entries (deployment 1b650679, 2026-08-31T18:47:59.662Z).
REAL_BROKER_PAYLOAD = [
    {"exchange_index": 2, "fees_paid_dollars": "0.053900",
     "last_updated_ts": "2026-08-28T11:34:21.249101Z",
     "market_exposure_dollars": "1.140000", "position_fp": "-6.00",
     "realized_pnl_dollars": "0.000000",
     "ticker": "KXBTCD-26AUG2808-T79599.99",
     "total_traded_dollars": "1.140000"},
    {"exchange_index": 2, "fees_paid_dollars": "0.011200",
     "last_updated_ts": "2026-08-28T11:18:25.000000Z",
     "market_exposure_dollars": "0.800000", "position_fp": "-1.00",
     "realized_pnl_dollars": "0.000000",
     "ticker": "KXBTCD-26AUG2808-T79799.99",
     "total_traded_dollars": "0.800000"},
    {"exchange_index": 2, "fees_paid_dollars": "0.089700",
     "last_updated_ts": "2026-08-28T01:43:19.000000Z",
     "market_exposure_dollars": "1.320000", "position_fp": "44.00",
     "realized_pnl_dollars": "0.000000",
     "ticker": "KXBTCD-26AUG2817-T84999.99",
     "total_traded_dollars": "1.320000"},
]

LOCAL_POSITIONS = {
    "393ba1539d39": {"trade_id": "393ba1539d39",
                     "ticker": "KXBTCD-26AUG2817-T84999.99", "side": "yes",
                     "count_initial": 44, "count": 44, "avg_price": 3,
                     "fees": 0.09, "opened_at": "2026-08-28T01:43:19+00:00",
                     "order_ids": [], "fill_ids": [], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
    "8f8cc89e2847": {"trade_id": "8f8cc89e2847",
                     "ticker": "KXBTCD-26AUG2808-T79799.99", "side": "no",
                     "count_initial": 1, "count": 1, "avg_price": 80,
                     "fees": 0.02, "opened_at": "2026-08-28T11:18:25+00:00",
                     "order_ids": [], "fill_ids": [], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
    "c91a43f56c9c": {"trade_id": "c91a43f56c9c",
                     "ticker": "KXBTCD-26AUG2808-T79599.99", "side": "no",
                     "count_initial": 5, "count": 5, "avg_price": 19,
                     "fees": 0.06, "opened_at": "2026-08-28T11:32:11+00:00",
                     "order_ids": [], "fill_ids": [], "state": "open",
                     "strategy": "btc_daily_above_strike",
                     "category": "Crypto", "market_score": None,
                     "entry_edge": None, "entry_ev": None},
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_payload_")
        self._saved_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        self.addCleanup(self._cleanup)
        self.client = MagicMock()
        self.client.get_positions.return_value = [dict(e) for e in
                                                  REAL_BROKER_PAYLOAD]
        self.pm = PositionManager(self.client, MagicMock())
        self.pm.positions = {k: dict(v) for k, v in LOCAL_POSITIONS.items()}

    def _cleanup(self):
        bot.CFG.DATA_DIR = self._saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)


class StartupReconcileRealPayloadTest(_Base):

    def test_real_payload_classifies_the_documented_quantity_mismatch(self):
        """Mission C: -6 broker vs -5 local on T79599.99, everything else
        matched, nothing deleted, nothing adopted, halt armed."""
        report = self.pm.reconcile_with_broker()
        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual(report["mismatches"],
                         [{"ticker": "KXBTCD-26AUG2808-T79599.99",
                           "kind": "quantity_mismatch",
                           "broker": -6, "local": -5}])
        self.assertEqual(sorted(report["matched"]),
                         ["KXBTCD-26AUG2808-T79799.99",
                          "KXBTCD-26AUG2817-T84999.99"])
        self.assertIsNotNone(self.pm.reconcile_halt)
        self.assertEqual(self.pm.open_count(), 3,
                         "the 2026-08-31 incident: all three restored "
                         "positions were destroyed on a parseable payload")

    def test_false_flat_regression_broker_count_is_three_not_zero(self):
        net, err = self.pm._broker_net_positions(
            [dict(e) for e in REAL_BROKER_PAYLOAD])
        self.assertIsNone(err)
        self.assertEqual(len(net), 3,
                         "position_fp rows must parse: broker_count=3, "
                         "never the incident's broker_count=0")
        self.assertEqual(net["KXBTCD-26AUG2808-T79599.99"], -6)

    def test_incident_regression_nothing_removed_nothing_rebuilt(self):
        before = {k: dict(v) for k, v in self.pm.positions.items()}
        self.pm.reconcile_with_broker()
        self.assertEqual(self.pm.positions, before)
        self.assertEqual(self.client.create_order.call_count, 0)
        self.assertEqual(self.client.cancel_order.call_count, 0)


class PeriodicVerifyRealPayloadTest(_Base):

    def test_broker_only_mismatch_detected_when_local_empty(self):
        # The engine's actual post-incident state: local wiped, broker
        # holding three positions. The verifier must halt, never MATCH.
        self.pm.positions = {}
        report = self.pm.verify_against_broker()
        self.assertEqual(report["status"], "MISMATCH")
        kinds = {m["kind"] for m in report["mismatches"]}
        self.assertEqual(kinds, {"broker_only"})
        self.assertIsNotNone(self.pm.reconcile_halt)

    def test_quantity_mismatch_detected_with_golden_local(self):
        # Golden local says 5 'no' on T79599.99; broker says 6 (the 6th
        # contract filled after the failed cancel). Must be a documented
        # quantity_mismatch, not a false MATCH.
        report = self.pm.verify_against_broker()
        self.assertEqual(report["status"], "MISMATCH")
        mm = {m["ticker"]: m for m in report["mismatches"]}
        self.assertEqual(list(mm), ["KXBTCD-26AUG2808-T79599.99"])
        self.assertEqual(mm["KXBTCD-26AUG2808-T79599.99"]["kind"],
                         "quantity_mismatch")
        self.assertEqual(mm["KXBTCD-26AUG2808-T79599.99"]["broker"], -6)
        self.assertEqual(mm["KXBTCD-26AUG2808-T79599.99"]["local"], -5)

    def test_exact_net_match_clears(self):
        self.pm.positions["c91a43f56c9c"]["count"] = 6   # local caught up
        self.pm.reconcile_halt = {"status": "MISMATCH", "detail": "old",
                                  "at": "t"}
        report = self.pm.verify_against_broker()
        self.assertEqual(report["status"], "MATCH")
        self.assertIsNone(self.pm.reconcile_halt)


if __name__ == "__main__":
    unittest.main()
