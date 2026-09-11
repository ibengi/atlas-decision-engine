"""Actual process-death transport recovery with an independent parent authority.

Spawned children never receive the authority's signing key or mutable checkpoint.
Two unidirectional OS pipes carry synthetic RPC; no sockets or broker transports
exist here. The parent also retains synthetic broker history across child death.
"""
import copy
import hashlib
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import threading
import traceback
import unittest


def _post(client, client_order_id):
    return client._req("POST", "/portfolio/events/orders", retries=8, json={
        "client_order_id": client_order_id, "ticker": "KXBTC15M-PROCESS",
        "side": "bid", "count": "1", "price": "0.2500",
        "time_in_force": "good_till_canceled",
    })


class _AuthorityProxy:
    def __init__(self, request_sender, response_receiver):
        self.request_sender = request_sender
        self.response_receiver = response_receiver

    def call(self, operation, *args):
        self.request_sender.send((operation, args))
        if not self.response_receiver.poll(10):
            raise RuntimeError("synthetic parent authority RPC timed out")
        ok, value = self.response_receiver.recv()
        if not ok:
            raise RuntimeError("synthetic parent authority refused: " + value)
        return value

    def verify_current(self, request):
        return self.call("verify_current", request)

    def advance(self, previous, candidate):
        return self.call("advance", previous, candidate)

    def attest_account(self, request, credential_identity=None):
        return self.call("attest_account", request, credential_identity)


def _process_worker(root, identity, policy, request_sender, response_receiver,
                    result_sender, action, attempt_second):
    """Top-level spawn target. os._exit deliberately skips Python cleanup."""
    try:
        from config import CFG, _p
        from continuity_authority import configure_trust
        from kalshi_client import KalshiAPIError
        from persistence import JsonStore, PersistenceSentinel
        from state_authority import WriterLease, recovery_problem
        import transport_intent as ti
        from test_transport_lifecycle import MemoryBroker

        # Explicitly closed runtime policy; MemoryBroker has no network method.
        CFG.DATA_DIR = root
        CFG.BROKER_ACCOUNT_ID = identity["account_id"]
        CFG.ALLOW_ORDER_SUBMISSION = False
        CFG.LIVE_BROKER_WRITES_AUTHORIZED = False
        CFG.PROD_ACCESS_MODE = "READ_ONLY"
        PersistenceSentinel.reset()
        proxy = _AuthorityProxy(request_sender, response_receiver)
        # Host-owned public policy crosses spawn; the provider supplies no key.
        configure_trust(proxy, policy)

        class ParentHeldBroker(MemoryBroker):
            def send(self, verb, path, *, retries, **kwargs):
                if retries != 0:
                    raise AssertionError("mutation retry enabled")
                reply = proxy.call("broker_send", verb, path, kwargs)
                if action == "sent_crash":
                    os._exit(73)
                if reply["timeout"]:
                    raise KalshiAPIError(0, "synthetic timeout after possible handoff")
                return reply["response"]

            def find_orders_by_client_order_id(self, cid, *, ticker=None):
                return proxy.call("broker_find", cid, ticker)

        lease = WriterLease(_p(ti.FILE))
        client = ParentHeldBroker(proxy)
        client._engine_writer_lease = lease
        if action in ("prepared_crash", "unknown_crash"):
            original_move = ti._move

            def crash_at_transition(path, rows, key, state, *args, **kwargs):
                if action == "prepared_crash" and state == "SENT":
                    # PREPARED already committed and externally checkpointed.
                    os._exit(73)
                result = original_move(path, rows, key, state, *args, **kwargs)
                if action == "unknown_crash" and state == "UNKNOWN":
                    # Crash only AFTER the actual durable UNKNOWN transition.
                    os._exit(73)
                return result

            ti._move = crash_at_transition
        if action.endswith("crash"):
            _post(client, "first")
            raise AssertionError("required process crash point was not reached")

        initial = ti.reconcile_transport_intents(client)
        again = ti.reconcile_transport_intents(client)
        mutation_result = None
        if attempt_second:
            try:
                _post(client, "second")
                mutation_result = "accepted_synthetically"
            except KalshiAPIError:
                mutation_result = "blocked"
        result_sender.send({
            "pid": os.getpid(), "initial": initial, "again": again,
            "mutation_result": mutation_result,
            "rows": JsonStore.load(_p(ti.FILE), {}),
            "unresolved": ti.has_unresolved_transport(),
            "root_problem": recovery_problem(_p(ti.FILE)),
            "lease_owned": lease.valid(),
            "capital_enabled": CFG.PROD_ACCESS_MODE == "CAPITAL",
            "allow_order_submission": CFG.ALLOW_ORDER_SUBMISSION,
            "live_broker_writes_authorized": CFG.LIVE_BROKER_WRITES_AUTHORIZED,
        })
        lease.close()
    except BaseException:
        try:
            result_sender.send({"worker_error": traceback.format_exc()})
        finally:
            raise
    finally:
        request_sender.close()
        response_receiver.close()
        result_sender.close()


