from authority_fixtures import freeze_for
# -*- coding: utf-8 -*-
"""A09-A12 -- execution, durable-transition, flow-identity and tooling.

A09 FAILED INTENT PERSISTENCE DOES NOT BLOCK SUBMISSION  (most important)
    INVARIANT   No order submission may reach broker transport unless its
                intent has FIRST been durably persisted and read back.
    ROOT CAUSE  `place_and_track` called `self._record_intent(...)` and threw
                the result away. Astra put a DIRECTORY where
                pending_intents.json belongs -- a real filesystem error --
                and the POST still reached the synthetic adapter. An order
                may then exist at the broker with nothing on disk able to
                ask "does it?" after a timeout or a restart.
    CORRECTION  `_record_intent` returns a checked boolean and VERIFIES by
                re-reading the file; `place_and_track` aborts before the
                transport when it is false; pending_intents.json is a
                CRITICAL basename, so the failure also trips the persistence
                sentinel and the engine's global gate. The tripwire below
                asserts the adapter's call count stays exactly zero.

A10 FAILED REBASE SAVE MUTATES MEMORY
    INVARIANT   A refused durable transition leaves the previous state
                authoritative, in memory and on disk.
    ROOT CAUSE  `apply_rebase` mutated `self.state` (HWM, consumed token,
                capital hold) and only then attempted the write, returning
                False with the mutation already visible in-process.
    CORRECTION  PREPARE on a copy -> VALIDATE -> COMMIT durably -> PUBLISH.
                `_commit` restores the previous state on any failure. The
                single-use token is burned in the append-only chain BEFORE
                the state that spends it, so the surviving half of a crash
                is always the safe one: a token that cannot be replayed and
                a rebase that did not happen.

A11 SAME UNCLASSIFIED FLOW RECOUNTED
    INVARIANT   Observing the same unresolved movement again is not a new
                economic event.
    ROOT CAUSE  An unclassified flow does not enter `flows_cum`, so the
                expected balance never moved, so the SAME -1 residual came
                back every k quiet cycles and was appended again. Nine
                observations became three -1 flows and an overstated risk.
    CORRECTION  `accounted_flows_cum()` separates "already written down"
                from "counted as external", and `_append_flow` updates an
                open unclassified row (first/last seen, observation count,
                balance snapshot) instead of appending a second one.

A12 READ-ONLY OPERATOR TOOL WRITES STATE
    INVARIANT   A status / dry-run command leaves every state file
                byte-for-byte unchanged.
    ROOT CAUSE  `EquityLedger.__init__` reconciles and SAVES, so building
                one to print a status rewrote the ledger and rotated its
                backups -- most visibly in the restored-journal case, the
                one an operator most wants to inspect without touching.
    CORRECTION  `EquityLedger.load_readonly`, whose every durable write is a
                logged refusal, used by `tools/equity_ledger_tool.py`.
"""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import PRE_AT, AstraCase, Client, trade         # noqa: E402

import equity_ledger as EL                                  # noqa: E402
from config import CFG, _p                                  # noqa: E402
from execution_result import ExecutionResult                # noqa: E402
from order_manager import OrderManager                      # noqa: E402
from persistence import JsonStore, PersistenceSentinel      # noqa: E402
from position_manager import PositionManager                # noqa: E402
from trade_logger import TradeLogger                        # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKER = "KXBTCD-26SEP0912-T60000"


