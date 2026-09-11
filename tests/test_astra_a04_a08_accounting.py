from authority_fixtures import corrupt_json
# -*- coding: utf-8 -*-
"""A04-A08 -- accounting and evidence defects.

A04 DUPLICATE ECONOMIC EVENT REPLAY
    INVARIANT   A settled trade is one economic event. Replaying it must
                never improve historical drawdown without new economic
                evidence.
    ROOT CAUSE  `settled_trades()` summed rows; nothing enforced identity.
                Astra appended a copy of a +3 row with the SAME trade_id and
                the drawdown fell from 30.769% to 7.692% with no mismatch:
                the count grew, the digest stayed self-consistent, and the
                aggregate looked plausible.
    CORRECTION  `TradeLogger.event_keys` defines economic identity
                (trade_id, settlement_id, correction_id); appends carrying
                an existing identity are REFUSED; `EquityLedger
                .duplicate_events()` detects any that reach the journal by
                other means; the watermark refuses to extend over them and
                `GUARD_JOURNAL_INTEGRITY` blocks CAPITAL. Corrections and
                reversals keep working: they are DIFFERENT events with their
                own correction_id pointing at the row they adjust.

A05 UNKNOWN ACCOUNTING MODE
    INVARIANT   An unrecognized configuration is never a licence to use a
                denominator that a deposit can repair.
    ROOT CAUSE  `_strategy_mode()` tested `mode == "strategy"`, so any other
                string silently selected the cash denominator. With a -3
                history and a deposit, both `cash` and the typo `stratgey`
                reported 3% instead of 30% and the global gates passed.
    CORRECTION  A recognized enum; `cash` stays available as a documented
                rollback but is observation-only (GUARD_ACCOUNTING_MODE
                keeps CAPITAL blocked); anything unrecognized keeps the
                loss-preserving computation AND blocks.

A06 UNEXPLAINED CASH RESIDUAL
    INVARIANT   OBSERVABLE, RECONCILED and CAPITAL_ADMISSIBLE are three
                different things.
    ROOT CAUSE  A -0.10 movement under observation left the status
                RECONCILED and no accounting guard, so the global gates
                returned (True, None) while an unexplained adverse movement
                was open.
    CORRECTION  `GUARD_RESIDUAL_UNEXPLAINED`, raised from the FIRST
                observation beyond the rounding tolerance. The rounding
                epsilon stays a rounding epsilon; it is not allowed to
                become a tolerance for unexplained economic loss.

A07 MIGRATION / SEED APPLY NOT ATOMICALLY BOUND
    INVARIANT   A migration may never make equity, the HWM or the drawdown
                look safer because the evidence changed underneath it.
    ROOT CAUSE  `apply_seed` compared the operator's hash against
                `proposal["sha256"]` -- a field inside the mutable object it
                authenticated -- and never re-derived the proposal, so a
                settlement landing between proposal and apply was absorbed
                into the seed prefix.
    CORRECTION  The hash is RECOMPUTED from the proposal's own data; the
                proposal is bound to the source state root, the settlement
                frontier, the ledger generation, the cash evidence, the
                schema and the continuity head; and it is re-derived from
                live state immediately before applying.

A08 GATEKEEPER ACCEPTS INVALID VALIDATION EVIDENCE
    INVARIANT   Evidence that cannot be parsed, counted or dated is not
                evidence.
    ROOT CAUSE  `ran` was never read (zero tests passed as green); NaN and
                future timestamps both read as "fresh"; absent fields were
                treated as defaults; nothing bound the artifacts to the code
                or the model they described.
    CORRECTION  Strict schema, tests_run > 0, tests_passed == tests_run,
                finite plausible timestamps bounded in both directions, and
                two identity bindings (`code_identity`,
                `model_validation_sha256`).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import PRE_AT, AstraCase, trade                # noqa: E402

import equity_ledger as EL                                 # noqa: E402
import model_gatekeeper as gate                            # noqa: E402
from config import CFG, _p                                 # noqa: E402
from equity_ledger import EquityLedger                     # noqa: E402
from persistence import JsonStore                          # noqa: E402
from risk_manager import RiskManager                       # noqa: E402
from trade_logger import TradeLogger                       # noqa: E402


# ══════════════════════════════════════════════════════ A04
class DuplicateEconomicEventsAreRefused(AstraCase):

    def history(self):
        """+3 then -4: drawdown 30.769% on a HWM of 13."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        win = trade(tlog, ticker="KX-WIN")
        tlog.settle_trade(win["trade_id"], "yes", True, 3.0, 3.0)
        loss = trade(tlog, ticker="KX-LOSS")
        tlog.settle_trade(loss["trade_id"], "no", False, -4.0, -4.0)
        for i in range(3):
            led.observe(9.0, cycle_n=i + 1, quiet=True)
        win = next(t for t in tlog.trades if t["trade_id"] == win["trade_id"])
        return client, tlog, pos, led, win

    def test_the_baseline_drawdown_is_what_astra_measured(self):
        _, _, _, led, _ = self.history()
        self.assertAlmostEqual(led.risk_equity_reference(), 13.0, places=6)
        self.assertAlmostEqual(led.drawdown_pct(), 100.0 * 4.0 / 13.0, places=3)

    def test_a_replayed_profitable_trade_cannot_repair_the_drawdown(self):
        """Astra J_appended_duplicate_profitable_trade."""
        client, tlog, pos, led, win = self.history()
        before = led.drawdown_pct()
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        raw.append(json.loads(json.dumps(win)))            # same trade_id
        corrupt_json(_p(CFG.TRADES_FILE), raw)
        led2, tlog2, pos2 = self.reload()
        self.assertTrue(tlog2.duplicate_ids)
        self.assertTrue(led2.duplicate_events())
        self.assertIn(EL.GUARD_JOURNAL_INTEGRITY, led2.guards())
        self.assertFalse(led2.capital_eligible())
        self.assertEqual(led2.derive_status(), EL.STATUS_UNRECONCILED)
        self.assertGreaterEqual(led2.drawdown_pct(), before - 1e-9)

    def test_the_watermark_refuses_to_extend_over_a_duplicate(self):
        client, tlog, pos, led, win = self.history()
        evidenced = dict(led.state["journal_watermark"])
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        raw.append(json.loads(json.dumps(win)))
        corrupt_json(_p(CFG.TRADES_FILE), raw)
        led2, _, _ = self.reload()
        led2._advance_journal_watermark()
        self.assertEqual(led2.state["journal_watermark"]["settled_count"],
                         evidenced["settled_count"])

    def test_the_journal_refuses_to_write_a_duplicate_identity(self):
        client, tlog, pos = self.stack()
        first = trade(tlog, ticker="KX-DUP")
        with patch("uuid.uuid4") as uid:
            uid.return_value.hex = first["trade_id"] + "0" * 32
            with self.assertRaises(ValueError):
                trade(tlog, ticker="KX-DUP2")
        self.assertEqual(len(tlog.trades), 1)

    def test_a_duplicate_settlement_id_is_detected(self):
        client, tlog, pos = self.stack()
        self.reconciled_ledger(tlog, pos)
        a = trade(tlog, ticker="KX-S1")
        b = trade(tlog, ticker="KX-S2")
        tlog.settle_trade(a["trade_id"], "no", False, -1.0, -1.0)
        tlog.settle_trade(b["trade_id"], "no", False, -1.0, -1.0)
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        for r in raw:
            r["settlement_id"] = "stl-1"                    # same settlement
        corrupt_json(_p(CFG.TRADES_FILE), raw)
        led2, _, _ = self.reload()
        self.assertTrue(led2.duplicate_events())
        self.assertIn(EL.GUARD_JOURNAL_INTEGRITY, led2.guards())

    def test_duplication_survives_a_restart(self):
        client, tlog, pos, led, win = self.history()
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        raw.append(json.loads(json.dumps(win)))
        corrupt_json(_p(CFG.TRADES_FILE), raw)
        for _ in range(3):
            led2, _, _ = self.reload()
            self.assertIn(EL.GUARD_JOURNAL_INTEGRITY, led2.guards())

    def test_positive_control_a_correction_is_a_distinct_event(self):
        """A correction adjusts economics through its OWN identity; it is
        not a duplicate and must keep working."""
        client, tlog, pos, led, win = self.history()
        raw = JsonStore.load(_p(CFG.TRADES_FILE), [])
        raw.append({"schema": TradeLogger.SCHEMA, "correction_id": "corr-1",
                    "corrects_trade_id": win["trade_id"], "net_pnl": -0.5,
                    "gross_pnl": -0.5, "ticker": "KX-WIN", "state": "settled",
                    "timestamp": PRE_AT, "settled_at": PRE_AT})
        corrupt_json(_p(CFG.TRADES_FILE), raw)
        led2, tlog2, _ = self.reload()
        self.assertFalse(led2.duplicate_events())
        self.assertNotIn(EL.GUARD_JOURNAL_INTEGRITY, led2.guards())
        self.assertAlmostEqual(led2.realized_pnl_cum(), -1.5, places=6)

    def test_positive_control_two_distinct_trades_are_not_duplicates(self):
        client, tlog, pos, led, win = self.history()
        self.assertFalse(led.duplicate_events())
        self.assertNotIn(EL.GUARD_JOURNAL_INTEGRITY, led.guards())


