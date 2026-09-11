"""A30/A34/A35: real engine construction and process locks, synthetic broker.

No send-capable adapter exists in these tests. All state and process races use
temporary directories; PID-file contents never substitute for OS ownership.
"""
import json
import multiprocessing as mp
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import _gates  # noqa: F401 -- before production imports
from config import CFG, _p
from execution_engine import ExecutionEngine
from persistence import PersistenceSentinel
from state_authority import AuthorityError, WriterLease


class ObservationOnlyBroker:
    env = "demo"
    base_url = "synthetic://lease-tests"
    cred_src = "synthetic"

    def get_balance(self):
        return 100.0

    def list_orders(self):
        return []

    def get_positions(self):
        return []

    def get_positions_proof(self):
        return {"complete": True, "rows": [], "pages": 1, "cursors": []}

    def get_market(self, _ticker):
        return {}

    def create_order(self, *args, **kwargs):
        raise AssertionError("broker writes forbidden in lease tests")

    cancel_order = create_order


def configure_synthetic(root):
    CFG.DATA_DIR = root
    CFG.BROKER_ACCOUNT_ID = "synthetic-lease-account"
    CFG.EXECUTION_MODE = "demo"
    CFG.SHADOW_MODE = True
    CFG.DRY_RUN = True
    CFG.ALLOW_ORDER_SUBMISSION = False
    CFG.API_PARALLEL_ENABLED = False
    CFG.REQUIRE_PERSISTENT_STATE = False
    PersistenceSentinel.reset()


def engine_process(root, pipe, release, barrier=None):
    configure_synthetic(root)
    if barrier is not None:
        barrier.wait(timeout=15)
    engine = None
    try:
        engine = ExecutionEngine(ObservationOnlyBroker(), 100.0)
        pipe.send(("owner", os.getpid(), engine._writer_lease.valid()))
        if not release.wait(timeout=20):
            raise RuntimeError("parent failed to finish bounded engine test")
    except AuthorityError:
        pipe.send(("blocked", os.getpid(), False))
    except BaseException as exc:
        pipe.send(("unexpected", os.getpid(), repr(exc)))
    finally:
        if engine is not None:
            engine.close()
        pipe.close()


class WriterLeaseLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-lease-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        values = {key: getattr(CFG, key) for key in (
            "DATA_DIR", "BROKER_ACCOUNT_ID", "EXECUTION_MODE", "SHADOW_MODE",
            "DRY_RUN", "ALLOW_ORDER_SUBMISSION", "API_PARALLEL_ENABLED",
            "REQUIRE_PERSISTENT_STATE")}
        self.addCleanup(lambda: [setattr(CFG, key, value)
                                for key, value in values.items()])
        self.addCleanup(PersistenceSentinel.reset)
        configure_synthetic(self.root)

    def engine(self):
        engine = ExecutionEngine(ObservationOnlyBroker(), 100.0)
        # Also permits the two constructor invariants to run on the rejected
        # baseline, whose engine did not yet expose a lifetime close method.
        self.addCleanup(lambda: getattr(engine, "close", lambda: None)())
        return engine

    def process(self, barrier=None):
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=False)
        release = ctx.Event()
        proc = ctx.Process(target=engine_process,
                           args=(self.root, child, release, barrier))
        proc.start()
        child.close()

        def cleanup():
            # A SIGKILL may leave an Event's internal condition unrecoverable.
            # Dead children need no notification and own no surviving lease.
            if proc.is_alive():
                release.set()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(5)
            parent.close()

        self.addCleanup(cleanup)
        return proc, parent, release

    def receive(self, pipe):
        self.assertTrue(pipe.poll(15), "engine child did not finish construction")
        result = pipe.recv()
        self.assertNotEqual(result[0], "unexpected", result)
        return result

    def test_real_engine_acquires_before_first_economic_load(self):
        import execution_engine
        real_logger = execution_engine.TradeLogger
        observed = []

        def inspect_first_load():
            with self.assertRaises(AuthorityError):
                WriterLease(_p("equity_ledger.json"))
            observed.append(True)
            return real_logger()

        with patch.object(execution_engine, "TradeLogger", side_effect=inspect_first_load):
            engine = self.engine()
        self.assertEqual(observed, [True])
        self.assertTrue(engine._writer_lease.valid())

    def test_explicit_authority_reaches_client_before_economic_load(self):
        client = ObservationOnlyBroker()
        supplied_authority = object()

        def inspect_first_load():
            self.assertIs(client.continuity_authority, supplied_authority)
            self.assertTrue(client._engine_writer_lease.valid())
            raise RuntimeError("synthetic stop after verifying wiring")

        with patch("execution_engine.TradeLogger", side_effect=inspect_first_load):
            with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                ExecutionEngine(client, 100.0, continuity_authority=supplied_authority)
        self.assertFalse(client._engine_writer_lease.valid())

    def test_second_engine_same_process_fails_before_loading_components(self):
        first = self.engine()
        with patch("execution_engine.TradeLogger") as constructor:
            with self.assertRaises(AuthorityError):
                ExecutionEngine(ObservationOnlyBroker(), 100.0)
        constructor.assert_not_called()
        self.assertTrue(first._writer_lease.valid())

    def test_two_simultaneous_real_engines_have_exactly_one_owner(self):
        barrier = mp.get_context("spawn").Barrier(2)
        first, pipe1, release1 = self.process(barrier)
        second, pipe2, release2 = self.process(barrier)
        results = [self.receive(pipe1), self.receive(pipe2)]
        self.assertEqual(sorted(row[0] for row in results), ["blocked", "owner"])
        self.assertTrue(next(row[2] for row in results if row[0] == "owner"))
        release1.set()
        release2.set()
        first.join(10)
        second.join(10)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_second_process_after_first_running_fails_before_loading(self):
        _proc, pipe, _release = self.process()
        self.assertEqual(self.receive(pipe)[0], "owner")
        with patch("execution_engine.TradeLogger") as constructor:
            with self.assertRaises(AuthorityError):
                ExecutionEngine(ObservationOnlyBroker(), 100.0)
        constructor.assert_not_called()

    def test_process_crash_releases_lease_and_real_engine_restarts(self):
        proc, pipe, _release = self.process()
        self.assertEqual(self.receive(pipe)[0], "owner")
        proc.kill()  # SIGKILL: no destructor, close, or finally can run.
        proc.join(10)
        self.assertEqual(proc.exitcode, -signal.SIGKILL)
        restarted = self.engine()
        self.assertTrue(restarted._writer_lease.valid())
        self.assertEqual(restarted._writer_lease.pid, os.getpid())

    def test_fork_child_cannot_use_engine_or_release_parent_authority(self):
        engine = self.engine()
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                valid = engine._writer_lease.valid()
                guard = engine._evaluate_global_guards()
                engine.close()
                try:
                    ExecutionEngine(ObservationOnlyBroker(), 100.0)
                    blocked = False
                except AuthorityError:
                    blocked = True
                os.write(write_fd, json.dumps([valid, guard, blocked]).encode())
                os._exit(0)
            except BaseException:
                os._exit(91)
        os.close(write_fd)
        try:
            self.assertTrue(select.select([read_fd], [], [], 10)[0])
            result = json.loads(os.read(read_fd, 4096))
            self.assertEqual(result, [False, [False, "stale_engine_writer_lease"], True])
        finally:
            os.close(read_fd)
            done, status = os.waitpid(child, os.WNOHANG)
            if not done:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
        self.assertTrue(engine._writer_lease.valid())

    def test_stale_pid_text_does_not_block_construction(self):
        Path(self.root, ".atlas-engine-writer.lock").write_text('{"pid":2147483647}')
        self.assertTrue(self.engine()._writer_lease.valid())

    def test_reused_live_pid_text_has_no_ownership_authority(self):
        # Feasible PID reuse simulation: even a currently live PID recorded by
        # an earlier process is ignored. Kernel PID allocation need not change.
        Path(self.root, ".atlas-engine-writer.lock").write_text(
            json.dumps({"pid": os.getpid()}))
        self.assertTrue(self.engine()._writer_lease.valid())

    def test_wrong_pid_text_cannot_release_a_live_owner(self):
        first = self.engine()
        Path(first._writer_lease.path).write_text('{"pid":2147483647}')
        with self.assertRaises(AuthorityError):
            ExecutionEngine(ObservationOnlyBroker(), 100.0)
        self.assertTrue(first._writer_lease.valid())

    def test_failed_construction_releases_the_acquired_lease(self):
        with patch("execution_engine.TradeLogger", side_effect=RuntimeError("synthetic load failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic load failure"):
                ExecutionEngine(ObservationOnlyBroker(), 100.0)
        self.assertTrue(self.engine()._writer_lease.valid())

    def test_fatal_initialization_releases_the_acquired_lease(self):
        with patch("execution_engine.TradeLogger", side_effect=SystemExit(2)):
            with self.assertRaises(SystemExit):
                ExecutionEngine(ObservationOnlyBroker(), 100.0)
        self.assertTrue(self.engine()._writer_lease.valid())

    def test_close_revokes_old_engine_before_new_engine_construction(self):
        first = self.engine()
        first.close()
        self.assertEqual(first._evaluate_global_guards(), (False, "stale_engine_writer_lease"))
        self.assertTrue(self.engine()._writer_lease.valid())

    def test_missing_lease_on_initialized_engine_fails_closed(self):
        engine = self.engine()
        lease = engine._writer_lease
        del engine._writer_lease
        try:
            self.assertEqual(engine._evaluate_global_guards(), (False, "stale_engine_writer_lease"))
        finally:
            engine._writer_lease = lease

    def test_canonical_alias_cannot_create_a_second_authority(self):
        self.engine()
        alias = Path(self.root, "alias")
        alias.symlink_to(self.root, target_is_directory=True)
        with patch.object(CFG, "DATA_DIR", str(alias)):
            with self.assertRaises(AuthorityError):
                ExecutionEngine(ObservationOnlyBroker(), 100.0)

    def test_different_state_roots_can_have_distinct_engines(self):
        first = self.engine()
        with tempfile.TemporaryDirectory(prefix="atlas-other-lease-") as other:
            with patch.object(CFG, "DATA_DIR", other):
                second = ExecutionEngine(ObservationOnlyBroker(), 100.0)
                try:
                    self.assertTrue(first._writer_lease.valid())
                    self.assertTrue(second._writer_lease.valid())
                finally:
                    second.close()

    def test_configuration_rebind_does_not_move_existing_engine_authority(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory(prefix="atlas-rebound-root-") as other:
            with patch.object(CFG, "DATA_DIR", other):
                self.assertEqual(engine._evaluate_global_guards(),
                                 (False, "stale_engine_writer_lease"))

    def test_closed_or_replaced_descriptor_is_not_effective_authority(self):
        engine = self.engine()
        os.close(engine._writer_lease.fd)
        self.assertFalse(engine._writer_lease.valid())
        self.assertEqual(engine._evaluate_global_guards(), (False, "stale_engine_writer_lease"))

    def test_stale_descriptor_owner_cannot_close_a_reused_lease_descriptor(self):
        first = WriterLease(_p("equity_ledger.json"))
        os.close(first.fd)
        second = WriterLease(_p("equity_ledger.json"))
        self.addCleanup(second.close)
        first.close()
        self.assertTrue(second.valid())

    def test_process_lease_is_not_inheritable_across_exec(self):
        lease = self.engine()._writer_lease
        self.assertFalse(os.get_inheritable(lease.fd))

    def test_authority_guards_survive_independent_accounting_accumulation(self):
        ledger = self.engine().equity
        authority = ("synthetic-authority-one", "synthetic-authority-two")
        with patch.object(ledger, "_authority_guards", return_value=authority):
            guards = ledger.guards()
        self.assertTrue(set(authority).issubset(guards))
        self.assertGreater(len(guards), len(authority))
        self.assertEqual(authority, ("synthetic-authority-one", "synthetic-authority-two"))

    def test_returned_guards_cannot_mutate_or_discard_authority_guards(self):
        ledger = self.engine().equity
        expected = ledger._authority_guards()
        self.assertIsInstance(expected, tuple)
        guards = ledger.guards()
        guards.clear()
        self.assertTrue(set(expected).issubset(ledger.guards()))

    def test_test_bootstrap_avoids_default_repository_lock_artifacts(self):
        repo = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.pop("DATA_DIR", None)
        code = """import os, _bootstrap
from config import CFG, _p
from state_authority import root_lock
with root_lock(_p('synthetic.json')):
    assert os.path.realpath(CFG.DATA_DIR) != os.path.realpath(os.getcwd())
print('ISOLATED_TEST_ROOT')
"""
        before = {p.name for p in repo.glob(".atlas*lock")}
        result = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ISOLATED_TEST_ROOT", result.stdout)
        self.assertEqual({p.name for p in repo.glob(".atlas*lock")}, before)


if __name__ == "__main__":
    unittest.main()
