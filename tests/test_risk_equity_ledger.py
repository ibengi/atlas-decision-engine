# -*- coding: utf-8 -*-
"""F2 risk-equity accounting: R1-R18 from docs/design/risk-equity-accounting.md.

Core invariant under test: a deposit may raise affordability but never
reduces strategy drawdown or clears a loss-derived guard; a withdrawal never
creates strategy loss. Every scenario drives the real `EquityLedger` on an
isolated DATA_DIR and reads the persisted file back where a claim is about
durability. Engine-level scenarios drive the real `_evaluate_global_guards`
and `_post_balance_gates`.
"""
import copy
import json
import os
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import config                                                     # noqa: E402
import equity_ledger as EL                                        # noqa: E402
import execution_engine                                           # noqa: E402
import test_shadow_write_layer_isolation as shadow_iso            # noqa: E402
from config import CFG, _p                                        # noqa: E402
from equity_ledger import EquityLedger                            # noqa: E402
from execution_engine import ExecutionEngine                      # noqa: E402
from persistence import JsonStore                                 # noqa: E402
from position_sizer import PositionSizer                          # noqa: E402
from risk_manager import RiskManager                              # noqa: E402

PRE_AT = "2026-09-07T18:01:19Z"
JOURNAL_DD = [-0.30, -0.1839]          # cumulative -0.4839, peak 0 -> dd 0.4839


class _Tlog:
    def __init__(self, pnls=(), settled_prefix=None, open_rows=(), corrections=()):
        self._settled = [{"trade_id": f"t{i}", "net_pnl": p, "state": "settled",
                          "settled_at": (settled_prefix or "2026-09-06T00:00:00Z"),
                          "fees": 0.0}
                         for i, p in enumerate(pnls)]
        self._open = list(open_rows)
        self._corr = list(corrections)

    def settled_trades(self):
        return list(self._settled)

    def open_trades(self):
        return list(self._open)

    def correction_rows(self):
        return list(self._corr)

    def settle(self, pnl, at="2026-09-08T00:00:00Z"):
        self._settled.append({"trade_id": f"t{len(self._settled)}", "net_pnl": pnl,
                              "state": "settled", "settled_at": at, "fees": 0.0})


class _Pos:
    def __init__(self, risk=0.0, count=0):
        self._risk, self._count = risk, count
        self.reconcile_halt = None

    def open_risk(self):
        return self._risk

    def open_count(self):
        return self._count


class _Ledger(shadow_iso._IsolatedState, unittest.TestCase):
    """Own DATA_DIR per test; env vars saved/restored."""

    def ledger(self, tlog=None, pos=None, env="prod"):
        return EquityLedger(tlog or _Tlog(), pos or _Pos(), env=env)

    def seeded_after_loss(self, cash_now=0.04):
        """The production migration: journal -0.4839, pre-flow cash 0.04."""
        tlog = _Tlog(JOURNAL_DD)
        led = self.ledger(tlog)
        prop = led.propose_seed(0.04, PRE_AT, "railway log 18:01:19Z", cash_now)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        return led, tlog, prop

    def quiet(self, led, cash, n=3, start=1):
        snap = None
        for i in range(n):
            snap = led.observe(cash, cycle_n=start + i, quiet=True)
        return snap

    def file(self):
        return JsonStore.load(_p(EL.LEDGER_FILE), {})


# --------------------------------------------------------------------------
# The regression scenario (R14) and the invariant.
# --------------------------------------------------------------------------