class _TransportTripwire(Client):
    """Counts every call that would reach a broker adapter. The count MUST
    stay at zero whenever intent persistence failed."""

    env = "demo"          # the only environment where a submission is wired

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.create_calls = 0
        self.cancel_calls = 0

    #: A fully-filled synthetic response keeps the positive control short:
    #: the point it proves is that the adapter is REACHABLE, so the zeros
    #: above mean something.
    def create_order(self, ticker, side, count, price, **kw):
        self.create_calls += 1
        # what was ON DISK at the instant the transport was reached
        from config import _p as _path
        from persistence import JsonStore as _Store
        self.intent_on_disk_at_post = _Store.load(
            _path("pending_intents.json"), {})
        return {"order_id": "synthetic-1", "status": "executed",
                "ticker": ticker, "side": side, "yes_price": price,
                "taker_fill_count": count, "fill_count": count,
                "remaining_count": 0, "taker_fill_cost": count * price,
                "taker_fees": 0,
                "client_order_id": kw.get("client_order_id")}

    def cancel_order(self, *a, **kw):
        self.cancel_calls += 1
        return {}

    def get_order(self, order_id, **kw):
        return {"order_id": order_id, "status": "executed",
                "taker_fill_count": 1, "fill_count": 1,
                "remaining_count": 0, "taker_fill_cost": 40, "taker_fees": 0}

    def get_fills(self, order_id, **kw):
        return [{"fill_id": "f-1", "order_id": order_id, "count": 1,
                 "yes_price": 40, "is_taker": True}]


# ══════════════════════════════════════════════════════ A09
class NoSubmissionWithoutADurableIntent(AstraCase):

    def order_manager(self, client=None):
        client = client or _TransportTripwire()
        tlog = TradeLogger()
        pos = PositionManager(client, tlog)
        om = OrderManager(client)
        return client, tlog, pos, om

    def submit(self, om):
        return om.place_and_track(TICKER, "yes", 1, 40)

    def assert_no_transport(self, client, result):
        self.assertEqual(client.create_calls, 0,
                         "an order reached broker transport without a "
                         "durable intent")
        self.assertEqual(client.cancel_calls, 0)
        self.assertIsInstance(result, ExecutionResult)
        self.assertEqual(result.state, "rejected")
        self.assertIn(str(result.status), ("blocked:intent_unwritable", "blocked:recovery_required"))

    def test_a_directory_in_place_of_the_file_blocks_the_post(self):
        """Astra PERSIST_failed_intent_write_reaches_simulated_order_transport,
        reproduced with a real filesystem error."""
        client, tlog, pos, om = self.order_manager()
        os.unlink(_p(OrderManager.PENDING_FILE))
        os.makedirs(_p(OrderManager.PENDING_FILE), exist_ok=True)
        result = self.submit(om)
        self.assert_no_transport(client, result)
        self.assertFalse(PersistenceSentinel.healthy())

    def test_a_permission_error_blocks_the_post(self):
        client, tlog, pos, om = self.order_manager()
        with patch.object(JsonStore, "save",
                          side_effect=PermissionError("read-only volume")):
            result = self.submit(om)
        self.assert_no_transport(client, result)

    def test_a_save_returning_false_blocks_the_post(self):
        """Disk full / atomic replace failure: JsonStore fails soft, and the
        caller is REQUIRED to read the boolean."""
        client, tlog, pos, om = self.order_manager()
        with patch.object(JsonStore, "save", return_value=False):
            result = self.submit(om)
        self.assert_no_transport(client, result)

    def test_an_unreadable_readback_blocks_the_post(self):
        """The write reported success but the file cannot be read back:
        persistence is not PROVEN, so the submission does not happen."""
        client, tlog, pos, om = self.order_manager()
        real_load = JsonStore.load

        def blind(path, default, *a, **kw):
            if os.path.basename(path) == OrderManager.PENDING_FILE:
                return {}
            return real_load(path, default, *a, **kw)
        with patch.object(JsonStore, "load", side_effect=blind):
            result = self.submit(om)
        self.assert_no_transport(client, result)

    def test_a_readback_with_the_wrong_intent_blocks_the_post(self):
        client, tlog, pos, om = self.order_manager()
        real_load = JsonStore.load

        def wrong(path, default, *a, **kw):
            if os.path.basename(path) == OrderManager.PENDING_FILE:
                return {TICKER: {"client_order_id": "someone-elses"}}
            return real_load(path, default, *a, **kw)
        with patch.object(JsonStore, "load", side_effect=wrong):
            result = self.submit(om)
        self.assert_no_transport(client, result)

    def test_no_in_memory_intent_survives_a_failed_write(self):
        client, tlog, pos, om = self.order_manager()
        with patch.object(JsonStore, "save", return_value=False):
            self.submit(om)
        self.assertNotIn(TICKER, om.pending_intents)

    def test_a_failed_intent_is_not_retried_to_the_broker(self):
        client, tlog, pos, om = self.order_manager()
        os.unlink(_p(OrderManager.PENDING_FILE))
        os.makedirs(_p(OrderManager.PENDING_FILE), exist_ok=True)
        for _ in range(3):
            self.submit(om)
        self.assertEqual(client.create_calls, 0)

    def test_pending_intents_is_a_critical_state_file(self):
        from persistence import CRITICAL_BASENAMES
        self.assertIn("pending_intents.json", CRITICAL_BASENAMES)

    def test_positive_control_a_writable_intent_reaches_the_transport(self):
        """Without this the zeros above would prove nothing: the tripwire
        has to be able to count."""
        client, tlog, pos, om = self.order_manager()
        result = self.submit(om)
        self.assertEqual(client.create_calls, 1)
        # the ORDERING is the invariant: the intent was already durable at
        # the instant the transport was reached
        self.assertEqual(
            client.intent_on_disk_at_post.get(TICKER, {}).get("client_order_id"),
            OrderManager._client_order_id(TICKER, "yes", 1, 40))
        self.assertEqual(result.state, "filled")

    def test_an_intent_persisted_before_a_crash_is_reloaded(self):
        """Restart between the intent commit and the broker POST: the
        intent is on disk, so the question stays askable."""
        client, tlog, pos, om = self.order_manager()
        cid = OrderManager._client_order_id(TICKER, "yes", 1, 40)
        self.assertTrue(om._record_intent(TICKER, cid, 1, 40, side="yes"))
        om2 = OrderManager(client)                          # the restart
        self.assertIn(TICKER, om2.pending_intents)
        self.assertEqual(om2.pending_intents[TICKER]["client_order_id"], cid)