class _ParentAuthorityAndBroker:
    """Checkpoint genesis is fixed BEFORE any child writes, never disk-derived."""
    def __init__(self, identity):
        from authority_fixtures import CheckpointStore
        from continuity_authority import challenge, TrustPolicy
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from strict_data import dumps

        initial = {"version": 1, "generation": 0, "files": {}}
        digest = hashlib.sha256(dumps(initial, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
        self.authority = CheckpointStore(challenge(identity, 0, digest))
        self.policy = TrustPolicy(
            self.authority.authority_id,
            self.authority.signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
            frozenset({identity["fingerprint"]}), frozenset({identity["environment"]}),
        )
        self.orders = []
        self.calls = []
        self.visible = True
        self.mode = "success"
        self.errors = []

    def handle(self, operation, args):
        if operation in ("verify_current", "advance", "attest_account"):
            return getattr(self.authority, operation)(*args)
        if operation == "broker_find":
            cid, ticker = args
            return copy.deepcopy([row for row in self.orders
                if row["client_order_id"] == cid and row["ticker"] == ticker]) if self.visible else []
        if operation == "broker_send":
            verb, path, kwargs = args
            if verb != "POST" or path != "/portfolio/events/orders":
                raise AssertionError("unsupported synthetic mutation")
            self.calls.append(copy.deepcopy((verb, path, kwargs)))
            body = copy.deepcopy(kwargs["json"])
            row = {**body, "order_id": "synthetic-" + body["client_order_id"],
                   "initial_count": body["count"], "remaining_count": body["count"],
                   "status": "resting"}
            if self.mode != "timeout_absent":
                self.orders.append(row)
            return {"timeout": self.mode.startswith("timeout"), "response": {
                "order": {"order_id": row["order_id"],
                          "client_order_id": row["client_order_id"]}}}
        raise AssertionError("unknown synthetic RPC operation: " + operation)

    def serve(self, request_receiver, response_sender, stop):
        try:
            while not stop.is_set():
                if not request_receiver.poll(.1):
                    continue
                try:
                    operation, args = request_receiver.recv()
                except EOFError:
                    return
                try:
                    response = (True, self.handle(operation, args))
                except Exception as exc:
                    self.errors.append(type(exc).__name__ + ": " + str(exc))
                    response = (False, self.errors[-1])
                response_sender.send(response)
        except (BrokenPipeError, EOFError, OSError) as exc:
            if not stop.is_set():
                self.errors.append("RPC failed: " + str(exc))


class TransportProcessRecovery(unittest.TestCase):
    def setUp(self):
        from continuity_authority import account_identity
        self.directory = tempfile.TemporaryDirectory(prefix="atlas-transport-process-")
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        self.identity = account_identity("kalshi", "prod", "process-synthetic-account")
        self.parent = _ParentAuthorityAndBroker(self.identity)
        self.context = mp.get_context("spawn")

    def run_child(self, action, *, attempt_second=False):
        # duplex=False uses unidirectional OS pipes, not Unix-domain socketpair.
        request_receiver, request_sender = self.context.Pipe(duplex=False)
        response_receiver, response_sender = self.context.Pipe(duplex=False)
        result_receiver, result_sender = self.context.Pipe(duplex=False)
        stop = threading.Event()
        server = threading.Thread(target=self.parent.serve,
            args=(request_receiver, response_sender, stop), daemon=True)
        child = self.context.Process(target=_process_worker, args=(
            self.root, self.identity, self.parent.policy, request_sender,
            response_receiver, result_sender, action, attempt_second))
        try:
            child.start()
            request_sender.close()
            response_receiver.close()
            result_sender.close()
            server.start()
            child.join(20)
            if child.is_alive():
                child.terminate()
                child.join(5)
                self.fail("synthetic transport child did not finish")
            result = None
            if result_receiver.poll(1):
                try:
                    result = result_receiver.recv()
                except EOFError:
                    pass
            expected_exit = 73 if action.endswith("crash") else 0
            self.assertEqual(child.exitcode, expected_exit, result)
            self.assertFalse(self.parent.errors, self.parent.errors)
            if expected_exit == 0:
                self.assertIsNotNone(result)
                self.assertNotIn("worker_error", result)
                self.assertTrue(result["lease_owned"])
                self.assertIsNone(result["root_problem"])
                self.assertFalse(result["capital_enabled"])
                self.assertFalse(result["allow_order_submission"])
                self.assertFalse(result["live_broker_writes_authorized"])
            return result
        finally:
            stop.set()
            if server.ident is not None:
                server.join(3)
            for connection in (request_receiver, request_sender, response_receiver,
                               response_sender, result_receiver, result_sender):
                connection.close()
            if child.pid is not None and child.is_alive():
                child.terminate()
                child.join(5)

    def stored_rows(self):
        from strict_data import loads
        return loads(Path(self.root, "transport_intents.json").read_bytes())

    def assert_one_state(self, state):
        rows = self.stored_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(next(iter(rows.values()))["state"], state)
        return rows

    def test_process_death_after_prepared_recovers_without_dispatch(self):
        self.run_child("prepared_crash")
        self.assert_one_state("PREPARED")
        before_checkpoint = self.parent.authority.current
        self.assertEqual(self.parent.calls, [])
        result = self.run_child("reconcile")
        self.assert_one_state("CONFIRMED_NOT_APPLIED")
        self.assertFalse(result["unresolved"])
        self.assertEqual(self.parent.calls, [])
        self.assertGreater(self.parent.authority.current[1], before_checkpoint[1])

    def test_process_death_after_sent_resolves_parent_observed_order(self):
        self.run_child("sent_crash")
        self.assert_one_state("SENT")
        self.assertEqual(len(self.parent.calls), 1)
        self.assertEqual(len(self.parent.orders), 1)
        result = self.run_child("reconcile")
        self.assert_one_state("CONFIRMED_APPLIED")
        self.assertFalse(result["unresolved"])
        self.assertEqual(len(self.parent.calls), 1)
        self.assertEqual(result["initial"], result["again"])

    def test_process_death_unknown_and_empty_reads_never_resends(self):
        self.parent.mode = "timeout_absent"
        self.run_child("unknown_crash")
        self.assert_one_state("UNKNOWN")
        result = self.run_child("reconcile", attempt_second=True)
        self.assert_one_state("UNKNOWN")
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["mutation_result"], "blocked")
        self.assertEqual(len(self.parent.calls), 1)
        self.assertEqual(self.parent.orders, [])

    def test_delayed_visibility_across_process_restart_allows_order_two(self):
        self.parent.mode, self.parent.visible = "timeout_present", False
        self.run_child("unknown_crash")
        self.assert_one_state("UNKNOWN")
        blocked = self.run_child("reconcile", attempt_second=True)
        self.assertEqual(blocked["mutation_result"], "blocked")
        self.assertEqual(len(self.parent.calls), 1)
        self.parent.mode, self.parent.visible = "success", True
        resolved = self.run_child("reconcile", attempt_second=True)
        self.assertEqual(resolved["mutation_result"], "accepted_synthetically")
        self.assertFalse(resolved["unresolved"])
        self.assertEqual(len(self.parent.calls), 2)
        self.assertEqual(len(self.parent.orders), 2)
        rows = self.stored_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["state"] == "CONFIRMED_APPLIED" for row in rows.values()))
        self.assertEqual({row["request"]["json"]["client_order_id"] for row in rows.values()},
                         {"first", "second"})


if __name__ == "__main__":
    unittest.main()
