# -*- coding: utf-8 -*-
"""Astra's two defects on df85507, reproduced with the REAL journal, position,
order and ledger classes, plus the adversarial restore and rebase neighbours.

Defect 1: a restored/older/replaced journal must never erase a previously
evidenced loss or improve provenance. Defect 2: a rebase must be refused
whenever any live broker order exists, judged on the authoritative order
state, with no ledger mutation and the token left unconsumed.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import equity_ledger as EL                                        # noqa: E402
import execution_engine                                           # noqa: E402
import test_shadow_write_layer_isolation as shadow_iso            # noqa: E402
from config import CFG, _p                                        # noqa: E402
from equity_ledger import EquityLedger                            # noqa: E402
from kalshi_client import KalshiAPIError                          # noqa: E402
from order_manager import OrderManager                            # noqa: E402
from persistence import JsonStore                                 # noqa: E402
from position_manager import PositionManager                      # noqa: E402
from trade_logger import TradeLogger                              # noqa: E402

PRE_AT = "2026-09-07T18:01:19Z"
OK_CTX = {"drawdown_firing": True, "reconcile_status": "MATCH",
          "open_positions": 0, "in_flight_orders": 0}


class _Client:
    """Minimal broker double for the read-only queries the classes make."""
    env = "prod"

    def __init__(self, orders=(), positions=(), orders_error=None):
        self.orders, self.positions, self.orders_error = list(orders), list(positions), orders_error

    def list_orders(self, **kw):
        if self.orders_error:
            raise self.orders_error
        return list(self.orders)

    def get_positions(self):
        return list(self.positions)

    def get_positions_proof(self, **_kw):
        """The completeness contract the real client now answers (A02)."""
        return {"rows": list(self.positions), "complete": True, "pages": 1,
                "cursors": []}


def _trade(tlog, ticker="KXBTC15M-X", count=6, price=50):
    return tlog.open_trade(ticker=ticker, market_title="m", side="yes", req_price=price,
                           avg_price=price, req_count=count, filled_count=count, spread=1,
                           fees=0.0, edge=0.1, ev=0.1, confidence=8, grade="A", reason="r",
                           analysis={}, order_id="o-" + ticker, order_status="executed")


class _Real(shadow_iso._IsolatedState, unittest.TestCase):
    """Real TradeLogger / PositionManager / OrderManager / EquityLedger on an
    isolated DATA_DIR."""

    def stack(self, client=None):
        client = client or _Client()
        tlog = TradeLogger()
        pos = PositionManager(client, tlog)
        return client, tlog, pos

    def reconciled_ledger(self, tlog, pos, env="prod"):
        led = EquityLedger(tlog, pos, env=env)
        prop = led.propose_seed(10.0, PRE_AT, "x", 10.0)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        att = led.propose_attestation("OPS-A", "a" * 64)
        self.assertTrue(led.apply_attestation("OPS-A", "a" * 64, att["token"]))
        self.assertEqual(led.derive_status(), EL.STATUS_RECONCILED)
        return led

    def lose(self, tlog, led, pnl=-3.0, cash=7.0, start=1):
        t = _trade(tlog)
        tlog.settle_trade(t["trade_id"], "no", False, pnl, pnl)
        for i in range(3):
            led.observe(cash, cycle_n=start + i, quiet=True)
        return t

    def file(self):
        return JsonStore.load(_p(EL.LEDGER_FILE), {})

    def reload(self, client=None):
        client, tlog, pos = self.stack(client)
        return EquityLedger(tlog, pos, env="prod"), tlog, pos


# --------------------------------------------------------------------------
# Defect 1
# --------------------------------------------------------------------------

class ARestoredJournalCannotEraseAnEvidencedLoss(_Real):

    def test_astra_defect_1_exact(self):
        """Seed 10 (attested RECONCILED), lose 3 through the real journal,
        restore the pre-loss journal, restart. df85507 came back RECONCILED
        with equity 10 and capital_eligible=True."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        self.assertAlmostEqual(led.drawdown_pct(), 30.0, places=6)
        hwm = led.risk_equity_reference()
        JsonStore.save(_p(CFG.TRADES_FILE), [])             # the older journal
        led2, tlog2, pos2 = self.reload()
        led2.observe(7.0, cycle_n=10, quiet=True)
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertIn(EL.GUARD_UNRECONCILED, led2.guards())
        self.assertFalse(led2.capital_eligible())
        self.assertAlmostEqual(led2.risk_equity_reference(), hwm, places=9)   # monotone
        self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-9)             # loss kept
        self.assertLessEqual(led2.strategy_equity_conservative(), 7.0 + 1e-9)
        f = self.file()
        self.assertEqual(f["risk_equity_status"], EL.STATUS_UNRECONCILED)
        self.assertTrue(f.get("journal_mismatch"))
        self.assertTrue(any("journal" in u for u in f["status_basis"]["unproven"]))

    def test_journal_shorter_than_previously_evidenced(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        t2 = _trade(tlog, ticker="KXBTC15M-Z")
        tlog.settle_trade(t2["trade_id"], "yes", True, 0.5, 0.5)
        for i in range(3):
            led.observe(7.5, cycle_n=20 + i, quiet=True)
        rows = JsonStore.load(_p(CFG.TRADES_FILE), [])
        JsonStore.save(_p(CFG.TRADES_FILE), rows[:1])       # drop the last settled row
        led2, _, _ = self.reload()
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertFalse(led2.capital_eligible())

    def test_journal_replaced_with_same_length_different_history(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)                                  # one settled loss
        rows = JsonStore.load(_p(CFG.TRADES_FILE), [])
        rows[0]["trade_id"] = "someone-else"
        rows[0]["net_pnl"] = 3.0                              # the loss became a win
        rows[0]["gross_pnl"] = 3.0
        rows[0]["won"] = True
        JsonStore.save(_p(CFG.TRADES_FILE), rows)
        led2, _, _ = self.reload()
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertGreaterEqual(led2.drawdown_pct(), 30.0 - 1e-9)   # not improved
        self.assertFalse(led2.capital_eligible())

    def test_persisted_hwm_greater_than_recomputed_is_kept(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        f = self.file()
        f["hwm"]["risk_equity_reference"] = 12.0
        JsonStore.save(_p(EL.LEDGER_FILE), f)
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.risk_equity_reference(), 12.0, places=9)

    def test_persisted_accounting_evidence_newer_than_journal(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        wm = self.file()["journal_watermark"]
        self.assertEqual(wm["settled_count"], 1)
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        # the watermark is NOT lowered to the shorter journal
        self.assertEqual(self.file()["journal_watermark"]["settled_count"], 1)

    def test_restart_after_bad_restore_stays_unreconciled(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        for _ in range(3):                                    # three restarts
            led2, _, _ = self.reload()
            led2.observe(7.0, cycle_n=1, quiet=True)
            self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
            self.assertFalse(led2.capital_eligible())

    def test_restore_followed_by_reconciliation_does_not_lift_the_status(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        for i in range(8):                                    # many quiet cycles at any cash
            led2.observe(10.0 if i % 2 else 7.0, cycle_n=i + 1, quiet=True)
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertFalse(led2.capital_eligible())
        self.assertAlmostEqual(led2.risk_equity_reference(), 10.0, places=9)

    def test_restore_followed_by_attestation_is_refused(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        before = json.dumps(self.file(), sort_keys=True)
        att = led2.propose_attestation("OPS-B", "b" * 64)
        self.assertFalse(led2.apply_attestation("OPS-B", "b" * 64, att["token"]))
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertEqual(json.dumps(self.file(), sort_keys=True), before)

    def test_restore_followed_by_attempted_rebase_is_refused(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        hwm = led2.risk_equity_reference()
        tokens = list(led2.state["consumed_tokens"])
        rb = led2.propose_rebase("r", "OPS-C")
        self.assertFalse(led2.apply_rebase("r", "OPS-C", rb["token"], OK_CTX))
        self.assertAlmostEqual(led2.risk_equity_reference(), hwm, places=9)
        self.assertEqual(led2.state["consumed_tokens"], tokens)

    def test_evidence_returning_restores_the_prior_status(self):
        """Positive control: the mismatch is about evidence, not punishment.
        Putting the correct journal back clears it."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        good = JsonStore.load(_p(CFG.TRADES_FILE), [])
        JsonStore.save(_p(CFG.TRADES_FILE), [])
        led2, _, _ = self.reload()
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        JsonStore.save(_p(CFG.TRADES_FILE), good)
        led3, _, _ = self.reload()
        self.assertEqual(led3.derive_status(), EL.STATUS_RECONCILED)
        self.assertAlmostEqual(led3.drawdown_pct(), 30.0, places=6)

    def test_a_longer_journal_that_keeps_the_evidenced_prefix_is_fine(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        led2, tlog2, _ = self.reload()
        t = _trade(tlog2, ticker="KXBTC15M-W")
        tlog2.settle_trade(t["trade_id"], "yes", True, 1.0, 1.0)
        led2.observe(8.0, cycle_n=1, quiet=True)
        self.assertEqual(led2.derive_status(), EL.STATUS_RECONCILED)
        self.assertAlmostEqual(led2.strategy_equity(), 8.0, places=9)


# --------------------------------------------------------------------------
# Defect 2
# --------------------------------------------------------------------------

class ARebaseIsRefusedWhileAnyOrderIsLive(_Real):

    def blown(self, client=None):
        client, tlog, pos = self.stack(client)
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        om = OrderManager(client)
        risk = type("R", (), {"rolling_drawdown_pct": lambda self: 30.0})()
        return client, tlog, pos, om, led, risk

    def ctx(self, client, om, pos, risk, led=None):
        return execution_engine.equity_rebase_context(client, om, pos, risk,
                                                      equity=led, quiescent=True)

    def assert_refused(self, led, ctx, expect_reason):
        hwm = led.risk_equity_reference()
        before = json.dumps(self.file(), sort_keys=True)
        tokens = list(led.state["consumed_tokens"])
        rb = led.propose_rebase("losses acknowledged", "OPS-R")
        with self.assertLogs("EQUITY", level="WARNING") as cm:
            self.assertFalse(led.apply_rebase("losses acknowledged", "OPS-R", rb["token"], ctx))
        self.assertTrue(any("[EQUITY_REBASE] refused" in m and expect_reason in m
                            for m in cm.output), cm.output)
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)
        self.assertEqual(led.state["consumed_tokens"], tokens)
        self.assertIsNone(led.state["capital_hold"])
        self.assertEqual(json.dumps(self.file(), sort_keys=True), before)

    def test_astra_defect_2_exact_open_order_in_orders_state(self):
        client, tlog, pos, om, led, risk = self.blown(
            _Client(orders=[{"order_id": "ord-1", "ticker": "KXBTC15M-X",
                             "status": "resting", "remaining_count": 1}]))
        om.open_orders["ord-1"] = {"ticker": "KXBTC15M-X", "side": "no", "count": 1,
                                   "price": 9, "placed_at": "now"}
        om.flush()
        om2 = OrderManager(client)                            # the persisted truth
        self.assertIn("ord-1", om2.open_orders)
        self.assert_refused(led, self.ctx(client, om2, pos, risk), "open")

    def test_pending_submit_intent(self):
        client, tlog, pos, om, led, risk = self.blown()
        om._record_intent("KXBTC15M-X", "cid-1", 1, 9)
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "pending")

    def test_partial_fill_still_active(self):
        client = _Client(orders=[{"order_id": "ord-2", "status": "resting",
                                  "fill_count": 3, "remaining_count": 3}])
        client, tlog, pos, om, led, risk = self.blown(client)
        om.open_orders["ord-2"] = {"ticker": "KXBTC15M-X", "side": "no", "count": 6,
                                   "price": 9, "placed_at": "now", "known_filled": 3}
        om.flush()
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "open")

    def test_cancel_requested_but_not_confirmed(self):
        client = _Client(orders=[{"order_id": "ord-3", "status": "resting", "remaining_count": 1}])
        client, tlog, pos, om, led, risk = self.blown(client)
        om.open_orders["ord-3"] = {"ticker": "KXBTC15M-X", "side": "no", "count": 1,
                                   "price": 9, "placed_at": "now",
                                   "state": "unknown_cancel_failed"}
        om.flush()
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "open")

    def test_stale_locally_closed_order_the_broker_still_holds(self):
        client = _Client(orders=[{"order_id": "ord-4", "status": "resting", "remaining_count": 1}])
        client, tlog, pos, om, led, risk = self.blown(client)
        self.assertEqual(om.open_orders, {})                  # local thinks: nothing open
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "broker")

    def test_broker_order_state_unknown_is_a_refusal(self):
        client = _Client(orders_error=KalshiAPIError(0, "listing failed"))
        client, tlog, pos, om, led, risk = self.blown(client)
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "unknown")

    def test_ambiguous_resolution_halt(self):
        client, tlog, pos, om, led, risk = self.blown()
        om.resolution_halt = {"status": "ambiguous", "detail": "x"}
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "ambiguous")

    def test_open_position(self):
        client, tlog, pos, om, led, risk = self.blown()
        t = _trade(tlog, ticker="KXBTC15M-P")
        pos.open_position(t)
        self.assertEqual(pos.open_count(), 1)
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "position")

    def test_reconciliation_not_match(self):
        # the broker shows a position the local state does not have
        client = _Client(positions=[{"ticker": "KXBTC15M-Q", "position": 2}])
        client, tlog, pos, om, led, risk = self.blown(client)
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "reconciliation")

    def test_pending_residual_and_unclassified_flow_and_existing_hold(self):
        client, tlog, pos, om, led, risk = self.blown()
        led.observe(7.55, cycle_n=50, quiet=True)             # pending residual
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "pending residual")
        led.state["pending"] = None
        for i in range(3):
            led.observe(5.0, cycle_n=60 + i, quiet=True)      # unclassified -2
        self.assertTrue(led.unclassified_flows())
        self.assert_refused(led, self.ctx(client, om, pos, risk, led), "unclassified")
        fid = led.unclassified_flows()[0]["id"]
        led.classify_flow(fid, "withdrawal", action_id="OPS-W")
        led.state["capital_hold"] = {"reason": "post_rebase_validation", "since": "x",
                                     "rebase_id": "rb-x", "released_by": None}
        led.save()
        hwm = led.risk_equity_reference()
        rb = led.propose_rebase("r", "OPS-H")
        self.assertFalse(led.apply_rebase("r", "OPS-H", rb["token"], self.ctx(client, om, pos, risk, led)))
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)

    def test_positive_control_a_clean_state_is_accepted(self):
        client, tlog, pos, om, led, risk = self.blown()
        ctx = self.ctx(client, om, pos, risk, led)
        self.assertEqual(ctx["orders"]["broker_open"], 0)
        self.assertEqual(ctx["reconcile_status"], "MATCH")
        rb = led.propose_rebase("losses acknowledged", "OPS-OK")
        self.assertTrue(led.apply_rebase("losses acknowledged", "OPS-OK", rb["token"], ctx))
        self.assertTrue(led.state["capital_hold"])

    def test_order_and_position_checks_read_the_same_truth(self):
        """The context is built from the persisted OrderManager state, the
        persisted PositionManager state AND a fresh broker query for both;
        a disagreement between any two is itself a refusal."""
        client = _Client(orders=[{"order_id": "ord-5", "status": "resting", "remaining_count": 1}])
        client, tlog, pos, om, led, risk = self.blown(client)
        om.open_orders["ord-9"] = {"ticker": "KXBTC15M-X", "side": "no", "count": 1,
                                   "price": 9, "placed_at": "now"}
        om.flush()
        ctx = self.ctx(client, om, pos, risk, led)
        self.assertEqual(sorted(ctx["orders"]["local_open"]), ["ord-9"])
        self.assertEqual(ctx["orders"]["broker_open"], 1)
        self.assertTrue(ctx["orders"]["disagreement"])
        self.assert_refused(led, ctx, "disagree")


if __name__ == "__main__":
    unittest.main()
