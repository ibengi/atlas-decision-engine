"""Synthetic independent Ed25519 trust, account mapping and broker-fence tests.

Signers and public-key pins live in the test host, outside restored state. No
network, credential, deployed authority, or broker mutation is used.
"""
import base64
import copy
import hashlib
import os
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from continuity_authority import (SignedEvidence, TrustPolicy, account_identity,
    account_identity_proven, advance, broker_freeze_proven, challenge,
    configure_trust, credential_fingerprint, evidence_bytes, verify_current, verify_evidence)
from authority_fixtures import CheckpointStore, FrozenSyntheticBroker
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class AuthenticatedContinuity(unittest.TestCase):
    def setUp(self):
        self.identity = account_identity("kalshi", "prod", "observed-account-A")
        self.request = challenge(self.identity, 7, "a" * 64)
        self.provider = CheckpointStore(self.request)

    def fresh(self, generation=7, digest="a" * 64):
        return challenge(self.identity, generation, digest)

    def test_valid_independently_pinned_signature(self):
        self.assertTrue(verify_current(self.provider, self.request))

    def test_echo_provider_is_not_evidence(self):
        class Echo:
            verify_current = lambda self, request: request
        self.assertFalse(verify_current(Echo(), self.request))

    def test_echo_still_fails_with_host_pinned_key(self):
        self.provider.verify_current = lambda request: request
        self.assertFalse(verify_current(self.provider, self.request))

    def test_provider_own_key_and_verifier_cannot_establish_trust(self):
        echo = SimpleNamespace(verify_current=lambda request: self.provider.sign_evidence(
            request, "continuity_current"), public_key=self.provider.signing_key.public_key(),
            verify=lambda *a: True, trusted=True)
        self.assertFalse(verify_current(echo, self.request))

    def test_signature_from_different_key_fails(self):
        response = self.provider.verify_current(self.request)
        key = Ed25519PrivateKey.generate()
        forged = replace(response, signature=base64.b64encode(
            key.sign(evidence_bytes(response.payload))).decode())
        self.assertFalse(verify_evidence(self.provider, self.request, forged,
                                        purpose="continuity_current"))

    def test_same_signed_proof_cannot_be_replayed(self):
        response = self.provider.verify_current(self.request)
        self.assertTrue(verify_evidence(self.provider, self.request, response,
                                       purpose="continuity_current"))
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))

    def test_old_nonce_does_not_answer_new_challenge(self):
        response = self.provider.verify_current(self.request)
        self.assertFalse(verify_evidence(self.provider, self.fresh(), response,
                                        purpose="continuity_current"))

    def test_expired_signed_proof_fails(self):
        now = time.time()
        response = self.provider.sign_evidence(self.request, "continuity_current",
                                               issued_at=now-61, expires_at=now-31)
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))

    def test_future_signed_proof_fails(self):
        now = time.time()
        response = self.provider.sign_evidence(self.request, "continuity_current",
                                               issued_at=now+30, expires_at=now+60)
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))

    def test_overlong_validity_fails(self):
        now = time.time()
        response = self.provider.sign_evidence(self.request, "continuity_current",
                                               issued_at=now, expires_at=now+600)
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))

    def test_monotonic_checkpoint_cannot_move_backwards(self):
        self.provider.sequence = 4
        self.assertTrue(verify_current(self.provider, self.request))
        self.provider.sequence = 3
        self.assertFalse(verify_current(self.provider, self.fresh()))

    def test_same_checkpoint_number_cannot_change_digest(self):
        self.assertTrue(verify_current(self.provider, self.request))
        newer = self.fresh(8, "b"*64)
        self.provider.current = self.provider.key(newer)
        self.assertFalse(verify_current(self.provider, newer))

    def test_signed_compare_and_swap_advances(self):
        self.assertTrue(verify_current(self.provider, self.request))
        newer = self.fresh(8, "b"*64)
        self.assertTrue(advance(self.provider, self.request, newer))
        self.assertTrue(verify_current(self.provider, self.fresh(8, "b"*64)))
        self.assertFalse(verify_current(self.provider, self.fresh()))

    def test_advance_without_previous_binding_fails(self):
        self.assertTrue(verify_current(self.provider, self.request))
        self.provider.advance = lambda before, after: self.provider.sign_evidence(
            after, "continuity_advance")
        self.assertFalse(advance(self.provider, self.request, self.fresh(8, "b"*64)))

    def test_account_label_without_independent_attestation_fails(self):
        self.provider.attest_account = lambda request, credential_identity=None: request
        self.assertFalse(account_identity_proven(self.provider, self.identity))

    def test_independently_attested_stable_account_passes(self):
        self.assertTrue(account_identity_proven(self.provider, self.identity))

    def test_credential_rotation_preserves_economic_account(self):
        before = copy.deepcopy(self.identity)
        self.assertTrue(account_identity_proven(self.provider, self.identity, "public-key-id-old"))
        self.assertTrue(account_identity_proven(self.provider, self.identity, "public-key-id-new"))
        self.assertEqual(before, self.identity)
        self.assertNotIn("credential_identity", self.identity)

    def test_attestation_for_another_credential_fails(self):
        original = self.provider.attest_account
        self.provider.attest_account = lambda request, credential_identity=None: original(
            request, "different-credential")
        self.assertFalse(account_identity_proven(self.provider, self.identity, "active-credential"))

    def test_state_copied_to_different_account_fails(self):
        other = account_identity("kalshi", "prod", "observed-account-B")
        self.assertFalse(account_identity_proven(self.provider, other))

    def test_state_copied_to_different_environment_fails(self):
        other = account_identity("kalshi", "demo", "observed-account-A")
        self.assertFalse(account_identity_proven(self.provider, other))

    def test_operator_label_source_even_signed_is_insufficient(self):
        self.provider.attest_account = lambda request, credential_identity=None: self.provider.sign_evidence(
            request, "account_identity", {"stable_account_id": self.identity["account_id"],
                "credential_identity": credential_identity, "source": "operator_label",
                "observation_id": "label"})
        self.assertFalse(account_identity_proven(self.provider, self.identity))

    def test_boolean_local_freeze_is_not_broker_proof(self):
        fake = SimpleNamespace(verify=lambda *args: True)
        self.assertFalse(broker_freeze_proven(fake, self.identity, {"root_generation": 7}))

    def test_signed_all_writer_freeze_passes(self):
        broker = SimpleNamespace(orders=[], positions=[], epoch=3)
        freeze = FrozenSyntheticBroker(broker, identity=self.identity)
        self.assertTrue(broker_freeze_proven(freeze, self.identity, {"root_generation": 7}))

    def test_broker_epoch_change_revokes_freeze(self):
        broker = SimpleNamespace(orders=[], positions=[], epoch=3)
        freeze = FrozenSyntheticBroker(broker, identity=self.identity)
        broker.epoch += 1
        self.assertFalse(broker_freeze_proven(freeze, self.identity, {"root_generation": 7}))

    def test_local_writer_only_scope_cannot_authorize_rebase(self):
        broker = SimpleNamespace(orders=[], positions=[], epoch=3)
        freeze = FrozenSyntheticBroker(broker, identity=self.identity)
        original = freeze.prove_freeze
        def limited(request, versions):
            payload = original(request, versions).payload
            claims = dict(payload["claims"], scope="local_atlas_writer")
            return freeze.sign_evidence(request, "broker_freeze", claims)
        freeze.prove_freeze = limited
        self.assertFalse(broker_freeze_proven(freeze, self.identity, {"root_generation": 7}))

    def test_freeze_with_open_broker_exposure_fails(self):
        broker = SimpleNamespace(orders=[{"order_id": "existing"}], positions=[], epoch=3)
        freeze = FrozenSyntheticBroker(broker, identity=self.identity)
        self.assertFalse(broker_freeze_proven(freeze, self.identity, {"root_generation": 7}))

    def test_automatically_expiring_fence_cannot_cover_local_commit(self):
        broker = SimpleNamespace(orders=[], positions=[], epoch=3)
        freeze = FrozenSyntheticBroker(broker, identity=self.identity)
        original = freeze.prove_freeze
        def expiring(request, versions):
            claims = dict(original(request, versions).payload["claims"], automatic_expiry=True)
            return freeze.sign_evidence(request, "broker_freeze", claims)
        freeze.prove_freeze = expiring
        self.assertFalse(broker_freeze_proven(freeze, self.identity, {"root_generation": 7}))

    def test_account_attestation_without_observation_reference_fails(self):
        self.provider.attest_account = lambda request, credential_identity=None: self.provider.sign_evidence(
            request, "account_identity", {"stable_account_id": self.identity["account_id"],
                "credential_identity": credential_identity, "source": "independent_account_attestation",
                "observation_id": ""})
        self.assertFalse(account_identity_proven(self.provider, self.identity))

    def test_account_proof_cannot_be_used_as_checkpoint(self):
        response = self.provider.sign_evidence(self.request, "account_identity")
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))

    def test_fork_drops_inherited_trust_and_resets_held_registry_lock(self):
        import continuity_authority as authority
        held = threading.Event()
        release = threading.Event()
        def hold_lock():
            with authority._registry_lock:
                held.set()
                release.wait(5)
        worker = threading.Thread(target=hold_lock)
        worker.start()
        self.assertTrue(held.wait(2))
        pid = os.fork()
        if pid == 0:
            import signal
            signal.alarm(3)
            os._exit(0 if not authority.has_trust(self.provider) else 2)
        try:
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
        finally:
            release.set()
            worker.join(5)
        self.assertTrue(verify_current(self.provider, self.request))

    def test_trust_pin_cannot_be_silently_replaced(self):
        public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        with self.assertRaises(ValueError):
            configure_trust(self.provider, TrustPolicy("replacement-authority", public,
                frozenset({self.identity["fingerprint"]}), frozenset({"prod"})))
        self.assertTrue(verify_current(self.provider, self.request))

    def test_untrusted_transport_outcome_signature_fails(self):
        response = self.provider.sign_evidence(self.request, "transport_outcome", {"outcome": "absent"})
        self.assertFalse(verify_evidence(object(), self.request, response,
                                        purpose="transport_outcome", expected_claims={"outcome": "absent"}))

    def test_transport_outcome_is_bound_to_exact_claims(self):
        response = self.provider.sign_evidence(self.request, "transport_outcome", {"client_order_id": "one"})
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="transport_outcome", expected_claims={"client_order_id": "two"}))


