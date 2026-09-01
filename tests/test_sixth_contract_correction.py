# -*- coding: utf-8 -*-
"""Sixth-contract broker-authoritative ledger remediation (order 01a04823).

Incident pinned here (2026-08-28, KXBTCD-26AUG2808-T79599.99):

  local  : 5 no-contracts @ 19c recorded (5 taker fills seen at submit);
           cancel of the resting 6th failed (DELETE -> HTTP 404) at
           11:32:11Z and the engine stopped watching the order
  broker : the resting remainder filled as a MAKER fill at 11:34:21Z --
           2m10s AFTER the cancel failure -- so the true fill count is 6
           (taker_fill_cost 0.95$ = 5 x 19c + maker_fill_cost 0.19$ =
           1 x 19c, fees 0.0539$ total, maker share 0.0000$)
  result : market settled "no"; broker paid 6.00$, the ledger accounted
           5.00$ (gross 4.05$, net 3.99$, fees 0.06$)

Root cause: FILL_ARRIVED_AFTER_CANCEL_FAILURE (proven by broker
get_order last_update_time vs local ORDER_CANCEL_FAILED timestamp).

The remediation is an APPEND-ONLY corrective ledger event:

  - never rewrites or deletes the historical journal rows
  - corrects quantity (+1) and PnL (gross +0.81$, fees -0.0061$,
    net +0.8161$) exactly once, keyed by broker order/fill identifiers
  - idempotent (a correction_id marker row IS the applied-flag) and
    restart-safe (the marker lives in the journal itself, atomic flush)
  - creates NO open position and performs ZERO broker writes
  - risk history follows automatically: every risk metric is recomputed
    from the journal (single source of truth)
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402
from ledger_corrections import (  # noqa: E402
    SIXTH_CONTRACT_CORRECTION, apply_ledger_corrections)

TICKER = "KXBTCD-26AUG2808-T79599.99"
ORDER_ID = "01a04823-8ec8-74c2-82de-f7ecd236fc01"
TRADE_ID = "c91a43f56c9c"
CID = SIXTH_CONTRACT_CORRECTION["correction_id"]


def production_journal():
    """Field-for-field mirror of the live state5 journal after the
    2026-09-01 settlements: the target trade settled on 5/6 contracts,
    plus two other settled trades from the same batch."""
    base = {
        "schema": "v11", "decision_id": None, "spread": 2, "edge": None,
        "ev": None, "confidence": None, "grade": None, "analysis": None,
        "reason": "test", "order_status": "executed", "roi": None,
        "holding_seconds": None, "market": "BTC daily",
    }
    return [
        dict(base, trade_id="aaaa11112222", timestamp="2026-08-28T11:30:00+00:00",
             ticker="KXBTCD-26AUG2808-T80249.99", side="yes",
             requested_price=47, avg_fill_price=47, requested_count=3,
             filled_count=3, fees=0.06, order_id="ord-other-1",
             state="settled", result="no", won=False,
             gross_pnl=-1.41, net_pnl=-1.41,
             settled_at="2026-09-01T05:42:31+00:00"),
        dict(base, trade_id="bbbb33334444", timestamp="2026-08-28T11:31:00+00:00",
             ticker="KXBTCD-26AUG2808-T78949.99", side="no",
             requested_price=76, avg_fill_price=76, requested_count=1,
             filled_count=1, fees=0.06, order_id="ord-other-2",
             state="settled", result="no", won=True,
             gross_pnl=0.24, net_pnl=0.18,
             settled_at="2026-09-01T05:42:31+00:00"),
        dict(base, trade_id=TRADE_ID, decision_id="9512fbe0e7-0f1af560",
             timestamp="2026-08-28T11:32:11+00:00", ticker=TICKER,
             side="no", requested_price=19, avg_fill_price=19,
             requested_count=6, filled_count=5, fees=0.06,
             order_id=ORDER_ID, order_status="unknown_cancel_failed",
             state="settled", result="no", won=True,
             gross_pnl=4.05, net_pnl=3.99,
             settled_at="2026-09-01T05:42:31+00:00"),
    ]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_sixth_")
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        PersistenceSentinel.reset()
        bot.CFG.DATA_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tlog(self, rows):
        tlog = bot.TradeLogger()
        tlog.trades = copy.deepcopy(rows)
        tlog.flush()
        return tlog

    @staticmethod
    def _rows_for(tlog, cid=CID):
        return [t for t in tlog.trades if t.get("correction_id") == cid]


class SixthContractRegressionTest(_Base):
    """The exact incident: local=5, broker=6, market settled 'no'."""

    def test_exactly_one_corrective_event_with_broker_truth(self):
        tlog = self._tlog(production_journal())
        client = MagicMock()   # present only to prove zero broker writes
        before = copy.deepcopy(tlog.trades)

        applied = apply_ledger_corrections(tlog)

        self.assertEqual(len(applied), 1)
        self.assertEqual(len(tlog.trades), len(before) + 1)
        # every historical row is byte-identical: append-only, no rewrite
        self.assertEqual(tlog.trades[:len(before)], before)

        corr = self._rows_for(tlog)[0]
        self.assertIs(tlog.trades[-1], corr)
        self.assertEqual(corr["schema"], tlog.SCHEMA,
                         "corrective row must survive the legacy-schema purge")
        self.assertTrue(corr["correction"])
        self.assertEqual(corr["corrects_trade_id"], TRADE_ID)
        self.assertEqual(corr["order_id"], ORDER_ID)
        self.assertEqual(corr["ticker"], TICKER)
        self.assertEqual(corr["side"], "no")
        # the missing MAKER fill: quantity +1 @ 19c
        self.assertEqual(corr["filled_count"], 1)
        self.assertEqual(corr["avg_fill_price"], 19)
        # PnL exactly once: payout 1.00 - entry 0.19 = gross 0.81;
        # fee correction 0.0539 (broker actual) - 0.06 (recorded) = -0.0061
        self.assertAlmostEqual(corr["gross_pnl"], 0.81, places=6)
        self.assertAlmostEqual(corr["fees"], -0.0061, places=6)
        self.assertAlmostEqual(corr["net_pnl"], 0.8161, places=6)
        self.assertEqual(corr["state"], "settled")
        self.assertEqual(corr["result"], "no")
        self.assertTrue(corr["won"])
        # broker identifiers ride with the event for the audit trail
        ev = corr["broker_evidence"]
        self.assertEqual(ev["fill_count"], 6)
        self.assertEqual(ev["status"], "executed")
        self.assertEqual(ev["last_update_time"], "2026-08-28T11:34:21.249018Z")
        self.assertEqual(ev["root_cause"], "FILL_ARRIVED_AFTER_CANCEL_FAILURE")
        # ZERO broker writes -- the module never even sees a client
        self.assertEqual(client.method_calls, [])

    def test_ledger_totals_reconcile_to_broker_basis(self):
        """After the correction this market's ledger equals the broker's
        economics: 6 x (1.00 - 0.19) - 0.0539 = 4.8061$."""
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)

        rows = [t for t in tlog.trades if t["ticker"] == TICKER]
        self.assertAlmostEqual(sum(t["net_pnl"] for t in rows), 4.8061, places=6)
        self.assertAlmostEqual(sum(t["gross_pnl"] for t in rows), 4.86, places=6)
        self.assertAlmostEqual(sum(t["fees"] for t in rows), 0.0539, places=6)
        self.assertEqual(sum(t["filled_count"] for t in rows), 6)

    def test_no_open_position_and_no_broker_write(self):
        tlog = self._tlog(production_journal())
        client = MagicMock()
        client.get_positions.return_value = []
        pm = bot.PositionManager(client, tlog)
        self.assertEqual(pm.open_count(), 0)

        apply_ledger_corrections(tlog)

        self.assertEqual(pm.open_count(), 0, "correction opens NO position")
        self.assertEqual(pm.positions, {})
        self.assertEqual(client.create_order.call_count, 0)
        self.assertEqual(client.cancel_order.call_count, 0)
        corr = self._rows_for(tlog)[0]
        self.assertEqual(corr["state"], "settled",
                         "row is born settled: nothing to release later")
        self.assertFalse(tlog.has_open_on(TICKER))

    def test_risk_history_follows_the_corrected_journal(self):
        """Risk metrics recompute from the journal; the corrective event
        must flow through them like any settled win."""
        tlog = self._tlog(production_journal())
        client = MagicMock()
        client.get_positions.return_value = []
        pm = bot.PositionManager(client, tlog)
        rm = bot.RiskManager(tlog, pm, capital=100.0)
        base_total = sum(t["net_pnl"] for t in tlog.settled_trades())

        apply_ledger_corrections(tlog)

        self.assertAlmostEqual(
            sum(t["net_pnl"] for t in tlog.settled_trades()),
            base_total + 0.8161, places=6)
        snap = rm.snapshot()
        self.assertAlmostEqual(snap["realized_pnl"],
                               round(base_total + 0.8161, 2), places=2)
        self.assertAlmostEqual(snap["fees_paid"], 0.17, places=2)  # 0.18-0.0061
        self.assertEqual(rm.consecutive_losses(), 0)


class IdempotencyAndRestartTest(_Base):

    def test_second_run_applies_nothing(self):
        tlog = self._tlog(production_journal())
        self.assertEqual(len(apply_ledger_corrections(tlog)), 1)
        n = len(tlog.trades)

        for _ in range(3):
            self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(len(tlog.trades), n)
        self.assertEqual(len(self._rows_for(tlog)), 1)

    def test_restart_safe_marker_lives_in_the_journal(self):
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)
        del tlog

        # process restart: reload the SAME disk
        tlog2 = bot.TradeLogger()
        self.assertEqual(len(self._rows_for(tlog2)), 1,
                         "corrective event survived the restart")
        self.assertEqual(apply_ledger_corrections(tlog2), [],
                         "restart does not duplicate the correction")
        self.assertEqual(len(self._rows_for(tlog2)), 1)

    def test_crash_before_flush_reapplies_cleanly(self):
        """If the process dies between append and a failed flush, the disk
        still holds the pre-correction journal; the next boot applies it."""
        tlog = self._tlog(production_journal())
        real_flush = tlog.flush
        tlog.flush = MagicMock()          # flush lost: simulated crash
        apply_ledger_corrections(tlog)
        tlog.flush = real_flush
        del tlog

        tlog2 = bot.TradeLogger()
        self.assertEqual(self._rows_for(tlog2), [], "nothing persisted")
        self.assertEqual(len(apply_ledger_corrections(tlog2)), 1)
        del tlog2
        tlog3 = bot.TradeLogger()
        self.assertEqual(len(self._rows_for(tlog3)), 1)


class PreconditionGuardTest(_Base):
    """The correction is surgical: any ledger that is not the exact
    incident ledger gets ZERO writes."""

    def test_foreign_ledger_is_untouched(self):
        rows = [r for r in production_journal() if r["trade_id"] != TRADE_ID]
        tlog = self._tlog(rows)
        before = copy.deepcopy(tlog.trades)

        self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(tlog.trades, before)

    def test_target_still_open_waits(self):
        """PnL math assumes the original trade already settled; an open
        target defers the correction instead of guessing."""
        rows = production_journal()
        rows[-1].update(state="open", result=None, won=None,
                        gross_pnl=None, net_pnl=None, settled_at=None)
        tlog = self._tlog(rows)

        self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(self._rows_for(tlog), [])

    def test_unexpected_quantity_refuses(self):
        """A target row already showing 6 fills means someone corrected it
        another way: adding +1 again would double-count."""
        rows = production_journal()
        rows[-1]["filled_count"] = 6
        tlog = self._tlog(rows)

        self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(self._rows_for(tlog), [])

    def test_unexpected_result_refuses(self):
        """gross +0.81 is only true for a 'no' settlement."""
        rows = production_journal()
        rows[-1].update(result="yes", won=False)
        tlog = self._tlog(rows)

        self.assertEqual(apply_ledger_corrections(tlog), [])

    def test_empty_journal_is_a_noop(self):
        tlog = self._tlog([])
        self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(tlog.trades, [])


if __name__ == "__main__":
    unittest.main()