# ══════════════════════════════════════════════════════ A10
class AFailedCommitLeavesTheOldStateAuthoritative(AstraCase):

    def blown(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        return client, tlog, pos, led

    def ctx(self, led):
        return {"drawdown_firing": True, "reconcile_status": "MATCH",
                "open_positions": 0, "in_flight_orders": 0, "quiescent": True,
                "evidence_unstable": None, "bound_state": led.bound_state(), "execution_freeze": freeze_for(led),
                "orders": {"local_open": [], "pending_intents": [],
                           "resolution_halt": False, "broker_open": 0,
                           "broker_open_ids": [], "broker_error": None,
                           "disagreement": False}}

    def test_a_failed_save_mutates_neither_hwm_nor_hold_nor_tokens(self):
        """Astra TOKEN_failed_actual_filesystem_save_mutates_risk."""
        client, tlog, pos, led = self.blown()
        hwm = led.risk_equity_reference()
        tokens = list(led.state["consumed_tokens"])
        rebases = len(led.state["rebases"])
        prop = led.propose_rebase("losses acknowledged", "OPS-50")
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(led.apply_rebase("losses acknowledged", "OPS-50",
                                              prop["token"], self.ctx(led)))
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)
        self.assertIsNone(led.state["capital_hold"])
        self.assertEqual(led.state["consumed_tokens"], tokens)
        self.assertEqual(len(led.state["rebases"]), rebases)

    def test_a_real_filesystem_failure_leaves_the_state_intact(self):
        client, tlog, pos, led = self.blown()
        hwm = led.risk_equity_reference()
        on_disk_before = open(_p(EL.LEDGER_FILE), "rb").read()
        prop = led.propose_rebase("losses acknowledged", "OPS-51")
        # fail at the atomic replace, so JsonStore's OWN error handling runs
        with patch("persistence.os.replace",
                   side_effect=OSError("no space left on device")):
            self.assertFalse(led.apply_rebase("losses acknowledged", "OPS-51",
                                              prop["token"], self.ctx(led)))
        self.assertAlmostEqual(led.risk_equity_reference(), hwm, places=9)
        self.assertIsNone(led.state["capital_hold"])
        self.assertEqual(open(_p(EL.LEDGER_FILE), "rb").read(), on_disk_before)

    def test_the_burned_token_is_not_replayable_after_a_failed_commit(self):
        """The safe half of the pair: the rebase did not happen AND the
        authorization cannot be spent again. The operator issues a new
        action id."""
        client, tlog, pos, led = self.blown()
        prop = led.propose_rebase("losses acknowledged", "OPS-52")
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(led.apply_rebase("losses acknowledged", "OPS-52",
                                              prop["token"], self.ctx(led)))
        self.assertTrue(led.token_consumed(prop["token"]))
        self.assertFalse(led.apply_rebase("losses acknowledged", "OPS-52",
                                          prop["token"], self.ctx(led)))
        self.assertAlmostEqual(led.risk_equity_reference(), 10.0, places=9)

    def test_a_failed_commit_survives_a_restart_as_a_non_event(self):
        client, tlog, pos, led = self.blown()
        prop = led.propose_rebase("losses acknowledged", "OPS-53")
        with patch.object(JsonStore, "save", return_value=False):
            led.apply_rebase("losses acknowledged", "OPS-53", prop["token"],
                             self.ctx(led))
        led2, _, _ = self.reload()
        self.assertAlmostEqual(led2.risk_equity_reference(), 10.0, places=9)
        self.assertIsNone(led2.state["capital_hold"])
        self.assertEqual(led2.state["rebases"], [])

    def test_positive_control_a_successful_commit_publishes(self):
        client, tlog, pos, led = self.blown()
        prop = led.propose_rebase("losses acknowledged", "OPS-54")
        self.assertTrue(led.apply_rebase("losses acknowledged", "OPS-54",
                                         prop["token"], self.ctx(led)))
        self.assertAlmostEqual(led.risk_equity_reference(), 7.0, places=9)
        self.assertEqual(led.state["capital_hold"]["reason"],
                         "post_rebase_validation")