from test_engine_authority import AuthorityCase


class LedgerAccountBinding(AuthorityCase):
    """Exercise real ledger eligibility, not only the response verifier."""
    def bind_observed_credentials(self):
        self.broker.key_id = "public-key-account-A-original"
        self.prove()
        allowed = {hashlib.sha256(v.encode()).hexdigest() for v in (
            "public-key-account-A-original", "public-key-account-A-rotated")}
        original = self.authority.attest_account
        self.observed_credentials = []
        def independently_observed(request, credential_identity=None):
            self.observed_credentials.append(credential_identity)
            if credential_identity not in allowed:
                return None
            return original(request, credential_identity)
        self.authority.attest_account = independently_observed

    def test_ledger_rotation_retains_same_economic_account(self):
        self.bind_observed_credentials()
        identity = copy.deepcopy(self.ledger.identity)
        economic_bytes = self.image()
        self.assertTrue(self.ledger.capital_eligible(), self.ledger.guards())
        self.broker.key_id = "public-key-account-A-rotated"
        self.assertTrue(self.ledger.capital_eligible(), self.ledger.guards())
        self.assertEqual(identity, self.ledger.identity)
        self.assertEqual(economic_bytes, self.image())
        self.assertEqual(set(self.observed_credentials), {
            hashlib.sha256(v.encode()).hexdigest() for v in (
                "public-key-account-A-original", "public-key-account-A-rotated")})

    def test_ledger_foreign_account_credential_cannot_report_eligible(self):
        self.bind_observed_credentials()
        self.broker.key_id = "public-key-account-B"
        self.assertFalse(self.ledger.capital_eligible())
        self.assertIn("external_continuity_unproven", self.ledger.guards())

    def test_ledger_runtime_environment_drift_cannot_report_eligible(self):
        self.bind_observed_credentials()
        self.broker.env = "demo"
        self.assertFalse(self.ledger.capital_eligible())
        self.assertIn("external_continuity_unproven", self.ledger.guards())

    def test_ledger_proof_never_persists_credential_or_secret(self):
        self.bind_observed_credentials()
        self.broker.private_key = "synthetic-private-key-must-not-be-read-or-stored"
        before = self.image()
        self.assertTrue(self.ledger.capital_eligible())
        after = self.image()
        self.assertEqual(before, after)
        combined = b"".join(after.values())
        self.assertNotIn(self.broker.key_id.encode(), combined)
        self.assertNotIn(self.broker.private_key.encode(), combined)
        self.assertNotIn(credential_fingerprint(self.broker).encode(), combined)

    def test_ledger_malformed_credential_identifier_blocks(self):
        self.bind_observed_credentials()
        self.broker.key_id = {"not": "an identifier"}
        self.assertFalse(self.ledger.capital_eligible())

    def test_credential_fingerprint_never_reads_private_key(self):
        class PublicIdentifierOnly:
            key_id = "public-identifier"
            @property
            def private_key(self):
                raise AssertionError("secret must not be read")
        self.assertEqual(credential_fingerprint(PublicIdentifierOnly()),
                         hashlib.sha256(b"public-identifier").hexdigest())


