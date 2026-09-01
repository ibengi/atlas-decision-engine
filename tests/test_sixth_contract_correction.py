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
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402
import performance  # noqa: E402
import research_export  # noqa: E402
from persistence import PersistenceSentinel  # noqa: E402
from trade_logger import EVENT_LEDGER_CORRECTION, fold_corrections  # noqa: E402
from ledger_corrections import (  # noqa: E402
    ACTIVATION_ENV, SIXTH_CONTRACT_CORRECTION, apply_ledger_corrections)

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
             gross_pnl=-1.35, net_pnl=-1.41,   # net = gross - fees
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
    ACTIVATE = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas_sixth_")
        self._old_env = os.environ.get(ACTIVATION_ENV)
        if self.ACTIVATE:
            os.environ[ACTIVATION_ENV] = CID
        else:
            os.environ.pop(ACTIVATION_ENV, None)
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        PersistenceSentinel.reset()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self._old_env is None:
            os.environ.pop(ACTIVATION_ENV, None)
        else:
            os.environ[ACTIVATION_ENV] = self._old_env
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
        # NOT a trade: typed as a correction, no independent outcome, and
        # attributed to the economic day of the settlement it corrects.
        self.assertEqual(corr["event_type"], EVENT_LEDGER_CORRECTION)
        self.assertIsNone(corr["won"])
        self.assertEqual(corr["settled_at"], "2026-09-01T05:42:31+00:00")
        self.assertTrue(corr["applied_at"])
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


