"""External continuity contract; no network implementation or auto-approval.

A provider must perform a linearizable compare/checkpoint against an independent
authority scoped to account fingerprint, and authenticate fresh nonce replies.
It MUST reject a generation/digest older than its checkpoint. A local file, an
operator token, or a funding-record hash alone does not implement this contract.
"""
import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol

from strict_data import dumps


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


class ContinuityAuthority(Protocol):
    def verify_current(self, checkpoint: Checkpoint) -> Checkpoint:
        """Return the authenticated exact challenge only if it is current."""

    def advance(self, previous: Checkpoint, candidate: Checkpoint) -> Checkpoint:
        """Linearizable CAS, before local publication. Ambiguity blocks recovery."""


def challenge(identity, generation, digest):
    return Checkpoint(identity["fingerprint"], generation, digest,
                      secrets.token_hex(32))