def _tamper_case(field, value):
    def test(self):
        response = self.provider.verify_current(self.request)
        response.payload[field] = value
        self.assertFalse(verify_evidence(self.provider, self.request, response,
                                        purpose="continuity_current"))
    return test


for _field, _value in {"account_id": "B", "account_fingerprint": "b"*64,
        "environment": "demo", "generation": 8, "digest": "b"*64,
        "nonce": "b"*64, "authority_id": "other-authority", "issued_at": 1,
        "monotonic_checkpoint": 99}.items():
    setattr(AuthenticatedContinuity, "test_tampered_" + _field + "_fails",
            _tamper_case(_field, _value))


def _callback_drift_case(stage, drift):
    def test(self):
        from config import CFG, _p
        from persistence import JsonStore
        from state_authority import WriterLease
        self.bind_observed_credentials()
        lease = WriterLease(self.ledger.path)
        self.addCleanup(lease.close)
        self.broker._engine_writer_lease = lease
        original = getattr(self.authority, stage)
        changed = False

        def callback(*args, **kwargs):
            nonlocal changed
            response = original(*args, **kwargs)
            if changed:
                return response
            changed = True
            if drift == "client_environment":
                self.broker.env = "demo"
            elif drift == "client_credential":
                self.broker.key_id = "public-key-account-B"
            elif drift == "configured_account":
                CFG.BROKER_ACCOUNT_ID = "account-B"
            elif drift == "configured_root":
                CFG.DATA_DIR = os.path.join(self.tmp.name, "different-root")
            elif drift == "ledger_identity":
                self.ledger.identity = account_identity("kalshi", "prod", "account-B")
            elif drift == "persisted_identity":
                self.ledger.state["identity"] = account_identity("kalshi", "prod", "account-B")
            elif drift == "authority":
                self.ledger.authority = SimpleNamespace(verify_current=lambda request: request)
            elif drift == "client_replacement":
                self.pos.client = SimpleNamespace(env="prod", key_id=self.broker.key_id)
            elif drift == "manifest":
                self.assertTrue(JsonStore.save(_p("submission_guard.json"), {"callback_changed": True}))
            elif drift == "writer_lease":
                lease.close()
            return response

        setattr(self.authority, stage, callback)
        self.assertFalse(self.ledger.capital_eligible(),
                         "a signed answer must not authorize state changed by its callback")
        self.assertTrue(changed)
    return test


for _stage in ("attest_account", "verify_current"):
    for _drift in ("client_environment", "client_credential", "configured_account",
                   "configured_root", "ledger_identity", "persisted_identity",
                   "authority", "client_replacement", "manifest", "writer_lease"):
        setattr(LedgerAccountBinding, "test_" + _stage + "_callback_fences_" + _drift,
                _callback_drift_case(_stage, _drift))


if __name__ == "__main__":
    unittest.main()