class AccountingSemanticsTest(_Base):
    """A correction fixes economics WITHOUT becoming another trade.

    Counting surfaces (trade count, win/loss, streak, trades_today,
    settlement recency, strategy rows) must be identical before and after;
    economic surfaces (PnL, fees, quantity) must move by exactly the
    broker delta.
    """

    def _metrics(self, tlog):
        client = MagicMock()
        client.get_positions.return_value = []
        pm = bot.PositionManager(client, tlog)
        rm = bot.RiskManager(tlog, pm, capital=100.0)
        st = bot.StatsEngine(tlog).compute()
        snap = rm.snapshot()
        settled = tlog.settled_trades()
        return {
            # -- counting
            "settled_count": len(settled),
            "stats_n": st["n"],
            "wins": sum(1 for t in settled if t["won"]),
            "losses": sum(1 for t in settled if not t["won"]),
            "win_rate": st["win_rate"],
            "consecutive_losses": rm.consecutive_losses(),
            "trades_today": rm.trades_today(),
            "journal_trade_rows": len(tlog.trade_rows()),
            "recency_known": rm.seconds_since_last_settlement() is not None,
            "last_settled_at": settled[-1]["settled_at"] if settled else None,
            # -- economics
            "realized_pnl": round(sum(t["net_pnl"] for t in settled), 4),
            "daily_realized_pnl": round(rm.daily_realized_pnl(), 4),
            "gross_pnl": round(sum(t["gross_pnl"] for t in settled), 4),
            "fees_paid": snap["fees_paid"],
            "drawdown": round(rm.rolling_drawdown(), 4),
            "qty_on_ticker": sum(t["filled_count"] for t in settled
                                 if t["ticker"] == TICKER),
        }

    def test_before_after_semantics(self):
        tlog = self._tlog(production_journal())
        before = self._metrics(tlog)

        self.assertEqual(len(apply_ledger_corrections(tlog)), 1)
        after = self._metrics(tlog)

        # ---- NOTHING that counts trades may move -------------------------
        for key in ("settled_count", "stats_n", "wins", "losses", "win_rate",
                    "consecutive_losses", "trades_today", "journal_trade_rows",
                    "recency_known", "last_settled_at"):
            self.assertEqual(after[key], before[key],
                             f"{key} changed: the correction is being counted "
                             f"as an independent trade")
        self.assertEqual(after["settled_count"], 3)

        # ---- economics move by EXACTLY the broker delta ------------------
        self.assertAlmostEqual(after["realized_pnl"],
                               before["realized_pnl"] + 0.8161, places=6)
        self.assertAlmostEqual(after["daily_realized_pnl"],
                               before["daily_realized_pnl"] + 0.8161, places=6)
        self.assertAlmostEqual(after["gross_pnl"],
                               before["gross_pnl"] + 0.81, places=6)
        self.assertAlmostEqual(after["fees_paid"],
                               round(before["fees_paid"] - 0.0061, 2), places=2)
        self.assertEqual(after["qty_on_ticker"], 6)
        self.assertEqual(before["qty_on_ticker"], 5)
        # drawdown recomputes on the corrected curve (here: still flat)
        self.assertEqual(before["drawdown"], 0.0)
        self.assertEqual(after["drawdown"], 0.0)

    def test_correction_folds_into_its_target_row(self):
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)

        eff = {t["trade_id"]: t for t in tlog.effective_trades()}
        self.assertNotIn(CID, eff, "a correction is never a row of its own")
        target = eff[TRADE_ID]
        self.assertEqual(target["filled_count"], 6)
        self.assertAlmostEqual(target["gross_pnl"], 4.86, places=6)
        self.assertAlmostEqual(target["net_pnl"], 4.8061, places=6)
        self.assertAlmostEqual(target["fees"], 0.0539, places=6)
        self.assertTrue(target["corrected"])
        self.assertEqual(target["correction_ids"], [CID])
        # gross - fees - net reconciles exactly on broker figures
        self.assertAlmostEqual(
            target["gross_pnl"] - target["fees"] - target["net_pnl"],
            0.0, places=6)

    def test_a_correction_never_resets_a_loss_streak(self):
        """The dangerous case: if the correction counted as a fresh win it
        would clear consecutive_losses and unlock the risk brake."""
        rows = production_journal()
        rows[1].update(won=False, gross_pnl=-0.76, net_pnl=-0.82)
        rows[-1].update(result="no", won=False, gross_pnl=-0.95,
                        net_pnl=-1.01)          # target trade is a LOSS too
        tlog = self._tlog(rows)
        client = MagicMock()
        client.get_positions.return_value = []
        rm = bot.RiskManager(tlog, bot.PositionManager(client, tlog), 100.0)
        before = rm.consecutive_losses()
        self.assertEqual(before, 3, "three losses in a row")

        apply_ledger_corrections(tlog)

        # the corrected trade is still a loss (-1.01 + 0.8161 = -0.1939):
        # the streak, and the risk brake it drives, must survive
        self.assertEqual(rm.consecutive_losses(), before,
                         "a ledger correction must not clear the streak")

    def test_settlement_recency_anchor_is_untouched(self):
        """seconds_since_last_settlement drives the half-open cooldown; a
        correction applied days later must not reset that anchor."""
        tlog = self._tlog(production_journal())
        client = MagicMock()
        client.get_positions.return_value = []
        rm = bot.RiskManager(tlog, bot.PositionManager(client, tlog), 100.0)
        before = rm._last_settlement_anchor()

        apply_ledger_corrections(tlog)

        self.assertEqual(rm._last_settlement_anchor(), before)
        self.assertEqual(before, "2026-09-01T05:42:31+00:00")

    def test_export_and_report_surfaces_serve_no_extra_row(self):
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)

        export = research_export.settlements(self.tmp, "", 50)
        ids = [r["trade_id"] for r in export["rows"]]
        self.assertEqual(len(ids), 3)
        self.assertNotIn(CID, ids)
        self.assertEqual(export["discrepancies"], [],
                         "corrected row reconciles gross - fees = net")
        corrected = [r for r in export["rows"] if r["trade_id"] == TRADE_ID][0]
        self.assertAlmostEqual(corrected["net_pnl"], 4.8061, places=6)

        rep = performance.load_report(self.tmp, capital=100.0)
        self.assertEqual(rep["trades_executed"], 3)
        self.assertEqual(rep["open_trades"], 0)

    def test_fold_ignores_an_orphan_correction(self):
        """A correction whose target is absent creates no phantom PnL."""
        rows = production_journal()
        tlog = self._tlog(rows)
        apply_ledger_corrections(tlog)
        orphaned = [t for t in tlog.trades
                    if t["trade_id"] != TRADE_ID]      # drop the target

        folded = fold_corrections(orphaned)
        self.assertEqual(len(folded), 2)
        self.assertAlmostEqual(sum(t["net_pnl"] for t in folded), -1.23,
                               places=6)