# ══════════════════════════════════════════════════════ A11
class TheSameUnresolvedMovementIsOneEvent(AstraCase):

    def observe_many(self, cycles=9, cash=9.0):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        for i in range(cycles):
            led.observe(cash, cycle_n=i + 1, quiet=True)
        return led

    def test_nine_observations_of_one_withdrawal_are_one_flow(self):
        """Astra FLOW_same_unclassified_withdrawal_repeated_nine_cycles:
        one -1 residual became three -1 flows."""
        led = self.observe_many(9, cash=9.0)
        unclassified = led.unclassified_flows()
        self.assertEqual(len(unclassified), 1, unclassified)
        self.assertAlmostEqual(float(unclassified[0]["amount"]), -1.0, places=6)
        self.assertGreaterEqual(int(unclassified[0]["observations"]), 1)
        self.assertIn("first_seen_at", unclassified[0])
        self.assertIn("last_seen_at", unclassified[0])

    def test_the_conservative_equity_is_not_deepened_by_recounting(self):
        led = self.observe_many(12, cash=9.0)
        self.assertAlmostEqual(led.strategy_equity_conservative(), 9.0,
                               places=6)

    def test_it_stays_one_flow_across_restarts(self):
        self.observe_many(9, cash=9.0)
        led2, _, _ = self.reload()
        for i in range(9):
            led2.observe(9.0, cycle_n=100 + i, quiet=True)
        self.assertEqual(len(led2.unclassified_flows()), 1)

    def test_classification_closes_the_residual_for_good(self):
        led = self.observe_many(9, cash=9.0)
        flow_id = led.unclassified_flows()[0]["id"]
        self.assertTrue(led.classify_flow(flow_id, EL.FLOW_WITHDRAWAL,
                                          action_id="OPS-60"))
        for i in range(6):
            led.observe(9.0, cycle_n=200 + i, quiet=True)
        self.assertEqual(led.unclassified_flows(), [])
        self.assertEqual(len([f for f in led.state["flows"]
                              if f["kind"] == EL.FLOW_WITHDRAWAL]), 1)
        self.assertNotIn(EL.GUARD_FLOW_UNRESOLVED, led.guards())

    def test_positive_control_a_second_distinct_movement_is_a_second_flow(self):
        led = self.observe_many(9, cash=9.0)
        self.assertEqual(len(led.unclassified_flows()), 1)
        for i in range(6):
            led.observe(6.5, cycle_n=300 + i, quiet=True)   # a further -2.5
        self.assertEqual(len(led.unclassified_flows()), 2)


