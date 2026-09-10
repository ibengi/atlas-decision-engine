"""Explicit completion of an independently committed local snapshot.

This cannot reconstruct missing history or approve a local rollback. A caller
must supply the exact reviewed manifest digest and an independent authority
that already recognizes that snapshot. No broker or network is used here.
"""
import hashlib
import os

from continuity_authority import account_identity
from persistence import PersistenceSentinel
from state_authority import (AuthorityError, root_lock, root_of, manifest,
    checkpoint, configure_authority, begin_write, durable_replace, remember_file,
    finish_write)
from strict_data import loads, dumps


def _verify_files(path, identity):
    root = root_of(path)
    authority = manifest(path)
    required = {"equity_ledger.json", "kalshi_trades.json", "positions_state.json",
                "orders_state.json", "pending_intents.json", "submission_guard.json",
                "equity_continuity.log"}
    if not required.issubset(authority["files"]):
        raise AuthorityError("recovery snapshot lacks required evidence")
    for name, digest in authority["files"].items():
        if os.path.basename(name) != name:
            raise AuthorityError("invalid recovery path")
        target = os.path.join(root, name)
        with open(target, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AuthorityError("recovery requires the exact committed files")
        if name.endswith(".json"):
            value = loads(data)
            with open(target + ".sha256") as fh:
                if fh.read().strip() != digest:
                    raise AuthorityError("incomplete recovery checksum")
            if name == "equity_ledger.json":
                from equity_ledger import EquityLedger
                if value.get("identity") != identity or EquityLedger._schema_problem(None, value):
                    raise AuthorityError("recovery identity/schema mismatch")
    from continuity import ContinuityChain
    ContinuityChain(os.path.join(root, "equity_continuity.log")).records()
    return authority


def complete_verified_recovery(path, identity, provider, expected_digest, action_id):
    """Finish only a current independent snapshot; preserve all economic bytes.

    The receipt and external CAS precede marker removal. Failure leaves the
    root blocked. Managers MUST be reloaded after success; no cache is merged.
    """
    if (not isinstance(identity, dict) or identity != account_identity(
            identity.get("broker"), identity.get("environment"), identity.get("account_id"))
            or provider is None or not isinstance(action_id, str) or not action_id.strip()):
        raise AuthorityError("explicit recovery identity, authority and action required")
    with root_lock(path):
        _verify_files(path, identity)
        previous = checkpoint(path, identity)
        if previous.digest != expected_digest or provider.verify_current(previous) != previous:
            raise AuthorityError("independent continuity does not authorize this recovery")
        _verify_files(path, identity)
        configure_authority(path, identity, provider)
        receipt = os.path.join(root_of(path), "recovery_receipts.json")
        records = []
        if os.path.exists(receipt):
            with open(receipt, "rb") as fh:
                records = loads(fh.read())
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise AuthorityError("invalid recovery receipt history")
        if any(r.get("action_id") == action_id for r in records):
            raise AuthorityError("recovery action already consumed")
        try:
            begin_write(path)
            records.append({"action_id": action_id, "identity": identity,
                            "prior_generation": previous.generation,
                            "reviewed_digest": expected_digest})
            payload = dumps(records, sort_keys=True).encode()
            durable_replace(receipt, payload)
            durable_replace(receipt + ".sha256", hashlib.sha256(payload).hexdigest().encode())
            remember_file(receipt, payload)
            candidate = checkpoint(path, identity)
            if provider.advance(previous, candidate) != candidate:
                raise AuthorityError("recovery checkpoint outcome uncertain")
            _verify_files(path, identity)
            finish_write(path)
        except Exception as exc:
            PersistenceSentinel.record_failure(path, str(exc))
            raise
        # This is the sole production acknowledgement. It follows exact byte
        # verification and independent commit, never an operator boolean alone.
        failure = PersistenceSentinel.failure()
        if failure is None or root_of(failure["path"]) == root_of(path):
            PersistenceSentinel._failure = None
        return candidate
