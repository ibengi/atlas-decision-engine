# -*- coding: utf-8 -*-
"""A13-A20 -- the transaction, freshness, durability and identity family.

VIOLATED INVARIANTS (Astra, on 3af848e)
    A13  Two processes starting from the same generation could both commit.
    A14  A prepared state was visible to readers before it was durable.
    A15  A refused operator action still mutated authoritative memory.
    A16  NaN/Inf passed validation and every guard comparison.
    A18  A short write and an ignored directory fsync counted as durable.
    A19  A reader kept deciding from an authority another process had moved.
    A20  Durable economic state was not bound to an account or environment.

Every test drives the PRODUCTION classes on a throwaway DATA_DIR. The
multi-process case forks real processes; the durability cases inject real
failures at the syscall boundary rather than stubbing the layer under test.
"""
import json
import math
import multiprocessing as mp
import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _astra import PRE_AT, AstraCase, Client, trade  # noqa: E402

import account_binding                                 # noqa: E402
import equity_ledger as EL                             # noqa: E402
import state_tx                                        # noqa: E402
from config import CFG, _p                             # noqa: E402
from equity_ledger import EquityLedger                 # noqa: E402
from order_manager import OrderManager                 # noqa: E402
from persistence import JsonStore, PersistenceSentinel  # noqa: E402
from position_manager import PositionManager           # noqa: E402
from state_tx import FenceError, StaleAuthority        # noqa: E402
from trade_logger import TradeLogger                   # noqa: E402


# ── A13: at most one writer may advance a generation ───────────────────
def _race_child(tmp, path, generation, barrier, results, index):
    """A real second process: it loads nothing, it simply tries to advance
    the same generation at the same instant."""
    from config import CFG as child_cfg
    child_cfg.DATA_DIR = tmp
    barrier.wait()
    ok = JsonStore.save(path, {"writer": index, "payload": "x" * 64},
                        expect_generation=generation)
    results[index] = 1 if ok else 0


