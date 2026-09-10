"""New A21+ interactions; real persistence classes, isolated synthetic data."""
import copy
import os
from pathlib import Path
from unittest.mock import patch

from test_engine_authority import AuthorityCase
from config import CFG, _p
from equity_ledger import EquityLedger
from persistence import JsonStore, PersistenceSentinel
from position_manager import PositionManager
from recovery import complete_verified_recovery
from state_authority import (AuthorityError, checkpoint, root_lock, pending_path,
                             recovery_problem, WriterLease)
from trade_logger import TradeLogger


class CrossComponent(AuthorityCase):
    def test_transport_disables_mutation_retry_and_retains_unknown_outcome(self):
        from types import SimpleNamespace
        from transport_intent import durable_transport
        from kalshi_client import KalshiAPIError
        self.prove()
        calls = []
        client = SimpleNamespace(env="prod", continuity_authority=self.authority,
                                 _assert_broker_write_allowed=lambda _: None)
        def local_transport(_client, method, path, *, retries, **kwargs):
            calls.append({"method": method, "retries": retries, "request": kwargs})
            return {"synthetic_acknowledgement": True}
        transport = durable_transport(local_transport)
        transport(client, "POST", "/synthetic-operation", retries=9, json={"quantity": 1})
        self.assertEqual(calls[0]["retries"], 0)
        with self.assertRaises(KalshiAPIError):
            transport(client, "DELETE", "/different-synthetic-operation")
        self.assertEqual(len(calls), 1)
        self.assertIn("transport_outcome_unresolved", self.ledger.guards())

    def test_authentication_kwargs_never_enter_the_intent_store(self):
        from types import SimpleNamespace
        from transport_intent import durable_transport
        from kalshi_client import KalshiAPIError
        client = SimpleNamespace(_assert_broker_write_allowed=lambda _: None)
        before = self.image()
        def unexpected(*args, **kwargs):
            self.fail("transport must not be called")
        with self.assertRaises(KalshiAPIError):
            durable_transport(unexpected)(client, "POST", "/synthetic-operation",
                headers={"Authorization": "synthetic-placeholder"})
        self.assertEqual(self.image(), before)

    def test_an_unrelated_correction_cannot_classify_an_unexplained_loss(self):
        t = self.loss()
        for n in range(5):
            self.ledger.observe(6., cycle_n=n)
        flow = self.ledger.unclassified_flows()[0]
        before = copy.deepcopy(self.ledger.state)
        correction = {"correction_id": "different-loss", "corrects_trade_id": t["trade_id"], "net_pnl": -.5}
        with patch.object(self.tlog, "correction_rows", return_value=[correction]):
            self.assertFalse(self.ledger.classify_flow(flow["id"], "loss", correction_id="different-loss"))
        self.assertEqual(self.ledger.state, before)

    def test_an_existing_correction_cannot_explain_a_later_equal_residual(self):
        t = self.loss()
        correction = {"correction_id": "prior-loss", "corrects_trade_id": t["trade_id"], "net_pnl": -1.}
        with patch.object(self.tlog, "correction_rows", return_value=[correction]):
            for n in range(5):
                self.ledger.observe(6., cycle_n=n)
            flow = self.ledger.unclassified_flows()[0]
            self.assertFalse(self.ledger.classify_flow(flow["id"], "loss", correction_id="prior-loss"))
        self.assertTrue(self.ledger.unclassified_flows())

    def loss_streak(self):
        with patch("trade_logger.now_iso", return_value="2020-01-01T00:00:00+00:00"):
            for n in range(CFG.MAX_CONSECUTIVE_LOSSES):
                self.loss(-.1, oid=f"historical-loss-{n}")

    def test_concurrent_risk_manager_cannot_claim_another_half_open_attempt(self):
        from risk_manager import RiskManager
        self.loss_streak()
        second = RiskManager(self.tlog, self.pos, 10.)
        self.assertTrue(self.risk.claim_half_open_attempt("first")[0])
        self.assertFalse(second.claim_half_open_attempt("second")[0])
        self.assertEqual(JsonStore.load(_p(CFG.RISK_FILE), {})["half_open_ticker"], "first")

    def test_failed_half_open_release_preserves_the_claim(self):
        self.loss_streak()
        self.assertTrue(self.risk.claim_half_open_attempt("first")[0])
        before = copy.deepcopy(self.risk.state)
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(self.risk.release_half_open_attempt("first", "synthetic no order"))
        self.assertEqual(self.risk.state, before)
        self.assertEqual(JsonStore.load(_p(CFG.RISK_FILE), None), before)

    def test_date_rollover_does_not_erase_the_half_open_claim(self):
        from risk_manager import RiskManager
        self.loss_streak()
        self.assertTrue(self.risk.claim_half_open_attempt("first")[0])
        old = copy.deepcopy(self.risk.state)
        old["date"] = "2000-01-01"
        self.assertTrue(JsonStore.save(_p(CFG.RISK_FILE), old))
        restarted = RiskManager(self.tlog, self.pos, 10.)
        self.assertTrue(restarted.state["half_open_claimed"])
        self.assertFalse(restarted.claim_half_open_attempt("second")[0])

    def open_trade(self):
        trade = self.tlog.open_trade(ticker="KXBTC15M-X", market_title="synthetic",
            side="yes", req_price=50, avg_price=50, req_count=6, filled_count=6,
            spread=1, fees=0., edge=.1, ev=.1, confidence=8, grade="A",
            reason="test", analysis={}, order_id="new-order", order_status="executed")
        self.pos.open_position(trade, {"fill_ids": ["fill-1"]})
        return trade

    def test_settlement_commits_journal_position_and_fill_ids(self):
        self.prove()
        t = self.open_trade()
        self.pos._settle_and_release(t["trade_id"], self.pos.positions[t["trade_id"]],
                                     "no", False, -3., -3.)
        journal = TradeLogger()
        pos = PositionManager(self.broker, journal)
        self.assertEqual(pos.open_count(), 0)
        self.assertEqual(pos.seen_fill_ids, {"fill-1"})
        self.assertEqual(journal.settled_trades()[0]["net_pnl"], -3.)
        ledger = EquityLedger(journal, pos, authority=self.authority)
        ledger.observe(7.)
        self.assertAlmostEqual(ledger.drawdown_pct(), 30.)
        self.assertIsNone(recovery_problem(ledger.path))

    def test_position_write_failure_never_publishes_settlement_or_release(self):
        t = self.open_trade()
        old_positions, old_trades = copy.deepcopy(self.pos.positions), copy.deepcopy(self.tlog.trades)
        original = JsonStore.save
        def fail(path, *args, **kwargs):
            if path == _p(CFG.POSITIONS_FILE):
                return False
            return original(path, *args, **kwargs)
        with patch.object(JsonStore, "save", side_effect=fail), self.assertRaises(RuntimeError):
            self.pos._settle_and_release(t["trade_id"], self.pos.positions[t["trade_id"]],
                                         "no", False, -3., -3.)
        self.assertEqual(self.pos.positions, old_positions)
        self.assertEqual(self.tlog.trades, old_trades)
        self.assertIsNotNone(recovery_problem(self.ledger.path))
        PersistenceSentinel.reset()
        self.assertFalse(EquityLedger(TradeLogger(), self.pos).capital_eligible())

    def test_missing_original_trade_retains_position(self):
        t = self.open_trade()
        with patch.object(self.tlog, "settle_trade", return_value=None):
            result = self.pos._settle_and_release(t["trade_id"], self.pos.positions[t["trade_id"]],
                                                 "yes", True, 3., 3.)
        self.assertIsNone(result)
        self.assertIn(t["trade_id"], self.pos.positions)
        self.assertEqual(self.pos.reconcile_halt["status"], "UNKNOWN")

    def test_old_position_with_incomplete_result_keeps_full_exposure(self):
        t = self.open_trade()
        self.pos.positions[t["trade_id"]]["opened_at"] = "2020-01-01T00:00:00+00:00"
        self.broker.get_market = lambda _ticker: {"status": "settled", "result": ""}
        self.assertEqual(self.pos.check_settlements(), [])
        self.assertEqual(self.pos.open_risk(), 3.)
        self.assertEqual(self.pos.reconcile_halt["status"], "UNKNOWN")

    def test_fill_replay_ids_are_not_truncated_on_restart(self):
        self.pos.seen_fill_ids = {f"fill-{n}" for n in range(6001)}
        self.pos.flush()
        restarted = PositionManager(self.broker, self.tlog)
        self.assertEqual(restarted.seen_fill_ids, self.pos.seen_fill_ids)

    def test_subcent_settlement_loss_remains_economic_history(self):
        self.loss(-.0001)
        self.assertEqual(self.tlog.settled_trades()[0]["net_pnl"], -.0001)
        self.assertGreater(self.ledger.drawdown_pct(), 0.)

    def test_busy_adverse_observation_cannot_disappear_on_balance_rebound(self):
        self.prove()
        self.ledger.observe(9., quiet=False)
        self.ledger.observe(10., quiet=False)
        self.assertEqual(sum(r["amount"] for r in self.ledger.unclassified_flows()), -1.)
        self.assertFalse(self.ledger.capital_eligible())
        restarted = EquityLedger(self.tlog, self.pos, authority=self.authority)
        self.assertFalse(restarted.capital_eligible())

    def test_positive_pending_observation_cannot_grant_eligibility(self):
        self.prove()
        self.ledger.observe(11.)
        self.assertFalse(self.ledger.capital_eligible())
        self.assertEqual(self.ledger.strategy_equity(), 10.)

    def test_cumulative_small_debits_exhaust_the_fixed_noise_budget(self):
        self.prove()
        for n in range(1, 7):
            self.ledger.observe(10. - n * .006)
        self.assertFalse(self.ledger.capital_eligible())
        noise = sum(-min(0., f["amount"]) for f in self.ledger.state["flows"]
                    if f["kind"] == "rounding")
        self.assertLessEqual(noise, .01)

    def test_direct_journal_rewrite_cannot_remove_unobserved_settlement(self):
        t = self.open_trade()
        self.tlog.settle_trade(t["trade_id"], "no", False, -3., -3.)
        self.assertFalse(JsonStore.save(self.tlog.path, []))
        self.assertEqual(TradeLogger().settled_trades()[0]["net_pnl"], -3.)
        self.assertFalse(self.ledger.capital_eligible())

    def test_silent_journal_flush_is_not_published_as_success(self):
        t = self.open_trade()
        before = copy.deepcopy(self.tlog.trades)
        with patch.object(self.tlog, "flush", return_value=None), self.assertRaises(RuntimeError):
            self.tlog.settle_trade(t["trade_id"], "no", False, -3., -3.)
        self.assertEqual(self.tlog.trades, before)
        self.assertEqual(JsonStore.load(self.tlog.path, None), before)

    def test_silent_ledger_save_is_not_published_as_success(self):
        self.loss()
        prop, ctx = self.rebase()
        before = copy.deepcopy(self.ledger.state)
        with patch.object(JsonStore, "save", return_value=True):
            self.assertFalse(self.ledger.apply_rebase(prop["reason"], prop["operator_action_id"],
                                                      prop["token"], ctx))
        self.assertEqual(self.ledger.state, before)
        self.assertFalse(self.ledger.capital_eligible())

    def test_pending_intent_cannot_close_without_an_order_identity(self):
        self.assertTrue(self.orders._record_intent("KXBTC15M-X", "cid", 1, 50, side="yes"))
        before = copy.deepcopy(self.orders.pending_intents)
        self.assertIsNot(self.orders._adopt_submission("KXBTC15M-X", "", "yes", 1, 50), True)
        self.assertEqual(self.orders.pending_intents, before)
        self.assertEqual(JsonStore.load(_p(self.orders.PENDING_FILE), None), before)

    def test_adoption_commit_failure_keeps_pending_intent(self):
        self.assertTrue(self.orders._record_intent("KXBTC15M-X", "cid", 1, 50, side="yes"))
        before = copy.deepcopy(self.orders.pending_intents)
        with patch.object(JsonStore, "save", return_value=False):
            self.assertIsNot(self.orders._adopt_submission("KXBTC15M-X", "oid", "yes", 1, 50), True)
        self.assertEqual(self.orders.pending_intents, before)
        self.assertEqual(self.orders.open_orders, {})

    def test_external_commit_callback_cannot_change_read_evidence(self):
        self.prove()
        self.loss()
        prop, ctx = self.rebase()
        original = self.authority.advance
        def change(before, after):
            response = original(before, after)
            Path(self.tlog.path).write_text("[]")
            return response
        old = copy.deepcopy(self.ledger.state)
        with patch.object(self.authority, "advance", side_effect=change):
            self.assertFalse(self.ledger.apply_rebase(prop["reason"], prop["operator_action_id"],
                                                      prop["token"], ctx))
        self.assertEqual(self.ledger.state, old)
        self.assertIsNotNone(recovery_problem(self.ledger.path))

    def test_fork_cannot_inherit_ledger_or_engine_writer_authority(self):
        self.prove()
        lease = WriterLease(self.ledger.path)
        self.addCleanup(lease.close)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                with root_lock(self.ledger.path):
                    blocked = not lease.valid() and not self.ledger.capital_eligible()
                os.write(write_fd, b"blocked" if blocked else b"unsafe")
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 20), b"blocked")
            _, status = os.waitpid(child, 0)
            self.assertEqual(status, 0)
            self.assertTrue(lease.valid())
        finally:
            os.close(read_fd)