# ══════════════════════════════════════════════════════ A05
class AccountingModeIsARecognizedEnum(AstraCase):

    def blown(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        risk = RiskManager(tlog, pos, capital=10.0)
        risk.equity = led
        return client, tlog, pos, led, risk

    def test_an_unknown_mode_never_falls_back_to_the_cash_denominator(self):
        """Astra GATE_unknown_risk_accounting_mode: the typo `stratgey`
        reported 3% instead of 30% after a deposit."""
        client, tlog, pos, led, risk = self.blown()
        for cycle in range(10, 16):
            led.observe(10.0, cycle_n=cycle, quiet=True)    # +3 deposit
        with patch.object(CFG, "RISK_EQUITY_MODE", "stratgey"):
            self.assertGreaterEqual(risk.rolling_drawdown_pct(), 30.0 - 1e-6)
            self.assertIn(EL.GUARD_ACCOUNTING_MODE, led.guards())
            self.assertFalse(led.capital_eligible())
            self.assertFalse(EL.accounting_mode_valid())

    def test_the_cash_rollback_is_recognized_but_never_capital_admissible(self):
        """Astra GATE_cash_rollback_after_deposit. `cash` stays available for
        observation; it may not authorize CAPITAL, because a deposit repairs
        a cash-denominated drawdown."""
        client, tlog, pos, led, risk = self.blown()
        for cycle in range(10, 16):
            led.observe(10.0, cycle_n=cycle, quiet=True)
        with patch.object(CFG, "RISK_EQUITY_MODE", "cash"):
            self.assertTrue(EL.accounting_mode_valid())
            self.assertFalse(EL.accounting_mode_capital_admissible())
            self.assertIn(EL.GUARD_ACCOUNTING_MODE, led.guards())
            self.assertFalse(led.capital_eligible())

    def test_an_empty_mode_blocks(self):
        client, tlog, pos, led, risk = self.blown()
        for value in ("", "   ", None, "STRATEGY_V2"):
            with self.subTest(mode=value):
                with patch.object(CFG, "RISK_EQUITY_MODE", value):
                    self.assertIn(EL.GUARD_ACCOUNTING_MODE, led.guards())
                    self.assertFalse(led.capital_eligible())

    def test_a_deposit_cannot_repair_strategy_drawdown_in_any_mode(self):
        client, tlog, pos, led, risk = self.blown()
        for cycle in range(10, 16):
            led.observe(10.0, cycle_n=cycle, quiet=True)
        for mode in ("strategy", "cash", "unknown-value"):
            with self.subTest(mode=mode):
                with patch.object(CFG, "RISK_EQUITY_MODE", mode):
                    self.assertAlmostEqual(led.drawdown_pct(), 30.0, places=6)
        # ...and the two non-strategy modes additionally refuse CAPITAL
        for mode in ("cash", "unknown-value"):
            with self.subTest(mode=mode, check="capital"):
                with patch.object(CFG, "RISK_EQUITY_MODE", mode):
                    self.assertFalse(led.capital_eligible())

    def test_positive_control_strategy_mode_is_admissible(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        with patch.object(CFG, "RISK_EQUITY_MODE", "strategy"):
            self.assertTrue(EL.accounting_mode_capital_admissible())
            self.assertNotIn(EL.GUARD_ACCOUNTING_MODE, led.guards())
            self.assertTrue(led.capital_eligible())


# ══════════════════════════════════════════════════════ A06
class AnUnexplainedResidualIsNotCapitalAdmissible(AstraCase):

    def test_a_negative_residual_under_observation_blocks_capital(self):
        """Astra GATE_pending_negative_residual_must_block: seed 10, balance
        9.90, status RECONCILED, no accounting guard, gates (True, None)."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.observe(9.90, cycle_n=1, quiet=True)
        self.assertIsNotNone(led.state["pending"])
        self.assertIsNotNone(led._unexplained_residual())
        self.assertIn(EL.GUARD_RESIDUAL_UNEXPLAINED, led.guards())
        self.assertFalse(led.capital_eligible())

    def test_the_rounding_tolerance_stays_a_rounding_tolerance(self):
        """A movement WITHIN the API-rounding epsilon is not an unexplained
        economic loss; a movement beyond it is, from the first observation."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.observe(10.0 - led.eps_base / 2, cycle_n=1, quiet=True)
        self.assertIsNone(led._unexplained_residual())
        self.assertTrue(led.capital_eligible())

    def test_it_blocks_a_rebase_too(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        led.observe(6.80, cycle_n=9, quiet=True)
        failures = led.rebase_preconditions(
            {"drawdown_firing": True, "reconcile_status": "MATCH",
             "open_positions": 0, "in_flight_orders": 0, "quiescent": True,
             "evidence_unstable": None, "bound_state": led.bound_state(),
             "orders": {"local_open": [], "pending_intents": [],
                        "resolution_halt": False, "broker_open": 0,
                        "broker_open_ids": [], "broker_error": None,
                        "disagreement": False}})
        self.assertTrue(any("unexplained cash residual" in f for f in failures),
                        failures)

    def test_it_survives_a_restart(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.observe(9.90, cycle_n=1, quiet=True)
        led2, _, _ = self.reload()
        self.assertIn(EL.GUARD_RESIDUAL_UNEXPLAINED, led2.guards())

    def test_positive_control_a_reconciled_balance_is_admissible(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.observe(10.0, cycle_n=1, quiet=True)
        self.assertNotIn(EL.GUARD_RESIDUAL_UNEXPLAINED, led.guards())
        self.assertTrue(led.capital_eligible())


# ══════════════════════════════════════════════════════ A07
class MigrationIsBoundToTheEvidenceItWasReviewedOn(AstraCase):

    def unseeded(self):
        client, tlog, pos = self.stack()
        return client, tlog, pos, EquityLedger(tlog, pos, env="prod")

    def test_a_settlement_between_proposal_and_apply_is_refused(self):
        """Astra M_settlement_after_proposal / M_boot_settlement_race: a -3
        loss landing after the proposal became the new seed prefix, and
        equity/HWM stayed at 10 with a drawdown of 0."""
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        t = trade(tlog, ticker="KX-LATE")
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        self.assertFalse(led.apply_seed(proposal, proposal["sha256"]))
        self.assertFalse(led.seeded)
        # regenerated on the real evidence, the loss is visible
        fresh = led.propose_seed(10.0, PRE_AT, "ref", 7.0)
        self.assertIn("settlements after the pre-flow observation",
                      fresh["unproven"])

    def test_a_modified_proposal_cannot_keep_its_accepted_hash(self):
        """Astra M_modified_proposal: the hash lived INSIDE the object it
        was supposed to authenticate."""
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        accepted = proposal["sha256"]
        proposal["hwm_0"] = 5.0                            # safer-looking
        proposal["strategy_equity_0"] = 5.0
        self.assertEqual(proposal["sha256"], accepted)     # field unchanged
        self.assertNotEqual(led.seed_proposal_sha(proposal), accepted)
        self.assertFalse(led.apply_seed(proposal, accepted))
        self.assertFalse(led.seeded)

    def test_the_proposal_binds_the_state_it_was_computed_on(self):
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        for key in ("bound_state", "settlement_frontier", "schema_version",
                    "continuity_head"):
            self.assertIn(key, proposal)
        self.assertIn("kalshi_trades.json", proposal["bound_state"])
        self.assertIn("orders_state.json", proposal["bound_state"])
        self.assertIn("pending_intents.json", proposal["bound_state"])

    def test_a_changed_orders_file_invalidates_the_proposal(self):
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        JsonStore.save(_p(CFG.ORDERS_FILE), {"brk-9": {"order_id": "brk-9"}})
        self.assertFalse(led.apply_seed(proposal, proposal["sha256"]))
        self.assertFalse(led.seeded)

    def test_migration_is_idempotent(self):
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        self.assertTrue(led.apply_seed(proposal, proposal["sha256"]))
        hwm = led.risk_equity_reference()
        self.assertFalse(led.apply_seed(proposal, proposal["sha256"]))
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.risk_equity_reference(), hwm, places=9)

    def test_migration_never_makes_the_drawdown_look_safer(self):
        client, tlog, pos, led = self.unseeded()
        t = trade(tlog, ticker="KX-EARLY")
        tlog.settle_trade(t["trade_id"], "no", False, -3.0, -3.0)
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 7.0)
        self.assertTrue(led.apply_seed(proposal, proposal["sha256"]))
        self.assertAlmostEqual(led.risk_equity_reference(), 13.0, places=6)
        self.assertGreater(led.drawdown_pct(), 0.0)

    def test_positive_control_a_still_state_applies(self):
        client, tlog, pos, led = self.unseeded()
        proposal = led.propose_seed(10.0, PRE_AT, "ref", 10.0)
        self.assertTrue(led.apply_seed(proposal, proposal["sha256"]))
        self.assertTrue(led.seeded)
        self.assertAlmostEqual(led.strategy_equity(), 10.0, places=6)


# ══════════════════════════════════════════════════════ A08
class GatekeeperRefusesInvalidEvidence(unittest.TestCase):
    """Runs in a throwaway cwd: the gate reads both artifacts relatively."""

    PROMOTION_OK = {"NO_LIVE_PROMOTION": "0", "MODEL_APPROVED_FOR_LIVE": "YES"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astra-gate-")
        self._cwd = os.getcwd()
        self._env = {k: os.environ.get(k) for k in self.PROMOTION_OK}
        os.environ.update(self.PROMOTION_OK)
        os.chdir(self.tmp)
        self.addCleanup(self._restore)
        self.write("model_validation.json",
                   {"generated_ts": time.time(), "approved": True,
                    "model_version": "btc15m-baseline-0.1",
                    "criteria": [{"name": n, "passed": True} for n in sorted(
                        gate.MODEL_CRITERIA["btc15m-baseline-0.1"])]})
        self.write("test_report.json", self.green())

    def _restore(self):
        os.chdir(self._cwd)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, payload):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def green(self, **over):
        r = {"generated_ts": time.time(), "ran": 1042, "failures": 0,
             "errors": 0, "skipped": 0, "failed_tests": [],
             "code_identity": gate.code_identity(),
             "model_validation_sha256":
                 gate.file_sha256(os.path.join(self.tmp,
                                               "model_validation.json"))}
        r.update(over)
        return r

    def refused(self, needle):
        ok, failed = gate.check_live_allowed()
        self.assertFalse(ok, f"LIVE autorise alors que: {needle}")
        self.assertIn(needle, " | ".join(failed))
        return failed

    def test_positive_control_a_complete_bound_pair_allows_live(self):
        ok, failed = gate.check_live_allowed()
        self.assertTrue(ok, f"le controle echoue: {failed}")

    def test_zero_tests_is_not_a_green_suite(self):
        """Astra GATEKEEPER_zero_tests."""
        self.write("test_report.json", self.green(ran=0))
        self.refused("ZERO test execute")

    def test_a_nan_test_timestamp_is_refused(self):
        """Astra GATEKEEPER_nan_test_timestamp. json.dump writes bare NaN,
        which json.load accepts -- so the value really does reach the gate."""
        self.write("test_report.json", self.green(generated_ts=float("nan")))
        self.refused("n'est pas un nombre fini")

    def test_an_infinite_test_timestamp_is_refused(self):
        self.write("test_report.json", self.green(generated_ts=float("inf")))
        self.refused("n'est pas un nombre fini")

    def test_a_future_test_timestamp_is_refused(self):
        """Astra GATEKEEPER_future_test_timestamp: ten days ahead read as
        'fresh' because the age was negative."""
        self.write("test_report.json",
                   self.green(generated_ts=time.time() + 10 * 86400))
        self.refused("dans le futur")

    def test_a_nan_model_timestamp_is_refused(self):
        """Astra GATEKEEPER_nan_model_timestamp."""
        self.write("model_validation.json",
                   {"generated_ts": float("nan"), "approved": True,
                    "model_version": "v1"})
        self.write("test_report.json", self.green())
        self.refused("n'est pas un nombre fini")

    def test_a_future_model_timestamp_is_refused(self):
        self.write("model_validation.json",
                   {"generated_ts": time.time() + 5 * 86400, "approved": True,
                    "model_version": "v1"})
        self.write("test_report.json", self.green())
        self.refused("dans le futur")

    def test_absent_or_mistyped_counters_are_refused_not_defaulted(self):
        for field, value in (("ran", None), ("ran", "1042"), ("ran", True),
                             ("failures", None), ("failures", "0"),
                             ("errors", []), ("skipped", -1)):
            with self.subTest(field=field, value=value):
                r = self.green()
                if value is None:
                    r.pop(field)
                else:
                    r[field] = value
                self.write("test_report.json", r)
                ok, failed = gate.check_live_allowed()
                self.assertFalse(ok, f"{field}={value!r} a laisse passer")

    def test_skipped_tests_are_not_passed_tests(self):
        self.write("test_report.json", self.green(skipped=3))
        self.refused("tests non verts")

    def test_a_report_from_another_tree_is_refused(self):
        """Stale artifact for different code."""
        self.write("test_report.json", self.green(code_identity="0" * 64))
        self.refused("AUTRE arbre")

    def test_a_report_without_the_code_binding_is_refused(self):
        r = self.green()
        r.pop("code_identity")
        self.write("test_report.json", r)
        self.refused("non lie au code evalue")

    def test_a_manifest_changed_after_the_tests_is_refused(self):
        """The tests must have exercised THIS manifest."""
        self.write("model_validation.json",
                   {"generated_ts": time.time(), "approved": True,
                    "model_version": "btc15m-baseline-0.2"})
        self.refused("artefact perime")

    def test_a_non_object_report_is_refused(self):
        self.write("test_report.json", ["ran", 1042])
        self.refused("objet attendu")

    def test_a_truncated_report_is_refused(self):
        with open(os.path.join(self.tmp, "test_report.json"), "w") as f:
            f.write('{"ran": 104')
        self.refused("absent ou illisible")

    def test_unmet_model_criteria_block(self):
        self.write("model_validation.json",
                   {"generated_ts": time.time(), "approved": True,
                    "model_version": "v1",
                    "criteria": [{"name": "brier", "passed": False}]})
        self.refused("criteres de validation non satisfaits")

    def test_the_shipped_manifest_is_still_correctly_refused(self):
        """The artifact actually in the repository stays a refusal."""
        os.chdir(self._cwd)
        ok, failed = gate.check_live_allowed()
        self.assertFalse(ok)
        self.assertTrue(any("non approuve" in f for f in failed), failed)


if __name__ == "__main__":
    unittest.main()
