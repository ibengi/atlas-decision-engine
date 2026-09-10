# -*- coding: utf-8 -*-
"""A01-A12 -- the second Astra pass: what the first remediation left open.

Each class below reproduces the specific hole Astra measured on 3af848e and
asserts the invariant that now closes it. All of them drive PRODUCTION
classes; none of them stub the module under test.

    A01  a coordinated restore, and the crash between two durable steps
    A02  contradictory broker rows netting to zero and reading as MATCH
    A03  rebase committing against exposure that moved after validation
    A04  the same economic order re-recorded under a new local id
    A06  an adverse residual dropped by a noisy window or a grown tolerance
    A07  a settlement absorbed into an already-authorized migration
    A08  contradictory / malformed / duplicated approval evidence
    A11  two distinct movements of equal size merged into one flow
    A05, A12  currently PASS: proved here not to regress
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import PRE_AT, AstraCase, Client, trade  # noqa: E402

import equity_ledger as EL                             # noqa: E402
import model_gatekeeper as MG                          # noqa: E402
from config import CFG, _p                             # noqa: E402
from equity_ledger import EquityLedger                 # noqa: E402
from order_manager import OrderManager                 # noqa: E402
from persistence import JsonStore, PersistenceSentinel  # noqa: E402
from position_manager import PositionManager           # noqa: E402
from trade_logger import TradeLogger                   # noqa: E402


# ── A02 ────────────────────────────────────────────────────────────────
class ContradictoryPositionsAreNeverFlat(AstraCase):
    """A portfolio the broker describes contradictorily is UNKNOWN. The
    arithmetic that made +1 and -1 cancel produced a MATCH on a market the
    engine could not actually account for."""

    def rows(self, *pairs):
        return [{"ticker": t, "position": q} for t, q in pairs]

    def verdict(self, broker_rows):
        client = Client(positions=broker_rows)
        tlog = TradeLogger()
        pos = PositionManager(client, tlog)
        return pos.verify_against_broker()

    def test_two_contradictory_rows_for_one_ticker_are_unknown(self):
        report = self.verdict(self.rows(("KXBTC15M-A", 1), ("KXBTC15M-A", -1)))
        self.assertNotEqual(report["status"], "MATCH",
                            "a contradictory portfolio read as flat")
        self.assertEqual(report["status"], "UNKNOWN")

    def test_a_duplicated_page_is_unknown_not_double(self):
        report = self.verdict(self.rows(("KXBTC15M-A", 2), ("KXBTC15M-A", 2)))
        self.assertEqual(report["status"], "UNKNOWN")

    def test_duplicate_rows_with_inconsistent_quantity_are_unknown(self):
        report = self.verdict(self.rows(("KXBTC15M-A", 2), ("KXBTC15M-A", 3)))
        self.assertEqual(report["status"], "UNKNOWN")

    def test_a_genuinely_flat_broker_still_matches(self):
        """The control: uniqueness is enforced, not emptiness."""
        report = self.verdict([])
        self.assertEqual(report["status"], "MATCH")

    def test_distinct_tickers_are_unaffected(self):
        report = self.verdict(self.rows(("KXBTC15M-A", 0), ("KXBTC15M-B", 0)))
        self.assertEqual(report["status"], "MATCH")

    def test_the_contradiction_survives_a_restart(self):
        rows = self.rows(("KXBTC15M-A", 1), ("KXBTC15M-A", -1))
        self.assertEqual(self.verdict(rows)["status"], "UNKNOWN")
        self.assertEqual(self.verdict(rows)["status"], "UNKNOWN")


# ── A04 ────────────────────────────────────────────────────────────────
class EconomicIdentitySurvivesANewLocalId(AstraCase):
    """`trade_id` is a uuid4 minted locally: a replay simply gets a new one.
    The broker's own identifiers are what a replay cannot change."""

    def settled_row(self, tlog, order_id, pnl, ticker="KXBTC15M-A"):
        t = trade(tlog, ticker=ticker, order_id=order_id)
        tlog.settle_trade(t["trade_id"], "yes", True, pnl, pnl)
        return t

    def test_the_same_broker_order_under_a_new_local_id_is_a_duplicate(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.settled_row(tlog, "BROKER-ORDER-1", 5.0)
        self.assertEqual(led.duplicate_events(), [])
        # the replay: identical economics, brand-new local id
        replay = dict(tlog.trades[-1])
        replay["trade_id"] = "a-brand-new-local-id"
        tlog.trades.append(replay)
        tlog.flush()
        self.assertTrue(led.duplicate_events(),
                        "a replayed broker order was invisible")
        self.assertIn(EL.GUARD_JOURNAL_INTEGRITY, led.guards())
        self.assertFalse(led.capital_eligible())

    def test_two_legitimate_equal_fills_are_not_merged(self):
        """The opposite error: distinct orders of identical size and price
        must stay two events."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.settled_row(tlog, "BROKER-ORDER-1", 2.0, ticker="KXBTC15M-A")
        self.settled_row(tlog, "BROKER-ORDER-2", 2.0, ticker="KXBTC15M-B")
        self.assertEqual(led.duplicate_events(), [])
        self.assertEqual(len(led.settled_unique()), 2)

    def test_a_replayed_fill_cannot_improve_the_drawdown(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)                       # a real -3 loss
        drawdown_before = led.drawdown_pct()
        gain = dict(tlog.trades[-1])
        gain.update({"trade_id": "replay-id", "net_pnl": 3.0,
                     "gross_pnl": 3.0, "result": "yes", "won": True})
        tlog.trades.append(gain)
        tlog.flush()
        self.assertTrue(led.duplicate_events())
        self.assertGreaterEqual(led.drawdown_pct(), drawdown_before - 1e-9,
                                "a replay improved the reported drawdown")
        self.assertFalse(led.capital_eligible())

    def test_a_correction_is_a_distinct_event_not_a_duplicate(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.settled_row(tlog, "BROKER-ORDER-1", 5.0)
        correction = dict(tlog.trades[-1])
        correction.update({"trade_id": "corr-1", "correction_id": "CORR-1",
                           "net_pnl": -5.0})
        tlog.trades.append(correction)
        tlog.flush()
        self.assertEqual(led.duplicate_events(), [],
                         "a correction was mistaken for a duplicate")


# ── A06 ────────────────────────────────────────────────────────────────
class AdverseCashIsNotForgivenByNoise(AstraCase):
    """Measurement tolerance and economic admissibility are different
    questions. Epsilon exists so the engine does not alert on API rounding;
    it is not permission to forget a loss."""

    def seeded(self):
        client, tlog, pos = self.stack()
        return self.reconciled_ledger(tlog, pos), tlog, pos

    def test_an_adverse_residual_during_a_noisy_window_is_recorded(self):
        led, tlog, pos = self.seeded()
        led.observe(10.0 - 2.0, cycle_n=1, quiet=False)   # never calm
        self.assertIsNotNone(led.state.get("adverse_residual_floor"),
                             "a non-quiet cycle discarded adverse evidence")
        self.assertIsNotNone(led._unexplained_residual())
        self.assertIn(EL.GUARD_RESIDUAL_UNEXPLAINED, led.guards())
        self.assertFalse(led.capital_eligible())

    def test_a_permanently_noisy_account_still_blocks(self):
        led, tlog, pos = self.seeded()
        for cycle in range(1, 12):
            led.observe(10.0 - 2.0, cycle_n=cycle, quiet=False)
        self.assertFalse(led.capital_eligible())
        self.assertIn(EL.GUARD_RESIDUAL_UNEXPLAINED, led.guards())

    def test_the_tolerance_cannot_grow_past_the_cap(self):
        led, tlog, pos = self.seeded()
        led.state["anchor"] = {"settled_since_anchor": 100000, "at": "t"}
        self.assertLessEqual(led._epsilon(), EquityLedger.EPSILON_CAP + 1e-12,
                             "the rounding tolerance grew without bound")

    def test_a_grown_tolerance_never_absorbs_an_adverse_residual(self):
        led, tlog, pos = self.seeded()
        led.state["anchor"] = {"settled_since_anchor": 40, "at": "t"}
        loss = -(abs(led.eps_base) * 5)
        led.observe(10.0 + loss, cycle_n=1, quiet=True)
        kinds = [f["kind"] for f in led.state["flows"]]
        self.assertNotIn(EL.FLOW_ROUNDING, kinds,
                         "an adverse movement was auto-classified as rounding")
        self.assertFalse(led.capital_eligible())

    def test_a_positive_rounding_difference_is_still_auto_classified(self):
        """The control: symmetric noise is still absorbed, so the change is
        about ADVERSITY, not about refusing everything."""
        led, tlog, pos = self.seeded()
        led.observe(10.0 + abs(led.eps_base) / 2.0, cycle_n=1, quiet=True)
        self.assertIn(EL.FLOW_ROUNDING,
                      [f["kind"] for f in led.state["flows"]])

    def test_the_floor_survives_a_restart(self):
        led, tlog, pos = self.seeded()
        led.observe(10.0 - 2.0, cycle_n=1, quiet=False)
        self.assertTrue(led.save())
        again, _, _ = self.reload()
        self.assertIsNotNone(again.state.get("adverse_residual_floor"))
        self.assertFalse(again.capital_eligible())


# ── A11 ────────────────────────────────────────────────────────────────
class EqualMovementsAreNotTheSameMovement(AstraCase):

    def seeded(self):
        client, tlog, pos = self.stack()
        return self.reconciled_ledger(tlog, pos), tlog, pos

    def unclassified(self, led):
        return [f for f in led.state["flows"]
                if f["kind"] == EL.FLOW_UNCLASSIFIED]

    def test_the_same_residual_observed_repeatedly_stays_one_flow(self):
        led, tlog, pos = self.seeded()
        for cycle in range(1, 8):
            led.observe(10.0 - 25.0, cycle_n=cycle, quiet=True)
        self.assertEqual(len(self.unclassified(led)), 1,
                         "one movement observed repeatedly multiplied")

    def test_two_distinct_equal_movements_are_two_flows(self):
        led, tlog, pos = self.seeded()
        for cycle in range(1, 6):
            led.observe(10.0 - 25.0, cycle_n=cycle, quiet=True)
        self.assertEqual(len(self.unclassified(led)), 1)
        first = self.unclassified(led)[0]
        # ... a long gap, then a SECOND withdrawal of exactly the same size
        for cycle in range(400, 406):
            led.observe(10.0 - 50.0, cycle_n=cycle, quiet=True)
        rows = self.unclassified(led)
        self.assertGreaterEqual(
            len(rows), 2,
            "two legitimate movements of equal size were merged into one")
        self.assertIn(first["id"], [r["id"] for r in rows])


# ── A07 / A03: the commit boundary ─────────────────────────────────────
class NothingIsAbsorbedAtTheCommitBoundary(AstraCase):

    def settle_late(self, tlog, pnl=-3.0):
        t = trade(tlog, ticker="KXBTC15M-LATE")
        tlog.settle_trade(t["trade_id"], "no", False, pnl, pnl)
        return t

    def test_a_settlement_after_the_proposal_refuses_the_migration(self):
        """Between authorization and apply: the proposal no longer describes
        the evidence, so it is refused rather than recomputed silently."""
        client, tlog, pos = self.stack()
        led = EquityLedger(tlog, pos, env="prod")
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        self.settle_late(tlog)
        self.assertFalse(led.apply_seed(prop, prop["sha256"]),
                         "a migration absorbed a settlement it never saw")
        self.assertFalse(led.seeded, "the ledger was left half-migrated")
        again, _, _ = self.reload()
        self.assertFalse(again.seeded)

    def test_a_settlement_at_the_commit_boundary_is_never_absorbed(self):
        """The narrowest window: the settlement lands DURING the durable
        write. Whatever the outcome, the loss must remain visible -- the
        migration may not swallow it into the seed prefix and report a
        drawdown of zero."""
        client, tlog, pos = self.stack()
        led = EquityLedger(tlog, pos, env="prod")
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        real_save = JsonStore.save
        landed = {"done": False}

        def settle_then_save(path, data, *a, **kw):
            if not landed["done"]:
                landed["done"] = True
                self.settle_late(tlog)
            return real_save(path, data, *a, **kw)

        with patch.object(JsonStore, "save", side_effect=settle_then_save):
            applied = led.apply_seed(prop, prop["sha256"])
        self.assertTrue(landed["done"], "the boundary was never exercised")
        if not applied:
            self.assertFalse(led.seeded)
            return
        # Applied: then the late loss must sit OUTSIDE the seed prefix and
        # be reported, not absorbed.
        self.assertGreaterEqual(led.drawdown_pct(), 30.0 - 1e-6,
                                "the late settlement was absorbed into the "
                                "migration and the drawdown read clean")
        self.assertLessEqual(led.strategy_equity(), 7.0 + 1e-9)
        again, _, _ = self.reload()
        self.assertGreaterEqual(again.drawdown_pct(), 30.0 - 1e-6)

    def test_a_clean_migration_still_applies(self):
        """The control: the boundary check refuses CHANGE, not migration."""
        client, tlog, pos = self.stack()
        led = EquityLedger(tlog, pos, env="prod")
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        self.assertTrue(led.seeded)


# ── A08 ────────────────────────────────────────────────────────────────
class GatekeeperRefusesContradictoryEvidence(AstraCase):

    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        os.chdir(self._tmp)
        self.addCleanup(lambda: os.chdir(self._cwd))

    def write(self, name, text):
        with open(os.path.join(self._tmp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def refusal(self):
        ok, failed = MG.check_live_allowed()
        self.assertFalse(ok)
        return " | ".join(failed)

    def test_duplicate_json_keys_are_refused_not_last_wins(self):
        self.write("model_validation.json",
                   '{"approved": false, "approved": true, '
                   '"model_version": "v", "generated_ts": 1}')
        self.assertIn("dupliquee", self.refusal())

    def test_a_malformed_criterion_refuses_instead_of_raising(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "criteria": ["not-an-object"]}))
        reasons = self.refusal()          # must not raise
        self.assertIn("critere", reasons)

    def test_a_null_criterion_refuses_instead_of_raising(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "criteria": [None]}))
        self.assertIn("critere", self.refusal())

    def test_approved_while_a_criterion_fails_is_refused(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "criteria": [{"name": "brier", "passed": False}]}))
        self.assertIn("non satisfaits", self.refusal())

    def test_a_criterion_contradicting_its_own_measurement_is_refused(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "criteria": [{"name": "n", "required": ">=300",
                           "observed": 108, "passed": True}]}))
        self.assertIn("incoherent", self.refusal())

    def test_the_same_criterion_stated_twice_differently_is_refused(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "criteria": [{"name": "brier", "passed": True},
                          {"name": "brier", "passed": False}]}))
        self.assertIn("brier", self.refusal())

    def test_an_approval_with_no_criteria_at_all_is_refused(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1}))
        self.assertIn("sans aucun", self.refusal())

    def test_approved_while_blocked_is_refused(self):
        self.write("model_validation.json", json.dumps(
            {"approved": True, "model_version": "v", "generated_ts": 1,
             "approval_blocked_reason": "sample too small",
             "criteria": [{"name": "n", "passed": True}]}))
        self.assertIn("contradictoire", self.refusal())

    def test_the_gatekeeper_never_raises(self):
        self.write("model_validation.json", "{not json at all")
        ok, failed = MG.check_live_allowed()
        self.assertFalse(ok)
        self.assertTrue(failed)


# ── A05 and A12: proved not to regress ─────────────────────────────────
class PreviouslyPassingInvariantsDoNotRegress(AstraCase):

    def test_A05_an_unknown_accounting_mode_is_never_a_fallback(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        with patch.object(CFG, "RISK_EQUITY_MODE", "not-a-mode"):
            self.assertFalse(EL.accounting_mode_valid())
            self.assertIn(EL.GUARD_ACCOUNTING_MODE, led.guards())
            self.assertFalse(led.capital_eligible())

    def test_A05_the_cash_rollback_mode_is_observation_only(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        with patch.object(CFG, "RISK_EQUITY_MODE", "cash"):
            self.assertTrue(EL.accounting_mode_valid())
            self.assertFalse(EL.accounting_mode_capital_admissible())
            self.assertFalse(led.capital_eligible())

    def test_A12_read_only_tooling_writes_nothing(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.assertTrue(led.save())
        before = self.snapshot_dir()
        ro = EquityLedger.load_readonly(tlog, pos, env="prod")
        ro.snapshot()
        ro.guards()
        ro.banner_line()
        self.assertFalse(ro.save(), "a read-only instance persisted state")
        self.assertEqual(self.snapshot_dir(), before,
                         "read-only tooling mutated DATA_DIR")

    def test_A12_a_read_only_instance_refuses_to_commit(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        led.save()
        before = self.snapshot_dir()
        ro = EquityLedger.load_readonly(tlog, pos, env="prod")
        prepared = json.loads(json.dumps(ro.state))
        prepared["hwm"]["risk_equity_reference"] = 1.0
        self.assertFalse(ro._commit(prepared))
        self.assertEqual(self.snapshot_dir(), before)


# ── A01: what the chain closes, and what it honestly does not ──────────
class WholeVolumeRollbackIsClassifiedNotClaimed(AstraCase):
    """The chain is a FLOOR that a partial rollback cannot rewind. A rollback
    of the whole volume takes the floor with it, and no local artefact can
    outrank that. The point of this class is that the limitation is stated
    and tested, not papered over."""

    def test_the_classification_is_explicit(self):
        import continuity
        self.assertEqual(continuity.ROLLBACK_RESISTANCE,
                         "MITIGATED_WITH_LIMITATION")
        self.assertEqual(continuity.WHOLE_VOLUME_ROLLBACK,
                         "EXTERNAL_AUTHORITY_REQUIRED")
        self.assertFalse(continuity.EXTERNAL_AUTHORITY_CONTRACT["implemented"],
                         "an unimplemented authority must not read as built")

    def test_a_partial_restore_that_leaves_the_chain_fails_closed(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        before = self.snapshot_dir()
        self.lose(tlog, led)
        self.assertTrue(led.save())
        # roll back the ledger AND the journal, leave the chain in place
        self.restore_dir(before, only=[EL.LEDGER_FILE, CFG.TRADES_FILE])
        again, _, _ = self.reload()
        self.assertIn(EL.GUARD_CONTINUITY, again.guards(),
                      "a coordinated restore was not detected")
        self.assertFalse(again.capital_eligible())

    def test_a_whole_volume_restore_is_not_claimed_to_be_detected(self):
        """Honesty test. Every local file goes back together, including the
        chain. The engine cannot see it, and the code must not pretend it
        can: what it must do instead is say so in the classification above
        and route external evidence through the attestation path."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        before = self.snapshot_dir()
        self.lose(tlog, led)
        self.assertTrue(led.save())
        for name in os.listdir(self._tmp):                # wipe everything
            path = os.path.join(self._tmp, name)
            if os.path.isfile(path):
                os.unlink(path)
        self.restore_dir(before)                          # whole volume back
        again, _, _ = self.reload()
        undetected = EL.GUARD_CONTINUITY not in again.guards()
        import continuity
        if undetected:
            self.assertEqual(continuity.WHOLE_VOLUME_ROLLBACK,
                             "EXTERNAL_AUTHORITY_REQUIRED",
                             "the case is undetected AND unclassified")
        # Either way, the attestation path is the only door an external
        # authority may use, and it is closed to a blocked ledger.
        self.assertIn("apply_attestation",
                      continuity.EXTERNAL_AUTHORITY_CONTRACT["entry_point"])