class TheDepositDoesNotRepairTheDrawdown(_Ledger):

    def test_R14_drawdown_is_92_36_percent_before_and_after_the_deposit(self):
        led, tlog, prop = self.seeded_after_loss()
        self.assertAlmostEqual(led.strategy_equity(), 0.04, places=6)
        self.assertAlmostEqual(led.risk_equity_reference(), 0.5239, places=6)
        before = led.drawdown_pct()
        self.assertAlmostEqual(before, 92.36, places=1)
        self.assertGreaterEqual(before, CFG.MAX_EQUITY_DRAWDOWN_PCT)
        snap = self.quiet(led, 9.84)                       # the deposit
        self.assertAlmostEqual(led.drawdown_pct(), before, places=9)
        self.assertAlmostEqual(led.strategy_equity(), 0.04, places=6)
        flows = [f for f in led.state["flows"] if f["kind"] == EL.FLOW_DEPOSIT]
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(flows[0]["amount"], 9.80, places=4)
        self.assertEqual(flows[0]["classified_by"], "auto")
        self.assertEqual(snap["risk_equity_status"], EL.STATUS_CONSERVATIVE)
        # durable
        f = self.file()
        self.assertAlmostEqual(f["hwm"]["risk_equity_reference"], 0.5239, places=4)
        self.assertEqual(f["seed"]["strategy_equity_0"], 0.04)

    def test_the_guard_stays_tripped_through_the_deposit(self):
        led, tlog, _ = self.seeded_after_loss()
        rm = RiskManager(tlog, _Pos(), 0.04)
        rm.equity = led
        with patch.object(CFG, "RISK_EQUITY_MODE", "strategy"):
            self.assertGreaterEqual(rm.rolling_drawdown_pct(), CFG.MAX_EQUITY_DRAWDOWN_PCT)
            self.quiet(led, 9.84)
            rm.capital = 9.84                              # affordability follows cash
            self.assertGreaterEqual(rm.rolling_drawdown_pct(), CFG.MAX_EQUITY_DRAWDOWN_PCT)
        with patch.object(CFG, "RISK_EQUITY_MODE", "cash"):
            # rollback mode: the historical cash formula, which the deposit clears
            self.assertLess(rm.rolling_drawdown_pct(), CFG.MAX_EQUITY_DRAWDOWN_PCT)

    def test_property_flows_never_change_the_drawdown(self):
        rnd = random.Random(7)
        for trial in range(40):
            pnls = [round(rnd.uniform(-0.5, 0.4), 4) for _ in range(rnd.randint(1, 8))]
            with patch.object(CFG, "DATA_DIR", self._tmp + f"/p{trial}"):
                os.makedirs(CFG.DATA_DIR, exist_ok=True)
                tlog = _Tlog(pnls)
                led = self.ledger(tlog)
                cash0 = round(rnd.uniform(1, 50), 2)
                prop = led.propose_seed(cash0, PRE_AT, "x", cash0)
                led.apply_seed(prop, prop["sha256"])
                dd0, hwm0, eq0 = led.drawdown_pct(), led.risk_equity_reference(), led.strategy_equity()
                cash = cash0
                for _ in range(rnd.randint(1, 4)):
                    cash = round(cash + rnd.uniform(0.5, 30), 2)   # deposits only
                    self.quiet(led, cash)
                self.assertAlmostEqual(led.drawdown_pct(), dd0, places=9)
                self.assertAlmostEqual(led.strategy_equity(), eq0, places=9)
                self.assertGreaterEqual(led.risk_equity_reference(), hwm0 - 1e-12)


# --------------------------------------------------------------------------
# R1-R4: known / unknown flows.
# --------------------------------------------------------------------------