class ConcurrentWritersCannotBothWin(AstraCase):
    """A13. The generation check and the write are one step, or they are a
    race with a silent loser."""

    def test_two_real_processes_racing_the_same_generation(self):
        path = os.path.join(self._tmp, "raced_state.json")
        self.assertTrue(JsonStore.save(path, {"seed": True},
                                       expect_generation=0))
        start_gen = 1
        ctx = mp.get_context("fork")
        for _attempt in range(3):
            barrier = ctx.Barrier(2)
            results = ctx.Array("i", [0, 0])
            procs = [ctx.Process(target=_race_child,
                                 args=(self._tmp, path, start_gen, barrier,
                                       results, i)) for i in range(2)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
                self.assertIsNotNone(p.exitcode, "child did not terminate")
            winners = sum(results)
            self.assertLessEqual(
                winners, 1,
                "two processes advanced the SAME generation: the check and "
                "the commit are not atomic")
            self.assertEqual(winners, 1, "nobody committed: the fence "
                                         "deadlocked instead of serialising")
            from persistence import read_generation
            self.assertEqual(read_generation(path), start_gen + 1)
            start_gen += 1

    def test_a_stale_writer_is_refused_after_another_commits(self):
        """Writer A validated at N; B commits N+1; A resumes and must lose."""
        path = os.path.join(self._tmp, "raced_state.json")
        JsonStore.save(path, {"v": 0}, expect_generation=0)
        held_by_a = 1                       # what A believes is on disk
        self.assertTrue(JsonStore.save(path, {"v": "B"},
                                       expect_generation=held_by_a))
        self.assertFalse(
            JsonStore.save(path, {"v": "A"}, expect_generation=held_by_a),
            "the stale writer overwrote the newer state")
        self.assertEqual(JsonStore.load(path, {})["v"], "B")

    def test_the_fence_is_reentrant_within_one_process(self):
        """A nested save must not deadlock against its own flock."""
        path = os.path.join(self._tmp, "nested.json")
        fence = state_tx.fence_for(path)
        with fence:
            self.assertTrue(JsonStore.save(path, {"nested": True}))
        self.assertFalse(fence.held())

    def test_a_fence_that_cannot_be_acquired_refuses_the_write(self):
        path = _p(EL.LEDGER_FILE)
        with patch.object(state_tx.WriterFence, "acquire",
                          side_effect=FenceError("boom")):
            self.assertFalse(JsonStore.save(path, {"x": 1}))
        self.assertFalse(PersistenceSentinel.healthy(),
                         "an unfenced critical write must trip the sentinel")


# ── A14: nothing is visible before it is durable ───────────────────────
class NothingIsPublishedBeforeItIsDurable(AstraCase):

    def seeded(self):
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        return led

    def test_a_reader_never_observes_the_prepared_state_mid_commit(self):
        led = self.seeded()
        before = {"hwm": led.risk_equity_reference(),
                  "hold": led.state.get("capital_hold"),
                  "tokens": list(led.state.get("consumed_tokens") or []),
                  "eligible": led.capital_eligible(),
                  "status": led.derive_status()}
        seen = []
        gate = threading.Event()

        real_save = JsonStore.save

        def slow_save(path, data, *a, **kw):
            # The commit is in flight: whatever a reader sees NOW must be
            # the OLD authority.
            seen.append({"hwm": led.risk_equity_reference(),
                         "hold": led.state.get("capital_hold"),
                         "tokens": list(led.state.get("consumed_tokens") or []),
                         "eligible": led.capital_eligible(),
                         "status": led.derive_status()})
            gate.set()
            return real_save(path, data, *a, **kw)

        prepared = json.loads(json.dumps(led.state))
        prepared["hwm"] = {"risk_equity_reference": 7.0, "at": "x",
                           "rebased_from": None, "floor_from_settled_index": 0}
        prepared["capital_hold"] = None
        prepared["consumed_tokens"] = list(prepared.get("consumed_tokens") or []) + ["t"]
        with patch.object(JsonStore, "save", side_effect=slow_save):
            led._commit(prepared)
        self.assertTrue(gate.is_set(), "the commit never reached the write")
        for observation in seen:
            self.assertEqual(observation["hwm"], before["hwm"])
            self.assertEqual(observation["hold"], before["hold"])
            self.assertEqual(observation["tokens"], before["tokens"])
            self.assertEqual(observation["eligible"], before["eligible"])
            self.assertEqual(observation["status"], before["status"])

    def test_a_failed_persistence_publishes_nothing(self):
        led = self.seeded()
        before = json.loads(json.dumps(led.state))
        prepared = json.loads(json.dumps(led.state))
        prepared["hwm"]["risk_equity_reference"] = 1.0
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(led._commit(prepared))
        self.assertEqual(led.state["hwm"], before["hwm"])
        self.assertEqual(led.risk_equity_reference(),
                         before["hwm"]["risk_equity_reference"])

    def test_restart_after_a_failed_commit_sees_the_old_state(self):
        led = self.seeded()
        hwm = led.risk_equity_reference()
        prepared = json.loads(json.dumps(led.state))
        prepared["hwm"]["risk_equity_reference"] = 0.5
        with patch.object(JsonStore, "save", return_value=False):
            led._commit(prepared)
        again, _, _ = self.reload()
        self.assertAlmostEqual(again.risk_equity_reference(), hwm, places=9)


# ── A15: a refused operator action has no side effect ──────────────────
class RefusedOperatorActionsAreTotalNoOps(AstraCase):

    def held(self):
        """A ledger carrying a post-rebase capital hold."""
        client, tlog, pos = self.stack()
        led = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led)
        led.state["capital_hold"] = {"reason": "post_rebase_validation",
                                     "since": "t", "rebase_id": "rb-1",
                                     "released_by": None}
        led.state["rebases"].append({"rebase_id": "rb-1",
                                     "operator_action_id": "OPS-REBASE",
                                     "validation": None})
        self.assertTrue(led.save())
        return led

    def test_a_hold_release_refused_by_persistence_keeps_the_hold(self):
        led = self.held()
        prop = led.propose_hold_release("OPS-REL", "validation-ref")
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(led.apply_hold_release(
                "OPS-REL", "validation-ref", prop["token"]))
        self.assertIsNotNone(led.state.get("capital_hold"),
                             "a REFUSED release still removed the hold")
        self.assertIn(EL.GUARD_CAPITAL_HOLD, led.guards())
        self.assertFalse(led.capital_eligible())
        again, _, _ = self.reload()
        self.assertIsNotNone(again.state.get("capital_hold"))

    def test_a_hold_release_from_a_stale_generation_keeps_the_hold(self):
        led = self.held()
        prop = led.propose_hold_release("OPS-REL", "validation-ref")
        # another writer advances the durable generation underneath
        state = JsonStore.load(_p(EL.LEDGER_FILE), {})
        JsonStore.save(_p(EL.LEDGER_FILE), state,
                       expect_generation=led.generation)
        self.assertFalse(led.apply_hold_release(
            "OPS-REL", "validation-ref", prop["token"]))
        self.assertIsNotNone(led.state.get("capital_hold"))
        self.assertFalse(led.capital_eligible())

    def test_a_refused_attestation_leaves_the_status_untouched(self):
        client, tlog, pos = self.stack()
        led = EquityLedger(tlog, pos, env="prod")
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        status_before = led.derive_status()
        att = led.propose_attestation("OPS-A", "a" * 64)
        with patch.object(JsonStore, "save", return_value=False):
            self.assertFalse(led.apply_attestation("OPS-A", "a" * 64,
                                                   att["token"]))
        self.assertEqual(led.derive_status(), status_before)
        self.assertNotEqual(led.derive_status(), EL.STATUS_RECONCILED)


