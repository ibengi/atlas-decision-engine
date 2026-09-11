"""Explicit non-secret identities and independent synthetic authority fixtures.

Only fixture construction lives here. No engine method, guard or test assertion
is patched. Checkpoints survive a simulated disk restore within the test.
"""
import base64
import os
import threading
import time
from config import CFG, _p
from continuity_authority import (account_identity, SignedEvidence, TrustPolicy,
    configure_trust, evidence_bytes, evidence_payload)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from state_authority import checkpoint


class SyntheticEvidenceSigner:
    """Test-owned independent signing authority; never saved in DATA_DIR.

    The test host explicitly pins its public key. Production code never calls
    this constructor or installs a provider-supplied key.
    """
    def __init__(self, identity):
        self.identity = identity
        self.signing_key = Ed25519PrivateKey.generate()
        self.authority_id = "synthetic-independent-authority"
        self.sequence = 0
        public = self.signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        configure_trust(self, TrustPolicy(self.authority_id, public,
            frozenset({identity["fingerprint"]}), frozenset({identity["environment"]})))

    def sign_evidence(self, request, purpose, claims=None, **times):
        payload = evidence_payload(request, purpose, self.authority_id,
                                   self.sequence, claims, **times)
        return SignedEvidence(payload, base64.b64encode(
            self.signing_key.sign(evidence_bytes(payload))).decode())

    def attest_account(self, request, credential_identity=None):
        if request.account_fingerprint != self.identity["fingerprint"]:
            return None
        return self.sign_evidence(request, "account_identity", {
            "stable_account_id": self.identity["account_id"],
            "credential_identity": credential_identity,
            "source": "independent_account_attestation",
            "observation_id": "synthetic-account-observation"})


class CheckpointStore(SyntheticEvidenceSigner):
    def __init__(self, request):
        super().__init__(account_identity(request.broker, request.environment, request.account_id))
        self.current = self.key(request)
        self.lock = threading.Lock()
    @staticmethod
    def key(request):
        return request.account_fingerprint, request.generation, request.digest
    def verify_current(self, request):
        with self.lock:
            return (self.sign_evidence(request, "continuity_current")
                    if self.key(request) == self.current else None)
    def advance(self, previous, candidate):
        with self.lock:
            if self.key(previous) != self.current or candidate.generation <= previous.generation:
                return None
            if candidate.account_fingerprint != previous.account_fingerprint:
                return None
            self.current = self.key(candidate)
            self.sequence += 1
            return self.sign_evidence(candidate, "continuity_advance", {
                "previous_generation": previous.generation,
                "previous_digest": previous.digest})


_providers = {}


def corrupt_json(path, value):
    """Inject restored/corrupt bytes outside the engine API, without advancing
    its authoritative manifest. Used only by filesystem fault scenarios."""
    from pathlib import Path
    from strict_data import dumps
    import hashlib
    raw = dumps(value, indent=1, ensure_ascii=False).encode()
    Path(path).write_bytes(raw)
    Path(path + ".sha256").write_text(hashlib.sha256(raw).hexdigest())


def provider_for(path=None, env="prod"):
    path = path or _p("equity_ledger.json")
    key = (os.getpid(), os.path.realpath(os.path.dirname(path)))
    if key not in _providers:
        identity = account_identity("kalshi", env, CFG.BROKER_ACCOUNT_ID)
        _providers[key] = CheckpointStore(checkpoint(path, identity))
    return _providers[key]


class FrozenSyntheticBroker(SyntheticEvidenceSigner):
    """Signed all-writer exclusion exists only in the isolated broker double."""
    def __init__(self, broker, env="prod", identity=None):
        super().__init__(identity or account_identity("kalshi", env, CFG.BROKER_ACCOUNT_ID))
        self.broker = broker
        self.epoch = getattr(broker, "epoch", None)
    def prove_freeze(self, request, versions):
        if (request.account_fingerprint != self.identity["fingerprint"] or
                self.broker.orders or self.broker.positions or
                getattr(self.broker, "epoch", None) != self.epoch):
            return None
        now = time.time()
        return self.sign_evidence(request, "broker_freeze", {
            "scope": "all_broker_writers", "open_orders": 0, "open_positions": 0,
            "atomic_exposure_snapshot": True, "freeze_id": "synthetic-fence-1",
            "fence_release_policy": "explicit_after_local_commit",
            "fence_state": "HELD", "automatic_expiry": False,
            "broker_watermark": str(self.epoch), "exclusive_until": now + 30},
            issued_at=now, expires_at=now + 30)


def initialize_empty():
    from persistence import JsonStore
    for name, value in (("kalshi_trades.json", []), ("positions_state.json", {}),
                        ("orders_state.json", {}), ("pending_intents.json", {}),
                        ("submission_guard.json", {})):
        if not os.path.exists(_p(name)):
            if not JsonStore.save(_p(name), value):
                raise RuntimeError("synthetic empty state initialization failed")


def freeze_for(ledger):
    from types import SimpleNamespace
    broker = getattr(ledger.posmgr, "client", None)
    if broker is None:
        broker = SimpleNamespace(orders=[], positions=[])
    return FrozenSyntheticBroker(broker, ledger.env)