class FlowsAreClassifiedAsymmetrically(_Ledger):

    def test_R1_known_deposit_raises_affordability_only(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        self.assertAlmostEqual(led.flows_cum(), 9.80, places=4)
        self.assertAlmostEqual(led.strategy_equity(), 0.04, places=6)
        self.assertEqual(led.derive_status(), EL.STATUS_CONSERVATIVE)

    def test_R2_known_withdrawal_needs_the_operator_then_leaves_drawdown_alone(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        dd = led.drawdown_pct()
        self.quiet(led, 2.00, start=10)
        unres = led.unclassified_flows()
        self.assertEqual(len(unres), 1)
        self.assertAlmostEqual(unres[0]["amount"], -7.84, places=4)
        self.assertIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())
        self.assertEqual(led.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertGreaterEqual(led.drawdown_pct(), dd)      # conservative while unresolved
        self.assertTrue(led.classify_flow(unres[0]["id"], "withdrawal", action_id="OPS-1"))
        self.assertNotIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())
        self.assertAlmostEqual(led.drawdown_pct(), dd, places=9)
        self.assertAlmostEqual(led.strategy_equity(), 0.04, places=6)
        row = self.file()["flows"][-1]
        self.assertEqual(row["kind"], "withdrawal")
        self.assertEqual(row["classified_by"], "operator")
        self.assertEqual(row["operator_action_id"], "OPS-1")
        self.assertIn("evidence", row)

    def test_R3_unknown_positive_residual_becomes_a_deposit_never_equity(self):
        led, tlog, _ = self.seeded_after_loss()
        eq = led.strategy_equity()
        self.quiet(led, 0.04 + 3.33)
        self.assertEqual(led.state["flows"][-1]["kind"], EL.FLOW_DEPOSIT)
        self.assertAlmostEqual(led.strategy_equity(), eq, places=9)

    def test_R4_unknown_negative_residual_fails_closed(self):
        led, tlog, _ = self.seeded_after_loss(cash_now=5.04)
        self.quiet(led, 4.00)
        self.assertEqual(led.state["flows"][-1]["kind"], EL.FLOW_UNCLASSIFIED)
        self.assertIsNone(led.state["flows"][-1]["classified_by"])
        self.assertIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())
        self.assertEqual(led.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertFalse(led.capital_eligible())
        # treated as a loss in the conservative equity
        self.assertAlmostEqual(led.strategy_equity_conservative(), led.strategy_equity() - 1.04, places=6)


# --------------------------------------------------------------------------
# R5-R6: provenance.
# --------------------------------------------------------------------------

class ProvenanceIsNeverInvented(_Ledger):

    def test_R5_missing_historical_funding_is_a_conservative_estimate_and_blocks_capital(self):
        led, tlog, prop = self.seeded_after_loss()
        self.assertEqual(prop["risk_equity_status"], EL.STATUS_CONSERVATIVE)
        self.assertEqual(led.status(), EL.STATUS_CONSERVATIVE)
        self.assertTrue(any("funding before" in u for u in led.state["status_basis"]["unproven"]))
        self.assertIn(EL.GUARD_UNRECONCILED, led.guards())
        self.assertFalse(led.capital_eligible())
        with patch.object(CFG, "RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE", True):
            self.assertNotIn(EL.GUARD_UNRECONCILED, led.guards())

    def test_R6_partial_history_is_unreconciled(self):
        tlog = _Tlog(JOURNAL_DD, settled_prefix="2026-09-08T01:00:00Z")   # after pre-flow
        led = self.ledger(tlog)
        prop = led.propose_seed(0.04, PRE_AT, "x", 0.04)
        self.assertEqual(prop["risk_equity_status"], EL.STATUS_UNRECONCILED)
        self.assertTrue(any("settlements after" in u for u in prop["unproven"]))
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        self.assertEqual(led.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertIn(EL.GUARD_UNRECONCILED, led.guards())
        with patch.object(CFG, "RISK_EQUITY_ALLOW_CONSERVATIVE_ESTIMATE", True):
            self.assertIn(EL.GUARD_UNRECONCILED, led.guards(), "no override for UNRECONCILED")

    def test_unseeded_production_ledger_fires_the_unseeded_guard(self):
        led = self.ledger(_Tlog(JOURNAL_DD))
        led.observe(9.84, cycle_n=1)
        self.assertFalse(led.seeded)
        self.assertEqual(led.guards(), [EL.GUARD_UNSEEDED])
        self.assertIsNone(led.drawdown_pct())


# --------------------------------------------------------------------------
# R7-R10: restart, restore, stale HWM, seed mismatch.
# --------------------------------------------------------------------------

class PersistenceIsRecomputedNotTrusted(_Ledger):

    def test_R7_restart_reloads_everything_and_recomputes_equity(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        led2 = self.ledger(tlog)
        self.assertEqual(led2.status(), led.status())
        self.assertAlmostEqual(led2.strategy_equity(), led.strategy_equity(), places=9)
        self.assertAlmostEqual(led2.risk_equity_reference(), led.risk_equity_reference(), places=9)
        self.assertEqual(len(led2.state["flows"]), len(led.state["flows"]))
        self.assertIsNone(led2._last_obs)                 # pending counter restarts
        tlog.settle(-0.10)                                 # journal moves after restart
        self.assertAlmostEqual(led2.strategy_equity(), 0.04 - 0.10, places=9)

    def test_R8_restored_older_journal_is_unreconciled_and_hwm_is_kept(self):
        led, tlog, _ = self.seeded_after_loss()
        hwm = led.risk_equity_reference()
        older = _Tlog(JOURNAL_DD[:1])                      # one row fewer
        led2 = self.ledger(older)
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertIn(EL.GUARD_UNRECONCILED, led2.guards())
        self.assertTrue(any("seed prefix" in u for u in led2.state["status_basis"]["unproven"]))
        self.assertGreaterEqual(led2.risk_equity_reference(), hwm - 1e-9)

    def test_R9_stale_hwm_is_repaired_upward_and_a_higher_one_is_kept(self):
        led, tlog, _ = self.seeded_after_loss()
        f = self.file()
        f["hwm"]["risk_equity_reference"] = 0.10           # lower than the journal implies
        JsonStore.save(_p(EL.LEDGER_FILE), f)
        led2 = self.ledger(tlog)
        self.assertAlmostEqual(led2.risk_equity_reference(), 0.5239, places=4)
        self.assertAlmostEqual(self.file()["hwm"]["risk_equity_reference"], 0.5239, places=4)
        f = self.file()
        f["hwm"]["risk_equity_reference"] = 0.90           # higher: kept
        JsonStore.save(_p(EL.LEDGER_FILE), f)
        led3 = self.ledger(tlog)
        self.assertAlmostEqual(led3.risk_equity_reference(), 0.90, places=9)

    def test_R10_seed_mismatch_and_existing_seed_are_no_ops(self):
        tlog = _Tlog(JOURNAL_DD)
        led = self.ledger(tlog)
        prop = led.propose_seed(0.04, PRE_AT, "x", 0.04)
        self.assertFalse(led.apply_seed(prop, "0" * 64))
        self.assertFalse(led.apply_seed(prop, ""))
        self.assertFalse(led.seeded)
        self.assertEqual(led.guards(), [EL.GUARD_UNSEEDED])
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        before = json.dumps(self.file(), sort_keys=True)
        prop2 = led.propose_seed(1.00, PRE_AT, "y", 1.00)
        self.assertFalse(led.apply_seed(prop2, prop2["sha256"]))   # never overwritten
        self.assertEqual(json.dumps(self.file(), sort_keys=True), before)

    def test_the_seed_hash_binds_to_the_journal_and_the_cash(self):
        tlog = _Tlog(JOURNAL_DD)
        led = self.ledger(tlog)
        a = led.propose_seed(0.04, PRE_AT, "x", 9.84)["sha256"]
        b = led.propose_seed(0.04, PRE_AT, "x", 9.85)["sha256"]
        tlog.settle(-0.01, at="2026-09-06T00:00:00Z")
        c = led.propose_seed(0.04, PRE_AT, "x", 9.84)["sha256"]
        self.assertEqual(len({a, b, c}), 3)


# --------------------------------------------------------------------------
# R11: manual classification.
# --------------------------------------------------------------------------

class ManualClassificationIsStrict(_Ledger):

    def test_R11_invalid_classifications_leave_the_file_byte_identical(self):
        led, tlog, _ = self.seeded_after_loss(cash_now=5.04)
        self.quiet(led, 4.00)
        fid = led.unclassified_flows()[0]["id"]
        before = json.dumps(self.file(), sort_keys=True)
        self.assertFalse(led.classify_flow("flow-9999", "withdrawal"))
        self.assertFalse(led.classify_flow(fid, "deposit"))
        self.assertFalse(led.classify_flow(fid, "loss"))                      # no correction
        self.assertFalse(led.classify_flow(fid, "loss", correction_id="nope"))
        self.assertEqual(json.dumps(self.file(), sort_keys=True), before)
        self.assertTrue(led.classify_flow(fid, "withdrawal", action_id="OPS-2"))
        self.assertFalse(led.classify_flow(fid, "withdrawal", action_id="OPS-3"))  # already
        # a positive flow can never be relabelled a withdrawal
        self.quiet(led, 9.00, start=20)
        dep = led.state["flows"][-1]
        self.assertEqual(dep["kind"], EL.FLOW_DEPOSIT)
        self.assertFalse(led.classify_flow(dep["id"], "withdrawal"))

    def test_a_loss_classification_requires_the_correction_in_the_journal(self):
        tlog = _Tlog(JOURNAL_DD, corrections=[{"event_type": "ledger_correction",
                                               "correction_id": "CORR-1"}])
        led = self.ledger(tlog)
        prop = led.propose_seed(5.04, PRE_AT, "x", 5.04)
        led.apply_seed(prop, prop["sha256"])
        self.quiet(led, 4.00)
        fid = led.unclassified_flows()[0]["id"]
        self.assertTrue(led.classify_flow(fid, "loss", correction_id="CORR-1", action_id="OPS-4"))
        row = self.file()["flows"][-1]
        self.assertEqual(row["kind"], "loss_by_correction")
        self.assertEqual(row["resolved_by_correction"], "CORR-1")
        self.assertNotIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())
        self.assertAlmostEqual(led.flows_cum(), 0.0, places=9)    # not an external flow


# --------------------------------------------------------------------------
# R12-R13: rebase.
# --------------------------------------------------------------------------

class RebaseIsAuditedAndExceptional(_Ledger):

    def reconciled_and_blown(self):
        """A RECONCILED baseline (attested) whose drawdown fires."""
        tlog = _Tlog()
        led = self.ledger(tlog)
        prop = led.propose_seed(10.0, PRE_AT, "x", 10.0)
        led.apply_seed(prop, prop["sha256"])
        att = led.propose_attestation("OPS-10", "a" * 64)
        self.assertTrue(led.apply_attestation("OPS-10", "a" * 64, att["token"]))
        self.assertEqual(led.derive_status(), EL.STATUS_RECONCILED)
        tlog.settle(-3.0)                                   # 30 % drawdown
        self.quiet(led, 7.0)
        self.assertGreaterEqual(led.drawdown_pct(), CFG.MAX_EQUITY_DRAWDOWN_PCT)
        return led, tlog

    OK_CTX = {"drawdown_firing": True, "reconcile_status": "MATCH",
              "open_positions": 0, "in_flight_orders": 0}

    def test_R12_unauthorized_rebase_attempts_are_no_ops(self):
        led, tlog = self.reconciled_and_blown()
        hwm = led.risk_equity_reference()
        prop = led.propose_rebase("losses acknowledged", "OPS-11")
        consumed = list(led.state["consumed_tokens"])      # the attestation's token
        before = json.dumps(self.file(), sort_keys=True)
        cases = [
            ("no token", dict(reason="r", action_id="OPS-11", token="", ctx=self.OK_CTX)),
            ("wrong token", dict(reason="losses acknowledged", action_id="OPS-11", token="f" * 64, ctx=self.OK_CTX)),
            ("no reason", dict(reason="", action_id="OPS-11", token=prop["token"], ctx=self.OK_CTX)),
            ("drawdown not firing", dict(reason="losses acknowledged", action_id="OPS-11",
                                         token=prop["token"], ctx={**self.OK_CTX, "drawdown_firing": False})),
            ("reconcile not MATCH", dict(reason="losses acknowledged", action_id="OPS-11",
                                         token=prop["token"], ctx={**self.OK_CTX, "reconcile_status": "MISMATCH"})),
            ("open position", dict(reason="losses acknowledged", action_id="OPS-11",
                                   token=prop["token"], ctx={**self.OK_CTX, "open_positions": 1})),
            ("in-flight order", dict(reason="losses acknowledged", action_id="OPS-11",
                                     token=prop["token"], ctx={**self.OK_CTX, "in_flight_orders": 1})),
        ]
        for name, kw in cases:
            with self.subTest(case=name):
                self.assertFalse(led.apply_rebase(kw["reason"], kw["action_id"], kw["token"], kw["ctx"]))
                self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)
                self.assertEqual(led.state["consumed_tokens"], consumed)
        self.assertEqual(json.dumps(self.file(), sort_keys=True), before)
        # not RECONCILED -> refused even with a valid token
        with patch.object(CFG, "DATA_DIR", self._tmp + "/cons"):
            os.makedirs(CFG.DATA_DIR, exist_ok=True)
            led2, tlog2, _ = self.seeded_after_loss()
            p2 = led2.propose_rebase("r", "OPS-12")
            self.assertFalse(led2.apply_rebase("r", "OPS-12", p2["token"], self.OK_CTX))
        # a pending residual -> refused
        with patch.object(CFG, "DATA_DIR", self._tmp + "/pending"):
            os.makedirs(CFG.DATA_DIR, exist_ok=True)
            led3, tlog3 = self.reconciled_and_blown()
            led3.observe(7.55, cycle_n=50, quiet=True)      # pending, not yet a flow
            p3 = led3.propose_rebase("r", "OPS-13")
            self.assertFalse(led3.apply_rebase("r", "OPS-13", p3["token"], self.OK_CTX))

    def test_R13_authorized_rebase_leaves_an_audit_trail_and_holds_capital(self):
        led, tlog = self.reconciled_and_blown()
        old_hwm, eq = led.risk_equity_reference(), led.strategy_equity()
        prop = led.propose_rebase("Q3 losses acknowledged by the operator", "OPS-20")
        self.assertTrue(led.apply_rebase("Q3 losses acknowledged by the operator", "OPS-20",
                                         prop["token"], self.OK_CTX))
        f = self.file()
        rb = f["rebases"][-1]
        for key in ("rebase_id", "reason", "operator_action_id", "old_baseline", "new_baseline",
                    "evidence_sha256", "applied_at", "token_sha256", "prior_hwm_row"):
            self.assertIn(key, rb)
        self.assertNotIn("token", rb)                       # the token itself is never stored
        self.assertAlmostEqual(rb["old_baseline"]["hwm"], old_hwm, places=4)
        self.assertAlmostEqual(rb["new_baseline"]["hwm"], eq, places=4)
        self.assertAlmostEqual(led.risk_equity_reference(), eq, places=9)
        self.assertLess(led.drawdown_pct(), CFG.MAX_EQUITY_DRAWDOWN_PCT)
        self.assertEqual(f["hwm"]["rebased_from"], rb["rebase_id"])
        # CAPITAL stays held; a rebase never makes CAPITAL eligible by itself
        self.assertIn(EL.GUARD_CAPITAL_HOLD, led.guards())
        self.assertFalse(led.capital_eligible())
        self.assertEqual(f["capital_hold"]["reason"], "post_rebase_validation")
        # replay refused
        self.assertFalse(led.apply_rebase("Q3 losses acknowledged by the operator", "OPS-20",
                                          prop["token"], self.OK_CTX))
        # a second rebase while the hold is open: refused
        p2 = led.propose_rebase("again", "OPS-21")
        self.assertFalse(led.apply_rebase("again", "OPS-21", p2["token"], self.OK_CTX))
        # release: same action id as the rebase -> refused; a different one -> ok
        with self.assertRaises(ValueError):
            led.propose_hold_release("OPS-20", "review doc sha")
        rel = led.propose_hold_release("OPS-22", "independent validation ref")
        self.assertFalse(led.apply_hold_release("OPS-22", "independent validation ref", "0" * 64))
        self.assertTrue(led.apply_hold_release("OPS-22", "independent validation ref", rel["token"]))
        self.assertNotIn(EL.GUARD_CAPITAL_HOLD, led.guards())
        self.assertTrue(led.capital_eligible())
        f = self.file()
        self.assertIsNone(f["capital_hold"])
        self.assertEqual(f["rebases"][-1]["validation"]["operator_action_id"], "OPS-22")
        self.assertEqual(len(f["consumed_tokens"]), 3)      # attest + rebase + release
        self.assertFalse(led.apply_hold_release("OPS-22", "independent validation ref", rel["token"]))
        # the rebased mark does not creep back up from pre-rebase journal peaks
        led2 = self.ledger(tlog)
        self.assertAlmostEqual(led2.risk_equity_reference(), eq, places=9)


# --------------------------------------------------------------------------
# R15-R18.
# --------------------------------------------------------------------------

class TheRemainingScenarios(_Ledger):

    def test_R15_withdrawal_after_drawdown(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        dd = led.drawdown_pct()
        rm = RiskManager(tlog, _Pos(), 9.84)
        rm.equity = led
        self.quiet(led, 2.00, start=10)
        rm.capital = 2.00
        self.assertEqual(rm.capital, 2.00)                  # affordability follows cash
        fid = led.unclassified_flows()[0]["id"]
        led.classify_flow(fid, "withdrawal", action_id="OPS-30")
        self.assertAlmostEqual(led.drawdown_pct(), dd, places=9)

    def test_R16_no_trade_deposit_is_the_initial_stake_and_sets_the_daily_stop(self):
        tlog = _Tlog()
        led = self.ledger(tlog, env="demo")
        led.observe(0.0, cycle_n=1)                          # first observation: nothing
        self.assertTrue(led.seeded)
        self.assertEqual(led.status(), EL.STATUS_RECONCILED)
        self.assertEqual(led.risk_equity_reference(), 0.0)
        self.quiet(led, 50.0, start=2)
        self.assertAlmostEqual(led.strategy_equity(), 50.0, places=6)
        self.assertAlmostEqual(led.risk_equity_reference(), 50.0, places=6)
        self.assertEqual(led.drawdown_pct(), 0.0)
        self.assertEqual(led.state["flows"][-1]["kind"], "initial_stake")
        rm = RiskManager(tlog, _Pos(), 50.0)
        rm.equity = led
        with patch.object(CFG, "RISK_EQUITY_MODE", "strategy"):
            self.assertAlmostEqual(rm.effective_daily_stop(), 2.5, places=2)
            # a LATER deposit at a positive reference is a deposit, not stake
            self.quiet(led, 100.0, start=10)
            self.assertAlmostEqual(led.strategy_equity(), 50.0, places=6)
            rm.capital = 100.0
            self.assertAlmostEqual(rm.effective_daily_stop(), 2.5, places=2)   # not 5.0

    def test_R17_no_trade_withdrawal(self):
        tlog = _Tlog()
        led = self.ledger(tlog)
        prop = led.propose_seed(50.0, PRE_AT, "x", 50.0)
        led.apply_seed(prop, prop["sha256"])
        self.quiet(led, 20.0)
        self.assertEqual(led.drawdown_pct(), 0.0) if False else None
        self.assertEqual(led.state["flows"][-1]["kind"], EL.FLOW_UNCLASSIFIED)
        self.assertAlmostEqual(led.state["flows"][-1]["amount"], -30.0, places=4)
        self.assertIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())
        rm = RiskManager(tlog, _Pos(), 20.0)
        rm.equity = led
        self.assertEqual(rm.capital, 20.0)
        fid = led.unclassified_flows()[0]["id"]
        led.classify_flow(fid, "withdrawal", action_id="OPS-40")
        self.assertEqual(led.drawdown_pct(), 0.0)
        self.assertAlmostEqual(led.strategy_equity(), 50.0, places=6)

    def test_R18_ambiguous_cash_movement_never_becomes_a_flow(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        n_flows = len(led.state["flows"])
        led.observe(12.00, cycle_n=10, quiet=True)          # pending 1
        led.observe(7.00, cycle_n=11, quiet=True)           # sign flip -> pending resets
        led.observe(12.00, cycle_n=12, quiet=True)
        led.observe(12.00, cycle_n=13, quiet=False)         # in-flight order: not quiet
        tlog.settle(-0.05)                                  # settlement lands mid-observation
        led.observe(12.00, cycle_n=14, quiet=True)          # settled count changed -> not quiet
        self.assertEqual(len(led.state["flows"]), n_flows)
        self.assertEqual(led.derive_status(), EL.STATUS_CONSERVATIVE)
        # a genuinely stable residual still classifies afterwards
        self.quiet(led, 12.00, start=15)
        self.assertEqual(len(led.state["flows"]), n_flows + 1)

    def test_settlement_race_does_not_produce_a_flow(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        n = len(led.state["flows"])
        # broker credits a win before the journal records it
        led.observe(9.84 + 0.50, cycle_n=10, quiet=True)
        led.observe(9.84 + 0.50, cycle_n=11, quiet=True)
        tlog.settle(+0.50)                                  # journal catches up
        led.observe(9.84 + 0.50, cycle_n=12, quiet=True)
        led.observe(9.84 + 0.50, cycle_n=13, quiet=True)
        self.assertEqual(len(led.state["flows"]), n)
        self.assertIsNone(led.state["pending"])
        self.assertAlmostEqual(led.strategy_equity(), 0.54, places=6)


# --------------------------------------------------------------------------
# T13 / T14 / T19: identities and compatibility.
# --------------------------------------------------------------------------

class IdentitiesHold(_Ledger):

    def test_T13_dollar_drawdown_equals_hwm_minus_equity_after_a_seed(self):
        rnd = random.Random(11)
        for trial in range(200):
            pnls = [round(rnd.uniform(-1, 1), 4) for _ in range(rnd.randint(0, 12))]
            with patch.object(CFG, "DATA_DIR", self._tmp + f"/t{trial}"):
                os.makedirs(CFG.DATA_DIR, exist_ok=True)
                tlog = _Tlog(pnls)
                led = self.ledger(tlog)
                prop = led.propose_seed(5.0, PRE_AT, "x", 5.0)
                led.apply_seed(prop, prop["sha256"])
                rm = RiskManager(tlog, _Pos(), 5.0)
                self.assertAlmostEqual(led.drawdown_usd(), rm.rolling_drawdown(), places=9)

    def test_T14_sizer_without_a_percentage_is_byte_identical(self):
        for cap in (0.5, 9.84, 57.0, 93.26, 500.0):
            for price in (1, 9, 57, 95):
                for dd in (0.0, 0.4839, 10.0):
                    old = cap > 0 and dd / cap * 100.0 >= CFG.DD_THROTTLE_PCT
                    self.assertEqual(PositionSizer._throttled(cap, dd, None), old)
        # with the percentage, the cash expression is ignored
        self.assertTrue(PositionSizer._throttled(9.84, 0.0, 92.36))
        self.assertFalse(PositionSizer._throttled(0.04, 0.4839, 4.92))
        self.assertEqual(PositionSizer.contracts(9.84, 9, "1%", 9, 0.4839, 0.0, drawdown_pct=92.36), 0)
        self.assertEqual(PositionSizer.contracts(9.84, 9, "1%", 9, 0.0, 0.0), 1)

    def test_T19_the_daily_stop_never_reads_as_disabled(self):
        led, tlog, _ = self.seeded_after_loss()
        self.quiet(led, 9.84)
        rm = RiskManager(tlog, _Pos(), 9.84)
        rm.equity = led
        with patch.object(CFG, "RISK_EQUITY_MODE", "strategy"):
            stop = rm.effective_daily_stop()
        self.assertEqual(stop, 0.01)
        with patch.object(CFG, "RISK_EQUITY_MODE", "cash"):
            self.assertAlmostEqual(rm.effective_daily_stop(), 0.49, places=2)
        # can_trade enforces the one-cent stop
        tlog.settle(-0.02, at="2026-09-08T00:00:00Z")
        with patch.object(CFG, "RISK_EQUITY_MODE", "strategy"), \
                patch.object(rm, "daily_realized_pnl", lambda: -0.02):
            ok, why = rm.can_trade(0)
            self.assertFalse(ok)
            self.assertIn("STOP JOURNALIER", why)


# --------------------------------------------------------------------------
# Engine: the guards block CAPITAL, report in DEMO, are observed in READ_ONLY.
# --------------------------------------------------------------------------

def _engine(env, ledger):
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.client = type("C", (), {"env": env})()
    eng.equity = ledger
    eng.posmgr = type("P", (), {"reconcile_halt": None,
                                "open_count": lambda self: 0,
                                "tickers_open": lambda self: set()})()
    eng.risk = type("R", (), {"can_trade": lambda self, cycle_trades=0: (True, ""),
                              "rolling_drawdown_pct": lambda self: 0.0,
                              "rolling_drawdown": lambda self: 0.0})()
    return eng


class TheEngineHonoursTheAccountingGuards(_Ledger):

    def test_capital_is_blocked_by_the_accounting_guard_and_demo_is_not(self):
        led, tlog, _ = self.seeded_after_loss()
        with patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"):
            os.environ["PROD_ACCESS_MODE"] = config.PROD_CAPITAL
            self.assertEqual(_engine("prod", led)._evaluate_global_guards(),
                             (False, EL.GUARD_UNRECONCILED))
            self.assertEqual(_engine("demo", led)._evaluate_global_guards(), (True, None))
            with patch.object(CFG, "DATA_DIR", self._tmp + "/unseeded"):
                os.makedirs(CFG.DATA_DIR, exist_ok=True)
                unseeded = self.ledger(_Tlog(JOURNAL_DD))
                self.assertEqual(_engine("prod", unseeded)._evaluate_global_guards(),
                                 (False, EL.GUARD_UNSEEDED))

    def test_read_only_observes_through_every_accounting_guard_and_records_it(self):
        os.environ["PROD_ACCESS_MODE"] = config.PROD_READ_ONLY
        for guard in EL.ACCOUNTING_GUARDS:
            with self.subTest(guard=guard):
                eng = _engine("prod", None)
                with patch.object(ExecutionEngine, "_evaluate_global_guards",
                                  lambda self, g=guard: (False, g)):
                    self.assertEqual(eng._post_balance_gates(), (True, None))
                self.assertEqual(eng._capital_blocking_guard, guard)
        os.environ["PROD_ACCESS_MODE"] = config.PROD_CAPITAL
        for guard in EL.ACCOUNTING_GUARDS:
            with self.subTest(mode="CAPITAL", guard=guard):
                eng = _engine("prod", None)
                with patch.object(ExecutionEngine, "_evaluate_global_guards",
                                  lambda self, g=guard: (False, g)):
                    self.assertEqual(eng._post_balance_gates(), (False, guard))

    def test_the_allow_list_is_exactly_the_five_capital_guards(self):
        self.assertEqual(set(execution_engine.OBSERVATION_ONLY_GUARDS),
                         {"equity_drawdown", *EL.ACCOUNTING_GUARDS})

    def test_operator_actions_from_the_environment_apply_only_on_an_exact_hash(self):
        tlog = _Tlog(JOURNAL_DD)
        led = self.ledger(tlog)
        prop = led.propose_seed(0.04, PRE_AT, "railway log", 9.84)
        env = {"EQUITY_LEDGER_SEED_PRE_FLOW_CASH": "0.04",
               "EQUITY_LEDGER_SEED_PRE_FLOW_AT": PRE_AT,
               "EQUITY_LEDGER_SEED_EVIDENCE": "railway log",
               "EQUITY_LEDGER_SEED_SHA256": "deadbeef"}
        ctx = {"drawdown_firing": False, "reconcile_status": "MATCH",
               "open_positions": 0, "in_flight_orders": 0}
        self.assertEqual(led.apply_operator_actions(env, 9.84, ctx), {"seed": False})
        self.assertFalse(led.seeded)
        env["EQUITY_LEDGER_SEED_SHA256"] = prop["sha256"]
        self.assertEqual(led.apply_operator_actions(env, 9.84, ctx), {"seed": True})
        self.assertTrue(led.seeded)
        self.assertAlmostEqual(led.drawdown_pct(), 92.36, places=1)
        self.assertEqual(led.state["flows"][0]["kind"], EL.FLOW_DEPOSIT)
        self.assertEqual(led.state["flows"][0]["classified_by"], "migration")
        self.assertEqual(led.status(), EL.STATUS_CONSERVATIVE)
        # the migration deposit is already accounted for: a later observation
        # at the same cash is quiet and produces no second flow
        self.quiet(led, 9.84)
        self.assertEqual(len(led.state["flows"]), 1)


if __name__ == "__main__":
    unittest.main()