# ── A16: non-finite numbers never enter economic state ─────────────────
class NonFiniteValuesFailClosed(AstraCase):

    def seeded(self):
        client, tlog, pos = self.stack()
        return self.reconciled_ledger(tlog, pos), tlog, pos

    def test_a_nan_balance_is_refused_and_blocks_capital(self):
        led, _, _ = self.seeded()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(balance=bad):
                led.observe(bad, cycle_n=9, quiet=True)
                self.assertIsNotNone(led.state.get("non_finite_input"))
                self.assertFalse(led.capital_eligible())
                self.assertIn(EL.GUARD_NON_FINITE, led.guards())
                led.state.pop("non_finite_input", None)

    def test_a_non_finite_value_cannot_be_persisted(self):
        led, _, _ = self.seeded()
        led.state["hwm"]["risk_equity_reference"] = float("nan")
        self.assertFalse(led.save(), "a NaN HWM was written to disk")

    def test_a_non_finite_literal_cannot_be_read_back(self):
        led, _, _ = self.seeded()
        raw = json.dumps({"generation": 1, "hwm": {"risk_equity_reference": 1.0}})
        raw = raw.replace("1.0", "NaN")
        with open(_p("nan_state.json"), "w", encoding="utf-8") as fh:
            fh.write(raw)
        self.assertEqual(JsonStore.load(_p("nan_state.json"), "REFUSED"),
                         "REFUSED", "a NaN literal was parsed into a float")

    def test_the_guard_uses_isfinite_not_a_comparison(self):
        led, _, _ = self.seeded()
        nan = float("nan")
        self.assertFalse(nan > 0)
        self.assertFalse(nan < 0)          # why a comparison cannot catch it
        led.state["flows"].append({"id": "f-nan", "kind": EL.FLOW_ROUNDING,
                                   "amount": nan, "at": "t"})
        self.assertFalse(led.economic_values_finite())
        self.assertIn(EL.GUARD_NON_FINITE, led.guards())

    def test_a_string_nan_is_not_laundered_into_a_float(self):
        with self.assertRaises(state_tx.NonFiniteValue):
            state_tx.check_finite("NaN", "cash")
        with self.assertRaises(state_tx.NonFiniteValue):
            state_tx.check_finite(True, "cash")