class ExplicitActivationTest(_Base):
    """Deploying the code must never mutate the ledger on its own."""

    ACTIVATE = False

    def test_no_correction_without_operator_activation(self):
        tlog = self._tlog(production_journal())
        before = copy.deepcopy(tlog.trades)

        self.assertEqual(apply_ledger_corrections(tlog), [])

        self.assertEqual(tlog.trades, before)
        self.assertEqual(self._rows_for(tlog), [])

    def test_wrong_id_does_not_activate(self):
        os.environ[ACTIVATION_ENV] = "corr-some-other-incident"
        tlog = self._tlog(production_journal())

        self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(self._rows_for(tlog), [])

    def test_activation_is_exact_and_applies_once(self):
        os.environ[ACTIVATION_ENV] = f"other-id, {CID} "
        tlog = self._tlog(production_journal())

        self.assertEqual(len(apply_ledger_corrections(tlog)), 1)
        self.assertEqual(apply_ledger_corrections(tlog), [])

    def test_activation_var_can_be_removed_after_application(self):
        os.environ[ACTIVATION_ENV] = CID
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)
        del tlog
        os.environ.pop(ACTIVATION_ENV)        # operator cleans the env

        tlog2 = bot.TradeLogger()             # restart, no activation
        self.assertEqual(len(self._rows_for(tlog2)), 1,
                         "the applied correction persists")
        self.assertEqual(apply_ledger_corrections(tlog2), [])


class ConcurrentApplicationTest(_Base):
    """SINGLE_REPLICA_REQUIRED is enforced outside; here we prove the
    engine DETECTS a violation and fails closed instead of silently
    double-correcting."""

    def test_concurrent_replica_write_is_detected_and_halts(self):
        tlog = self._tlog(production_journal())
        # another replica applied it and flushed to the same file while our
        # journal stayed in memory
        other = bot.TradeLogger()
        apply_ledger_corrections(other)
        self.assertEqual(len(self._rows_for(other)), 1)

        applied = apply_ledger_corrections(tlog)

        self.assertEqual(applied, [], "no second economic correction")
        self.assertFalse(PersistenceSentinel.healthy(),
                         "submissions must be blocked on a replica breach")
        self.assertIn("ledger_corrections",
                      str(PersistenceSentinel.failure()))

    def test_disk_holds_exactly_one_correction_after_apply(self):
        tlog = self._tlog(production_journal())
        apply_ledger_corrections(tlog)

        on_disk = bot.TradeLogger()
        self.assertEqual(len(self._rows_for(on_disk)), 1)
        self.assertTrue(PersistenceSentinel.healthy())


class IdempotencyAndRestartTest(_Base):

    def test_second_run_applies_nothing(self):
        tlog = self._tlog(production_journal())
        self.assertEqual(len(apply_ledger_corrections(tlog)), 1)
        n = len(tlog.trades)

        for _ in range(3):
            self.assertEqual(apply_ledger_corrections(tlog), [])
        self.assertEqual(len(tlog.trades), n)
        self.assertEqual(len(self._rows_for(tlog)), 1)

    def test_original_row_is_byte_identical_on_disk(self):
        """The corrected trade row must come back from disk with the same
        bytes it had before the correction was applied."""
        tlog = self._tlog(production_journal())
        raw_before = json.load(open(tlog.path, encoding="utf-8"))
        target_before = [r for r in raw_before if r["trade_id"] == TRADE_ID][0]
        digest_before = hashlib.sha256(
            json.dumps(target_before, sort_keys=True,
                       ensure_ascii=False).encode()).hexdigest()

        apply_ledger_corrections(tlog)

        raw_after = json.load(open(tlog.path, encoding="utf-8"))
        target_after = [r for r in raw_after if r["trade_id"] == TRADE_ID][0]
        digest_after = hashlib.sha256(
            json.dumps(target_after, sort_keys=True,
                       ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(digest_after, digest_before)
        self.assertEqual(target_after, target_before)
        # and every other historical row too
        self.assertEqual(raw_after[:len(raw_before)], raw_before)

    def test_double_initialization_in_one_process(self):
        """Two TradeLoggers built in sequence (double init) must not yield
        two economic corrections."""
        tlog = self._tlog(production_journal())
        self.assertEqual(len(apply_ledger_corrections(tlog)), 1)

        tlog2 = bot.TradeLogger()          # second init, same disk
        self.assertEqual(apply_ledger_corrections(tlog2), [])
        self.assertEqual(len(self._rows_for(tlog2)), 1)
        self.assertTrue(PersistenceSentinel.healthy())

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
