"""M3 isolated lifecycle tests; synthetic broker ledger and zero network writes."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from test_engine_authority import AuthorityCase
from authority_fixtures import SyntheticEvidenceSigner
from config import CFG, _p
from kalshi_client import KalshiAPIError, KalshiClient
from order_manager import OrderManager
from persistence import JsonStore, PersistenceSentinel
from state_authority import manifest, WriterLease
from transport_intent import (BeforeSendFailure, FILE, TERMINAL, _digest, _move,
    _save, _validate, durable_transport, has_unresolved_transport,
    reconcile_transport_intents)


class MemoryBroker:
    env = "prod"
    def __init__(self, authority):
        self.continuity_authority = authority
        self.orders = []
        self.calls = []
        self.mode = "success"
        self.visible = True
        self.read_callback = None
        self._req = durable_transport(type(self).send).__get__(self)
    def _assert_broker_write_allowed(self, operation):
        # This class has no network implementation or live credentials.
        assert operation.startswith(("POST", "DELETE"))
    def send(self, verb, path, *, retries, **kwargs):
        assert retries == 0
        self.calls.append((verb, path, copy.deepcopy(kwargs)))
        if self.mode == "before_send":
            raise BeforeSendFailure("synthetic connection not handed to network")
        if self.mode == "rejection":
            raise KalshiAPIError(422, "synthetic explicit rejection")
        if verb == "DELETE":
            oid = path.rsplit("/", 1)[-1]
            for order in self.orders:
                if order["order_id"] == oid:
                    order.update(status="canceled", remaining_count=0)
            return {"order_id": oid, "reduced_by": 1}
        body = copy.deepcopy(kwargs["json"])
        row = {**body, "order_id": "order-" + body["client_order_id"],
               "initial_count": body["count"], "remaining_count": body["count"],
               "status": "resting"}
        if self.mode not in ("timeout_absent", "connection_unknown"):
            self.orders.append(row)
        if self.mode.startswith("timeout"):
            raise KalshiAPIError(0, "synthetic timeout after possible send")
        if self.mode == "connection_unknown":
            raise requests.ConnectionError("synthetic ambiguous connection")
        return {"order": {"order_id": row["order_id"], "client_order_id": body["client_order_id"]}}
    def find_orders_by_client_order_id(self, cid, *, ticker=None):
        if self.read_callback:
            self.read_callback()
        return copy.deepcopy([r for r in self.orders if r["client_order_id"] == cid and
                              r["ticker"] == ticker]) if self.visible else []
    def get_order(self, oid):
        rows = [o for o in self.orders if o["order_id"] == oid]
        if not rows:
            raise KalshiAPIError(404, "synthetic not visible")
        return copy.deepcopy(rows[0])


class FinalEvidence(SyntheticEvidenceSigner):
    def __init__(self, identity, outcome, *, final=True, complete=True, future=False, old=False):
        super().__init__(identity)
        self.outcome, self.final, self.complete = outcome, final, complete
        self.future, self.old = future, old
        self.callback = None
    def observe(self, request, row):
        if self.callback:
            self.callback()
        claims = {"intent_id": row["intent_id"], "request_digest": row["digest"],
                  "operation": row["operation"], "outcome": self.outcome, "final": self.final,
                  "complete_history": self.complete, "no_future_acceptance": not self.future,
                  "observed_through": row.get("sent_at", row["created_at"]) + (-1 if self.old else 1)}
        return self.sign_evidence(request, "transport_outcome", claims)


class TransportLifecycle(AuthorityCase):
    def setUp(self):
        super().setUp()
        self.prove()
        self.client = MemoryBroker(self.authority)
        # Real policy remains OFF: only the in-memory protocol above can mutate.
        p = patch.object(CFG, "ALLOW_ORDER_SUBMISSION", False)
        p.start(); self.addCleanup(p.stop)
    def post(self, cid="one", *, client=None):
        return (client or self.client)._req("POST", "/portfolio/events/orders", retries=7,
            json={"client_order_id": cid, "ticker": "KXBTC15M-" + cid, "side": "bid",
                  "count": "1", "price": "0.2500", "time_in_force": "good_till_canceled"})
    def rows(self):
        return JsonStore.load(_p(FILE), {})
    def row(self):
        return next(iter(self.rows().values()))
    def unknown(self, mode="timeout_absent"):
        self.client.mode = mode
        self.client.visible = False
        with self.assertRaises((KalshiAPIError, requests.ConnectionError)):
            self.post()
        self.assertEqual(self.row()["state"], "UNKNOWN")
    def resolve(self):
        return reconcile_transport_intents(self.client)
    def evidence(self, outcome, **kwargs):
        provider = FinalEvidence(self.ledger.identity, outcome, **kwargs)
        self.client.transport_evidence_provider = provider
        return provider

    def test_successful_order_is_independently_confirmed(self):
        self.post()
        self.assertEqual(self.row()["state"], "CONFIRMED_APPLIED")
        self.assertEqual([e["to"] for e in self.row()["history"]],
                         ["PREPARED", "SENT", "ACKNOWLEDGED", "RECONCILING", "CONFIRMED_APPLIED"])
        self.assertFalse(has_unresolved_transport())
        self.assertEqual(len(self.client.calls), 1)

    def test_two_consecutive_legitimate_orders(self):
        self.post("one")
        first = copy.deepcopy(self.row())
        self.post("two")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["state"] == "CONFIRMED_APPLIED" for r in self.rows().values()))
        self.assertEqual(self.rows()[first["intent_id"]], first)
        self.assertNotIn("transport_outcome_unresolved", self.ledger.guards())

    def test_definite_broker_rejection_is_terminal_with_final_evidence(self):
        self.client.mode = "rejection"
        self.evidence("TERMINAL_FAILED")
        with self.assertRaises(KalshiAPIError): self.post("one")
        self.assertEqual(self.row()["state"], "TERMINAL_FAILED")
        self.client.mode = "success"
        self.post("two")
        self.assertEqual(len(self.client.calls), 2)

    def test_unproven_http_rejection_stays_unknown(self):
        self.client.mode = "rejection"
        with self.assertRaises(KalshiAPIError): self.post()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_connection_failure_proven_before_dispatch(self):
        self.client.mode = "before_send"
        with self.assertRaises(KalshiAPIError): self.post()
        self.assertEqual(self.row()["state"], "CONFIRMED_NOT_APPLIED")
        self.client.mode = "success"
        self.post("later")
        self.assertEqual(len(self.client.orders), 1)

    def test_actual_adapter_signing_failure_never_calls_session(self):
        client = SimpleNamespace(env="prod", continuity_authority=self.authority,
            _assert_broker_write_allowed=lambda _: None, _pk=object(), base_url="https://synthetic.invalid",
            _sign_headers=lambda *a: (_ for _ in ()).throw(ConnectionError("before dispatch")),
            session=SimpleNamespace(request=lambda *a, **k: self.fail("no session call")))
        client._req = KalshiClient._req.__get__(client)
        with self.assertRaises(KalshiAPIError): self.post(client=client)
        self.assertEqual(self.row()["state"], "CONFIRMED_NOT_APPLIED")

    def test_generic_connection_error_is_not_proof_of_no_send(self):
        self.unknown("connection_unknown")
        self.assertTrue(has_unresolved_transport())

    def test_timeout_after_possible_send_blocks_second_mutation(self):
        self.unknown("timeout_present")
        with self.assertRaises(KalshiAPIError): self.post("two")
        self.assertEqual(len(self.client.calls), 1)

    def test_delayed_visibility_then_presence_resolves(self):
        self.unknown("timeout_present")
        self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")
        self.client.visible = True
        self.resolve()
        self.assertEqual(self.row()["state"], "CONFIRMED_APPLIED")

    def test_restart_resumes_unknown_without_resending(self):
        self.unknown("timeout_present")
        self.client.visible = True
        restarted = OrderManager(self.client)
        # The real startup path calls this even without high-level pending rows.
        restarted.reconcile_startup(self.tlog, self.pos)
        self.assertEqual(self.row()["state"], "CONFIRMED_APPLIED")
        self.assertEqual(len(self.client.calls), 1)

    def test_duplicate_broker_rows_do_not_resolve(self):
        self.unknown("timeout_present")
        self.client.orders *= 2
        self.client.visible = True
        self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_repeated_reconciliation_keeps_terminal_bytes(self):
        self.post()
        before = Path(_p(FILE)).read_bytes()
        for _ in range(3): self.resolve()
        self.assertEqual(Path(_p(FILE)).read_bytes(), before)
        self.assertEqual(len(self.client.calls), 1)

    def test_confirmed_absence_unlocks_later_distinct_order(self):
        self.unknown()
        self.evidence("CONFIRMED_NOT_APPLIED")
        self.resolve()
        self.assertEqual(self.row()["state"], "CONFIRMED_NOT_APPLIED")
        self.client.mode, self.client.visible = "success", True
        self.post("two")
        self.assertEqual(len(self.client.calls), 2)

    def test_empty_complete_reads_never_prove_absence(self):
        self.unknown()
        for _ in range(4): self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")
        self.assertEqual(len(self.client.calls), 1)

    def test_absence_before_send_watermark_is_rejected(self):
        self.unknown(); self.evidence("CONFIRMED_NOT_APPLIED", old=True); self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_absence_without_finality_is_rejected(self):
        self.unknown(); self.evidence("CONFIRMED_NOT_APPLIED", final=False); self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_absence_with_future_acceptance_is_rejected(self):
        self.unknown(); self.evidence("CONFIRMED_NOT_APPLIED", future=True); self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_absence_with_incomplete_history_is_rejected(self):
        self.unknown(); self.evidence("CONFIRMED_NOT_APPLIED", complete=False); self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_unauthenticated_echo_outcome_is_rejected(self):
        self.unknown()
        self.client.transport_evidence_provider = SimpleNamespace(observe=lambda request, row: request)
        self.resolve()
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_terminal_economic_request_identity_cannot_be_reused(self):
        self.post()
        with self.assertRaises(KalshiAPIError): self.post()
        self.assertEqual(len(self.client.calls), 1)

    def test_contradictory_price_alias_blocks_presence(self):
        self.unknown("timeout_present"); self.client.visible = True
        self.client.orders[0]["yes_price"] = 90
        self.resolve(); self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_contradictory_action_blocks_presence(self):
        self.unknown("timeout_present"); self.client.visible = True
        self.client.orders[0]["action"] = "sell"
        self.resolve(); self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_missing_original_quantity_is_not_inferred_from_remaining(self):
        self.unknown("timeout_present"); self.client.visible = True
        for key in ("count", "initial_count"): self.client.orders[0].pop(key)
        self.resolve(); self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_payload_changed_on_broker_read_remains_unknown(self):
        self.unknown("timeout_present"); self.client.visible = True
        self.client.orders[0]["ticker"] = "different"
        self.resolve(); self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_cancellation_resolves_from_independent_terminal_read(self):
        self.post()
        oid = self.client.orders[0]["order_id"]
        self.client._req("DELETE", "/portfolio/events/orders/" + oid)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["state"] == "CONFIRMED_APPLIED" for r in self.rows().values()))

    def test_unresolved_intent_blocks_cancellation(self):
        self.unknown("timeout_present")
        with self.assertRaises(KalshiAPIError): self.client._req("DELETE", "/portfolio/events/orders/order-one")
        self.assertEqual(len(self.client.calls), 1)

    def test_persistence_failure_before_sent_prevents_dispatch(self):
        original = JsonStore.save
        def fail(path, rows, **kwargs):
            if path == _p(FILE) and any(r.get("state") == "SENT" for r in rows.values()): return False
            return original(path, rows, **kwargs)
        with patch.object(JsonStore, "save", side_effect=fail), self.assertRaises(KalshiAPIError): self.post()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.row()["state"], "PREPARED")
        self.assertFalse(PersistenceSentinel.healthy())

    def test_restart_prepared_proves_no_dispatch(self):
        # Stop after the actual PREPARED commit, before the durable SENT write.
        import transport_intent as ti
        move = ti._move
        def crash(path, rows, key, state, *args, **kwargs):
            if state == "SENT": raise SystemExit(73)
            return move(path, rows, key, state, *args, **kwargs)
        with patch.object(ti, "_move", side_effect=crash), self.assertRaises(SystemExit): self.post()
        self.assertEqual(self.row()["state"], "PREPARED")
        self.resolve()
        self.assertEqual(self.row()["state"], "CONFIRMED_NOT_APPLIED")
        self.assertEqual(self.client.calls, [])

    def test_reentrant_broker_writer_cannot_be_overwritten(self):
        self.unknown("timeout_present"); self.client.visible = True
        def change():
            self.client.read_callback = None
            self.assertTrue(JsonStore.save(_p("positions_state.json"), {"unexpected": {}}))
        self.client.read_callback = change
        result = self.resolve()
        self.assertIn("RECOVERY_REQUIRED", result)
        self.assertFalse(PersistenceSentinel.healthy())
        self.assertEqual(self.row()["state"], "RECONCILING")

    def test_closed_engine_client_cannot_prepare_transport(self):
        lease = WriterLease(_p(FILE)); self.client._engine_writer_lease = lease; lease.close()
        with self.assertRaises(KalshiAPIError): self.post()
        self.assertFalse(self.rows())

    def _proof_callback_at_state(self, state, change):
        original = self.authority.verify_current
        called = []
        def proof(request):
            path = Path(_p(FILE))
            rows = json.loads(path.read_text()) if path.exists() else {}
            if not called and any(row["state"] == state for row in rows.values()):
                called.append(True)
                change()
            return original(request)
        self.authority.verify_current = proof
        return called

    def _assert_handoff_binding_change_blocks(self, change):
        path = _p(FILE)
        called = self._proof_callback_at_state("SENT", change)
        with self.assertRaises(KalshiAPIError):
            self.post()
        self.assertEqual(called, [True])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(next(iter(JsonStore.load(path, {}).values()))["state"], "SENT")

    def test_proof_callback_lease_close_prevents_dispatch(self):
        lease = WriterLease(_p(FILE)); self.addCleanup(lease.close)
        self.client._engine_writer_lease = lease
        self._assert_handoff_binding_change_blocks(lease.close)

    def test_proof_callback_lease_rebind_prevents_dispatch(self):
        lease = WriterLease(_p(FILE)); self.addCleanup(lease.close)
        self.client._engine_writer_lease = lease
        self._assert_handoff_binding_change_blocks(
            lambda: setattr(self.client, "_engine_writer_lease", None))

    def test_proof_callback_environment_swap_prevents_dispatch(self):
        self._assert_handoff_binding_change_blocks(
            lambda: setattr(self.client, "env", "demo"))

    def test_proof_callback_account_swap_prevents_dispatch(self):
        self._assert_handoff_binding_change_blocks(
            lambda: setattr(CFG, "BROKER_ACCOUNT_ID", "account-B"))

    def test_proof_callback_credential_swap_prevents_dispatch(self):
        self.client.key_id = "synthetic-credential-A"
        self._assert_handoff_binding_change_blocks(
            lambda: setattr(self.client, "key_id", "synthetic-credential-B"))

    def test_proof_callback_authority_swap_prevents_dispatch(self):
        self._assert_handoff_binding_change_blocks(
            lambda: setattr(self.client, "continuity_authority", object()))

    def test_proof_callback_state_root_swap_prevents_dispatch(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="atlas-other-transport-root-") as other:
            self._assert_handoff_binding_change_blocks(
                lambda: setattr(CFG, "DATA_DIR", other))

    def test_proof_callback_lease_close_prevents_terminal_publication(self):
        self.unknown("timeout_present"); self.client.visible = True
        lease = WriterLease(_p(FILE)); self.addCleanup(lease.close)
        self.client._engine_writer_lease = lease
        called = self._proof_callback_at_state("RECONCILING", lease.close)
        self.assertIn("RECOVERY_REQUIRED", self.resolve())
        self.assertEqual(called, [True])
        self.assertEqual(self.row()["state"], "RECONCILING")
        self.assertEqual(len(self.client.calls), 1)

    def test_malformed_row_never_becomes_empty_on_restart(self):
        from authority_fixtures import corrupt_json
        corrupt_json(_p(FILE), {"unknown": {"state": "UNCLEAR"}})
        before = Path(_p(FILE)).read_bytes()
        self.assertIn("RECOVERY_REQUIRED", self.resolve())
        self.assertEqual(Path(_p(FILE)).read_bytes(), before)
        self.assertTrue(has_unresolved_transport())

    def test_transition_cannot_skip_independent_reconciliation(self):
        self.unknown()
        rows = self.rows(); key = next(iter(rows))
        with self.assertRaises(ValueError): _move(_p(FILE), rows, key, "CONFIRMED_APPLIED", "unsupported")
        self.assertEqual(self.row()["state"], "UNKNOWN")

    def test_terminal_rows_are_immutable_to_transition_writer(self):
        self.post(); rows = self.rows(); changed = copy.deepcopy(rows)
        next(iter(changed.values()))["request"]["json"]["count"] = "2"
        with self.assertRaises(ValueError): _save(_p(FILE), changed, expected=rows)
        self.assertEqual(self.rows(), rows)

    def test_direct_persistence_cannot_delete_unresolved_evidence(self):
        self.unknown()
        before = Path(_p(FILE)).read_bytes()
        self.assertFalse(JsonStore.save(_p(FILE), {}))
        self.assertEqual(Path(_p(FILE)).read_bytes(), before)

    def test_direct_persistence_cannot_rewind_terminal_evidence(self):
        self.post()
        before = Path(_p(FILE)).read_bytes()
        rows = self.rows(); next(iter(rows.values()))["state"] = "PREPARED"
        self.assertFalse(JsonStore.save(_p(FILE), rows))
        self.assertEqual(Path(_p(FILE)).read_bytes(), before)

    def test_transport_auth_options_are_never_persisted(self):
        before = self.image()
        with self.assertRaises(KalshiAPIError):
            self.client._req("POST", "/portfolio/events/orders", proxies={"https": "synthetic-private-option"})
        self.assertEqual(self.image(), before)
        self.assertEqual(self.client.calls, [])

    def test_malformed_acknowledgement_remains_unknown(self):
        self.client._req = durable_transport(lambda *args, **kwargs: {"value": float("nan")}).__get__(self.client)
        with self.assertRaises(KalshiAPIError): self.post()
        self.assertEqual(self.row()["state"], "UNKNOWN")


class LegacyTransportRecovery(AuthorityCase):
    def setUp(self):
        super().setUp()
        # Exact old-schema fixture established BEFORE the independent test
        # authority observes it; this is not a runtime migration approval.
        from state_authority import durable_replace, remember_file
        import hashlib
        from strict_data import dumps
        body = {"client_order_id": "old", "ticker": "KXBTC15M-old", "side": "bid", "count": "1", "price": "0.2500"}
        payload = {"identity": self.ledger.identity, "operation": "POST", "path": "/portfolio/events/orders", "request": {"json": body}}
        digest = _digest(payload)
        self.legacy = {**payload, "digest": digest, "generation": manifest(_p(FILE))["generation"] + 1, "state": "PREPARED"}
        raw = dumps({digest: self.legacy}, indent=1).encode()
        durable_replace(_p(FILE), raw)
        durable_replace(_p(FILE)+".sha256", hashlib.sha256(raw).hexdigest().encode())
        remember_file(_p(FILE), raw)
        from test_engine_authority import IndependentCheckpoint
        from state_authority import checkpoint
        from equity_ledger import EquityLedger
        self.authority = IndependentCheckpoint(checkpoint(self.ledger.path, self.ledger.identity))
        self.ledger = EquityLedger(self.tlog, self.pos, account_id="account-A", authority=self.authority)
        self.assertFalse(self.ledger.capital_eligible())
        self.client = MemoryBroker(self.authority)
        self.client.orders = [{**body, "order_id": "legacy-order", "initial_count": "1", "remaining_count": "1", "status": "resting"}]
    def test_old_prepared_is_unknown_not_proven_no_send(self):
        self.client.visible = False
        reconcile_transport_intents(self.client)
        row = next(iter(JsonStore.load(_p(FILE), {}).values()))
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(row["legacy_evidence"], self.legacy)
        self.assertEqual(self.client.calls, [])
    def test_old_intent_resolves_from_independent_order_identity(self):
        reconcile_transport_intents(self.client)
        row = next(iter(JsonStore.load(_p(FILE), {}).values()))
        self.assertEqual(row["state"], "CONFIRMED_APPLIED")
        self.assertEqual(row["legacy_evidence"], self.legacy)
        self.assertEqual(self.client.calls, [])