# ── A18: durability means every byte ───────────────────────────────────
class WriteDurabilityIsProvedNotAssumed(AstraCase):

    def test_a_short_write_is_not_a_successful_write(self):
        payload = b"x" * 4096
        r, w = os.pipe()
        os.close(r)                                   # writing now fails
        with self.assertRaises(OSError):
            state_tx.durable_write_all(w, payload)
        os.close(w)

    def test_durable_write_all_loops_until_complete(self):
        path = os.path.join(self._tmp, "chunked.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        real_write = os.write
        calls = []

        def one_byte(fdesc, buf):
            calls.append(len(buf))
            return real_write(fdesc, bytes(buf[:1]))
        try:
            with patch("os.write", side_effect=one_byte):
                state_tx.durable_write_all(fd, b"abcdef")
        finally:
            os.close(fd)
        self.assertEqual(open(path, "rb").read(), b"abcdef")
        self.assertEqual(len(calls), 6, "the writer trusted a short return")

    def test_a_zero_byte_write_is_an_error_not_a_spin(self):
        with patch("os.write", return_value=0):
            with self.assertRaises(OSError):
                state_tx.durable_write_all(1, b"abc")

    def test_eintr_is_retried_not_reported_as_failure(self):
        import errno
        path = os.path.join(self._tmp, "eintr.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        real_write, state = os.write, {"raised": False}

        def flaky(fdesc, buf):
            if not state["raised"]:
                state["raised"] = True
                raise OSError(errno.EINTR, "interrupted")
            return real_write(fdesc, buf)
        try:
            with patch("os.write", side_effect=flaky):
                state_tx.durable_write_all(fd, b"payload")
        finally:
            os.close(fd)
        self.assertEqual(open(path, "rb").read(), b"payload")

    def test_a_truncated_temp_file_is_not_promoted(self):
        path = _p("short.json")
        real_getsize = os.path.getsize

        def lying_size(p):
            if str(p).endswith(".tmp"):
                return 1
            return real_getsize(p)
        with patch("os.path.getsize", side_effect=lying_size):
            self.assertFalse(JsonStore.save(path, {"a": 1}))
        self.assertFalse(os.path.exists(path),
                         "a truncated write was promoted over the target")

    def test_a_failing_directory_fsync_fails_the_write(self):
        import errno
        path = _p(EL.LEDGER_FILE)

        def bad_fsync(fd):
            raise OSError(errno.EIO, "directory fsync failed")
        with patch("state_tx.os.fsync", side_effect=bad_fsync):
            self.assertFalse(JsonStore.save(path, {"a": 1}),
                             "an unsynced rename was reported as durable")

    def test_an_unsupported_directory_fsync_is_tolerated(self):
        """A platform that cannot fsync a directory says EINVAL. That is a
        capability statement, not an I/O error, and must not fail closed."""
        import errno
        real_fsync = os.fsync
        seen = {"n": 0}

        def picky(fd):
            seen["n"] += 1
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(errno.EINVAL, "not supported")
            return real_fsync(fd)
        path = _p("tolerated.json")
        with patch("state_tx.os.fsync", side_effect=picky):
            self.assertTrue(JsonStore.save(path, {"a": 1}))

    def test_a_continuity_append_that_cannot_be_synced_raises(self):
        import errno
        from continuity import ChainError
        chain = self.chain()
        with patch("state_tx.os.fsync",
                   side_effect=OSError(errno.EIO, "no sync")):
            with self.assertRaises(ChainError):
                chain.append("evidence", {"settled_count": 1}, "t")


# ── A19: a stale reader may not decide ─────────────────────────────────
class StaleReadersCannotDecide(AstraCase):

    def two_views(self):
        """Two independent views of the SAME durable ledger, both current."""
        client, tlog, pos = self.stack()
        led_a = self.reconciled_ledger(tlog, pos)
        self.lose(tlog, led_a)
        self.assertTrue(led_a.save())
        led_b, _, _ = self.reload()          # a second process's view
        self.assertTrue(led_b.authority_is_current()[0])
        self.assertTrue(led_a.authority_is_current()[0])
        return led_a, led_b

    def test_a_reader_behind_the_durable_generation_blocks_capital(self):
        led_a, led_b = self.two_views()
        self.assertTrue(led_b.save())        # B advances the authority
        ok, reason = led_a.authority_is_current()
        self.assertFalse(ok, "A believes it is current after B committed")
        self.assertIn("STALE_READER", reason)
        self.assertIn(EL.GUARD_STALE_READER, led_a.guards())
        self.assertFalse(led_a.capital_eligible())

    def test_a_stale_reader_cannot_run_an_operator_action(self):
        led_a, led_b = self.two_views()
        led_a.state["capital_hold"] = {"reason": "post_rebase_validation",
                                       "since": "t", "rebase_id": "rb-1",
                                       "released_by": None}
        led_a.state["rebases"].append({"rebase_id": "rb-1",
                                       "operator_action_id": "OPS-R",
                                       "validation": None})
        self.assertTrue(led_a.save())
        prop = led_a.propose_hold_release("OPS-REL", "vref")
        # a second process commits, so A's view is now superseded
        led_b, _, _ = self.reload()
        self.assertTrue(led_b.save())
        self.assertFalse(led_a.apply_hold_release("OPS-REL", "vref",
                                                  prop["token"]))
        self.assertIsNotNone(led_a.state.get("capital_hold"))

    def test_require_current_authority_raises_for_a_stale_view(self):
        led_a, led_b = self.two_views()
        self.assertTrue(led_b.save())
        with self.assertRaises(StaleAuthority):
            led_a.require_current_authority("test")

    def test_a_fresh_reload_clears_the_stale_state(self):
        led_a, led_b = self.two_views()
        self.assertTrue(led_b.save())
        self.assertIn(EL.GUARD_STALE_READER, led_a.guards())
        refreshed, _, _ = self.reload()
        self.assertNotIn(EL.GUARD_STALE_READER, refreshed.guards())


# ── A20: state is bound to an account and an environment ───────────────
class DurableStateIsBoundToItsAccount(AstraCase):

    DEMO = {"env": "demo", "base_url": "https://demo-api.kalshi.co/trade-api/v2",
            "key_id": "demo-key-1"}
    PROD = {"env": "prod", "base_url": "https://api.elections.kalshi.com/trade-api/v2",
            "key_id": "prod-key-1"}
    PROD_B = {"env": "prod", "base_url": "https://api.elections.kalshi.com/trade-api/v2",
              "key_id": "prod-key-2"}

    def bound(self, spec):
        return account_binding.fingerprint(**spec)

    def seed_under(self, spec):
        client, tlog, pos = self.stack()
        led = EquityLedger(tlog, pos, env=spec["env"],
                           binding=self.bound(spec))
        prop = led.propose_seed(10.0, PRE_AT, "evidence-ref", 10.0)
        self.assertTrue(led.apply_seed(prop, prop["sha256"]))
        return led

    def load_under(self, spec):
        client, tlog, pos = self.stack()
        return EquityLedger(tlog, pos, env=spec["env"],
                            binding=self.bound(spec))

    def test_demo_state_loaded_under_prod_blocks_capital(self):
        self.seed_under(self.DEMO)
        led = self.load_under(self.PROD)
        self.assertEqual(led.binding_status, "mismatch")
        self.assertIn(EL.GUARD_ACCOUNT_BINDING, led.guards())
        self.assertFalse(led.capital_eligible())

    def test_prod_state_loaded_under_another_prod_account_blocks(self):
        self.seed_under(self.PROD)
        led = self.load_under(self.PROD_B)
        self.assertEqual(led.binding_status, "credential_changed")
        self.assertIn(EL.GUARD_ACCOUNT_BINDING, led.guards())
        self.assertFalse(led.capital_eligible())

    def test_the_same_account_loads_cleanly(self):
        self.seed_under(self.PROD)
        led = self.load_under(self.PROD)
        self.assertEqual(led.binding_status, "match")
        self.assertNotIn(EL.GUARD_ACCOUNT_BINDING, led.guards())

    def test_a_rotation_can_be_acknowledged_for_that_exact_fingerprint(self):
        self.seed_under(self.PROD)
        current = self.bound(self.PROD_B)
        env = {account_binding.REBIND_ACK_VAR: account_binding.digest(current)}
        with patch.dict(os.environ, env):
            led = self.load_under(self.PROD_B)
        self.assertEqual(led.binding_status, "match")

    def test_an_acknowledgement_for_a_different_fingerprint_does_not_apply(self):
        self.seed_under(self.PROD)
        env = {account_binding.REBIND_ACK_VAR: "not-this-one"}
        with patch.dict(os.environ, env):
            led = self.load_under(self.PROD_B)
        self.assertEqual(led.binding_status, "credential_changed")
        self.assertFalse(led.capital_eligible())

    def test_legacy_unbound_state_is_adopted_not_silently_trusted(self):
        led = self.seed_under(self.PROD)
        state = JsonStore.load(_p(EL.LEDGER_FILE), {})
        state.pop("binding", None)
        JsonStore.save(_p(EL.LEDGER_FILE), state)
        again = self.load_under(self.PROD)
        self.assertEqual(again.binding_status, "unbound")
        self.assertNotIn(EL.GUARD_ACCOUNT_BINDING, again.guards())

    def test_no_secret_material_is_stored(self):
        led = self.seed_under(self.PROD)
        blob = json.dumps(led.state)
        self.assertNotIn("prod-key-1", blob)
        self.assertIn("api_host", blob)