class VerifiedRecovery(AuthorityCase):
    def recover(self, action="recovery-1", identity=None, digest=None, provider=None):
        return complete_verified_recovery(self.ledger.path, identity or self.ledger.identity,
            provider or self.authority, digest or checkpoint(self.ledger.path, self.ledger.identity).digest,
            action)

    def test_current_independent_snapshot_recovers_without_losing_economics(self):
        self.prove()
        self.loss()
        before = Path(self.ledger.path).read_bytes()
        PersistenceSentinel.record_failure(self.ledger.path, "interrupted acknowledgement")
        self.assertFalse(self.ledger.capital_eligible())
        self.recover()
        self.assertEqual(Path(self.ledger.path).read_bytes(), before)
        restarted = EquityLedger(self.tlog, self.pos, authority=self.authority)
        self.assertAlmostEqual(restarted.drawdown_pct(), 30.)
        self.assertTrue(PersistenceSentinel.healthy())

    def test_pending_marker_with_exact_independent_commit_can_be_completed(self):
        self.prove()
        Path(pending_path(self.ledger.path)).write_text('{"interrupted":true}')
        self.assertIsNotNone(recovery_problem(self.ledger.path))
        self.recover()
        self.assertIsNone(recovery_problem(self.ledger.path))

    def test_stale_review_digest_cannot_authorize_recovery(self):
        self.prove()
        previous = checkpoint(self.ledger.path, self.ledger.identity).digest
        self.loss()
        before = self.image()
        with self.assertRaises(AuthorityError):
            self.recover(digest=previous)
        self.assertEqual(self.image(), before)

    def test_other_account_cannot_authorize_recovery(self):
        self.prove()
        from continuity_authority import account_identity
        before = self.image()
        with self.assertRaises(AuthorityError):
            self.recover(identity=account_identity("kalshi", "prod", "account-B"))
        self.assertEqual(self.image(), before)

    def test_missing_independent_authority_cannot_authorize_recovery(self):
        before = self.image()
        with self.assertRaises(AuthorityError):
            complete_verified_recovery(self.ledger.path, self.ledger.identity, None, "0" * 64, "op")
        self.assertEqual(self.image(), before)

    def test_recovery_cas_ambiguity_leaves_a_durable_block(self):
        self.prove()
        with patch.object(self.authority, "advance", return_value=None), self.assertRaises(AuthorityError):
            self.recover()
        PersistenceSentinel.reset()
        self.assertIsNotNone(recovery_problem(self.ledger.path))
        self.assertFalse(EquityLedger(self.tlog, self.pos, authority=self.authority).capital_eligible())

    def test_recovery_action_id_cannot_be_reused(self):
        self.prove()
        self.recover()
        before = self.image()
        with self.assertRaises(AuthorityError):
            self.recover()
        self.assertEqual(self.image(), before)
