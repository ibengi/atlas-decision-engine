"""Independently authenticated, fail-closed continuity and account evidence.

The host pins an authority's public key and scopes OUTSIDE the provider and the
restorable economic state directory. Providers only return signed statements;
they never choose Atlas's verifier or trust roots. No provider is deployed or
implicitly trusted by this module. The selected external authority must keep a
linearizable, monotonic checkpoint independently of the economic volume.
"""
import base64
import hashlib
import math
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from strict_data import dumps, loads

_DOMAIN = b"atlas-independent-evidence-v1\x00"
_PURPOSES = frozenset({"continuity_current", "continuity_advance", "account_identity",
                       "broker_freeze", "transport_outcome"})
_registry = {}
_registry_lock = threading.RLock()


def _after_fork():
    # A child is a new host runtime. It must explicitly acquire its own writer
    # authority and independently provision trust; inherited mutexes may have
    # belonged to vanished parent threads and must not deadlock construction.
    global _registry, _registry_lock
    _registry = {}
    _registry_lock = threading.RLock()


os.register_at_fork(after_in_child=_after_fork)


def account_identity(broker, environment, stable_account_id):
    if (broker != "kalshi" or environment not in ("demo", "prod") or
            not isinstance(stable_account_id, str) or not stable_account_id.strip()):
        return None
    body = {"broker": broker, "environment": environment,
            "account_id": stable_account_id.strip()}
    return {**body, "fingerprint": hashlib.sha256(
        dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


@dataclass(frozen=True)
class Checkpoint:
    account_fingerprint: str
    generation: int
    digest: str
    nonce: str
    broker: str = ""
    environment: str = ""
    account_id: str = ""


@dataclass(frozen=True)
class SignedEvidence:
    """Untrusted wire envelope. Only verify_evidence grants it meaning."""
    payload: dict
    signature: str


@dataclass(frozen=True)
class TrustPolicy:
    """Host-owned pin, not an attribute or return value of the provider.

    Public keys are raw Ed25519 bytes. Re-establish pins from an independently
    controlled configuration after restart; restoring economic files must not
    restore or replace this trust decision. Credential IDs are never account IDs.
    """
    authority_id: str
    public_key: bytes
    account_fingerprints: frozenset
    environments: frozenset
    purposes: frozenset = _PURPOSES
    max_age_seconds: float = 60.0
    future_skew_seconds: float = 5.0


def configure_trust(provider, policy):
    """Explicit host provisioning; never call using provider-supplied keys."""
    if (provider is None or type(policy) is not TrustPolicy or
            not isinstance(policy.authority_id, str) or not policy.authority_id.strip() or
            type(policy.public_key) is not bytes or len(policy.public_key) != 32 or
            type(policy.account_fingerprints) is not frozenset or
            not policy.account_fingerprints or
            not all(_hex64(v) for v in policy.account_fingerprints) or
            type(policy.environments) is not frozenset or not policy.environments or
            not policy.environments.issubset({"demo", "prod"}) or
            type(policy.purposes) is not frozenset or not policy.purposes or
            not policy.purposes.issubset(_PURPOSES) or
            not _number(policy.max_age_seconds) or not 0 < policy.max_age_seconds <= 300 or
            not _number(policy.future_skew_seconds) or not 0 <= policy.future_skew_seconds <= 10):
        raise ValueError("invalid independently configured authority trust")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(policy.public_key)
    with _registry_lock:
        key = (os.getpid(), id(provider))
        prior = _registry.get(key)
        if prior is not None and (prior[0] is not provider or prior[1] != policy):
            raise ValueError("authority trust cannot be silently replaced")
        if prior is None:
            _registry[key] = (provider, policy, {}, {})


def has_trust(provider):
    with _registry_lock:
        row = _registry.get((os.getpid(), id(provider)))
        return bool(row and row[0] is provider)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hex64(value):
    return (type(value) is str and len(value) == 64 and
            all(c in "0123456789abcdef" for c in value))


def challenge(identity, generation, digest):
    if (not isinstance(identity, dict) or identity != account_identity(
            identity.get("broker"), identity.get("environment"), identity.get("account_id")) or
            type(generation) is not int or generation < 0 or not _hex64(digest)):
        raise ValueError("invalid independent evidence challenge")
    return Checkpoint(identity["fingerprint"], generation, digest,
                      secrets.token_hex(32), identity["broker"],
                      identity["environment"], identity["account_id"])


def evidence_bytes(payload):
    """Canonical, domain-separated signature input; also used by test signers."""
    return _DOMAIN + dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def evidence_payload(request, purpose, authority_id, monotonic_checkpoint,
                     claims=None, *, issued_at=None, expires_at=None):
    """Wire-format builder, with no authority or verification side effect."""
    issued_at = time.time() if issued_at is None else issued_at
    expires_at = issued_at + 30 if expires_at is None else expires_at
    return {"version": 1, "purpose": purpose, "broker": request.broker,
            "environment": request.environment, "account_id": request.account_id,
            "account_fingerprint": request.account_fingerprint,
            "generation": request.generation, "digest": request.digest,
            "nonce": request.nonce, "authority_id": authority_id,
            "issued_at": issued_at, "expires_at": expires_at,
            "monotonic_checkpoint": monotonic_checkpoint, "claims": claims or {}}


def verify_evidence(provider, request, response, *, purpose, expected_claims=None):
    """Verify signature, scope, freshness and challenge independently of provider.

    A returned Checkpoint, provider-supplied verifier, self-signed unknown key,
    copied signature, replayed nonce or stale monotonic checkpoint is refused.
    expected_claims must match signed values exactly (extra signed claims may be
    present). Callers still define the domain-specific policy for those claims.
    """
    try:
        if type(request) is not Checkpoint or type(response) is not SignedEvidence:
            return False
        with _registry_lock:
            registered = _registry.get((os.getpid(), id(provider)))
            if registered is None or registered[0] is not provider:
                return False
            _, policy, highwater, consumed = registered
            if (purpose not in policy.purposes or request.account_fingerprint not in
                    policy.account_fingerprints or request.environment not in policy.environments):
                return False
            ident = account_identity(request.broker, request.environment, request.account_id)
            if (ident is None or ident["fingerprint"] != request.account_fingerprint or
                    type(request.generation) is not int or request.generation < 0 or
                    not _hex64(request.digest) or not _hex64(request.nonce)):
                return False
            if type(response.payload) is not dict or type(response.signature) is not str:
                return False
            payload = loads(dumps(response.payload, sort_keys=True))
            if type(payload) is not dict or set(payload) != set(evidence_payload(
                    request, purpose, policy.authority_id, 0)):
                return False
            expected = {"version": 1, "purpose": purpose, "broker": request.broker,
                        "environment": request.environment, "account_id": request.account_id,
                        "account_fingerprint": request.account_fingerprint,
                        "generation": request.generation, "digest": request.digest,
                        "nonce": request.nonce, "authority_id": policy.authority_id}
            if any(type(payload[k]) is not type(v) or payload[k] != v for k, v in expected.items()):
                return False
            if (not _number(payload["issued_at"]) or not _number(payload["expires_at"]) or
                    type(payload["monotonic_checkpoint"]) is not int or
                    payload["monotonic_checkpoint"] < 0 or type(payload["claims"]) is not dict):
                return False
            now = time.time()
            if (payload["issued_at"] > now + policy.future_skew_seconds or
                    payload["issued_at"] < now - policy.max_age_seconds or
                    payload["expires_at"] <= now or
                    not 0 < payload["expires_at"] - payload["issued_at"] <= policy.max_age_seconds):
                return False
            if expected_claims is not None and (type(expected_claims) is not dict or any(
                    k not in payload["claims"] or
                    evidence_bytes({k: payload["claims"][k]}) != evidence_bytes({k: value})
                    for k, value in expected_claims.items())):
                return False
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            signature = base64.b64decode(response.signature, validate=True)
            Ed25519PublicKey.from_public_bytes(policy.public_key).verify(signature, evidence_bytes(payload))
            nonce_key = (purpose, request.nonce)
            for old_nonce, expiry in list(consumed.items()):
                if expiry <= now:
                    del consumed[old_nonce]
            if nonce_key in consumed:
                return False
            seq = payload["monotonic_checkpoint"]
            floor = highwater.get(request.account_fingerprint)
            if floor is not None and seq < floor[0]:
                return False
            if purpose in ("continuity_current", "continuity_advance"):
                point = (seq, request.generation, request.digest)
                if floor is not None and seq == floor[0] and point != floor:
                    return False
                if purpose == "continuity_advance" and floor is not None and seq <= floor[0]:
                    return False
                highwater[request.account_fingerprint] = point
            consumed[nonce_key] = payload["expires_at"]
            return True
    except Exception:
        return False


def verify_current(provider, request):
    try:
        if not has_trust(provider):
            return False
        return verify_evidence(provider, request, provider.verify_current(request),
                               purpose="continuity_current")
    except Exception:
        return False


def advance(provider, previous, candidate):
    try:
        if (not has_trust(provider) or
                candidate.account_fingerprint != previous.account_fingerprint or
                candidate.generation <= previous.generation):
            return False
        return verify_evidence(provider, candidate, provider.advance(previous, candidate),
            purpose="continuity_advance", expected_claims={
                "previous_generation": previous.generation, "previous_digest": previous.digest})
    except Exception:
        return False


def credential_fingerprint(client):
    """Nonsecret current credential reference, never an economic account ID.

    Only the public API-key identifier is read. Its independent attestation may
    rotate while the stable account fingerprint remains unchanged. Absence
    requires account attestation through the explicitly selected authority;
    malformed identifiers are not silently treated as absent.
    """
    key_id = getattr(client, "key_id", None) if client is not None else None
    if key_id is None:
        return None
    if type(key_id) is not str or not key_id.strip():
        raise ValueError("invalid nonsecret broker credential identifier")
    return hashlib.sha256(key_id.encode()).hexdigest()


def account_identity_proven(provider, identity, credential_identity=None):
    """A label alone is insufficient; require independently signed observation.

    Where the broker lacks an authenticated stable-account endpoint, a selected
    independent authority must attest the credential-to-account mapping before
    CAPITAL. Credential rotation changes that mapping proof, not account identity.
    No secret enters the attestation request or persisted economic identity.
    """
    try:
        if not has_trust(provider):
            return False
        if credential_identity is not None and (type(credential_identity) is not str or
                                                not credential_identity.strip()):
            return False
        claims = {"stable_account_id": identity["account_id"],
                  "credential_identity": credential_identity}
        request = challenge(identity, 0, hashlib.sha256(evidence_bytes(claims)).hexdigest())
        response = provider.attest_account(request, credential_identity)
        if type(response) is not SignedEvidence:
            return False
        observed = response.payload.get("claims", {})
        if (observed.get("source") not in ("broker_stable_account_identity",
                                           "independent_account_attestation") or
                type(observed.get("observation_id")) is not str or not observed["observation_id"].strip()):
            return False
        claims.update(source=observed["source"], observation_id=observed["observation_id"])
        return verify_evidence(provider, request, response, purpose="account_identity",
                               expected_claims=claims)
    except Exception:
        return False


def broker_freeze_proven(provider, identity, versions):
    """Require a signed all-writer broker fence and atomic zero-exposure proof.

    A local process lease, repeated read, boolean callback or empty order page
    cannot establish this property. The broker fence itself may not expire during
    an unbounded filesystem commit: only explicit release after the commit is
    admissible. Response expiry is proof freshness, not fence expiry. Real
    adapters currently expose no such proof.
    """
    try:
        if not has_trust(provider):
            return False
        request = challenge(identity, versions["root_generation"],
                            hashlib.sha256(evidence_bytes(versions)).hexdigest())
        response = provider.prove_freeze(request, versions)
        if type(response) is not SignedEvidence:
            return False
        claims = response.payload.get("claims", {})
        if (type(claims.get("freeze_id")) is not str or not claims["freeze_id"] or
                type(claims.get("broker_watermark")) is not str or not claims["broker_watermark"] or
                not _number(claims.get("exclusive_until")) or
                claims["exclusive_until"] < response.payload.get("expires_at", float("inf"))):
            return False
        return verify_evidence(provider, request, response, purpose="broker_freeze",
            expected_claims={"scope": "all_broker_writers", "open_orders": 0,
                             "open_positions": 0, "atomic_exposure_snapshot": True,
                             "fence_release_policy": "explicit_after_local_commit",
                             "fence_state": "HELD", "automatic_expiry": False,
                             "freeze_id": claims["freeze_id"],
                             "broker_watermark": claims["broker_watermark"],
                             "exclusive_until": claims["exclusive_until"]})
    except Exception:
        return False


class ContinuityAuthority(Protocol):
    def verify_current(self, checkpoint: Checkpoint) -> SignedEvidence:
        """Sign exact fresh request only when independent checkpoint is current."""

    def advance(self, previous: Checkpoint, candidate: Checkpoint) -> SignedEvidence:
        """Linearizable external CAS; sign candidate and exact previous checkpoint."""

    def attest_account(self, request: Checkpoint, credential_identity=None) -> SignedEvidence:
        """Independently observed stable account/environment and credential mapping."""