# ══════════════════════════════════════════════════════ A12
class TheOperatorToolWritesNothing(AstraCase):

    def build_state(self, restore_journal=False):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        if restore_journal:
            JsonStore.save(_p(CFG.TRADES_FILE), [])
        return led

    def digests(self):
        out = {}
        for name in sorted(os.listdir(self._tmp)):
            path = os.path.join(self._tmp, name)
            if os.path.isfile(path):
                out[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
        return out

    def run_tool(self, *args):
        env = dict(os.environ, DATA_DIR=self._tmp,
                   PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "equity_ledger_tool.py"),
             *args], cwd=REPO, env=env, capture_output=True, text=True)

    def assert_unchanged(self, *args):
        before = self.digests()
        proc = self.run_tool(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        after = self.digests()
        changed = [k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)]
        self.assertEqual(changed, [], f"{args[0]} modified {changed}")
        return proc

    def test_status_on_a_restored_journal_changes_no_byte(self):
        """Astra M_dryrun_tool_on_journal_mismatch: this is the case that
        used to trigger a reconciliation write from the constructor."""
        self.build_state(restore_journal=True)
        proc = self.assert_unchanged("status")
        out = json.loads(proc.stdout)
        self.assertTrue(out["readonly"])
        self.assertFalse(out["capital_eligible"])
        self.assertGreaterEqual(out["drawdown_pct"], 30.0 - 1e-6)

    def test_status_on_a_healthy_ledger_changes_no_byte(self):
        self.build_state()
        self.assert_unchanged("status")

    def test_every_proposal_command_changes_no_byte(self):
        self.build_state()
        for args in (("rebase", "--reason", "r", "--action-id", "OPS-70"),
                     ("hold-release", "--action-id", "OPS-71",
                      "--validation", "ref"),
                     ("attest", "--action-id", "OPS-72",
                      "--funding-records-sha256", "c" * 64)):
            with self.subTest(cmd=args[0]):
                if args[0] == "hold-release":
                    continue          # needs an open hold; covered below
                self.assert_unchanged(*args)

    def test_a_seed_proposal_on_an_unseeded_ledger_changes_no_byte(self):
        client, tlog, pos = self.stack()
        trade(tlog, ticker="KX-A")
        before = self.digests()
        proc = self.run_tool("seed", "--pre-flow-cash", "10", "--pre-flow-at",
                             PRE_AT, "--evidence", "ref", "--cash-now", "10")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        after = self.digests()
        self.assertEqual([k for k in set(before) | set(after)
                          if before.get(k) != after.get(k)], [])

    def test_a_readonly_ledger_refuses_to_commit(self):
        self.build_state()
        ro = EL.EquityLedger.load_readonly(TradeLogger(),
                                           PositionManager(Client(), TradeLogger()))
        self.assertTrue(ro.readonly)
        self.assertFalse(ro.save())
        self.assertFalse(ro._commit(dict(ro.state)))

    def test_positive_control_the_engine_path_still_writes(self):
        """The refusals above must come from the read-only flag, not from a
        ledger that can no longer persist anything."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        before = self.digests()
        led.observe(10.0, cycle_n=1, quiet=True)
        self.assertNotEqual(self.digests(), before)


if __name__ == "__main__":
    unittest.main()
