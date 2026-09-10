"""Last transport boundary: immutable durable mutation evidence, no retries.

No network I/O is performed here except through the supplied transport method.
Ambiguous writes stay pending; reconciliation must resolve them by broker ID.
"""
import copy
import functools
import hashlib
from config import CFG, _p
from continuity_authority import account_identity, challenge
from persistence import JsonStore, PersistenceSentinel
from state_authority import root_lock, manifest, recovery_problem, active_transaction, configure_authority
from strict_data import dumps


def durable_transport(method):
    @functools.wraps(method)
    def request(self, verb, path, *, retries=3, **kwargs):
        from kalshi_client import _is_mutating_method, KalshiAPIError
        if not _is_mutating_method(verb):
            return method(self, verb, path, retries=retries, **kwargs)
        # Read-only refusal precedes even creation of local intent files.
        self._assert_broker_write_allowed(str(verb) + " " + str(path))
        if set(kwargs) & {"headers", "auth", "cookies", "cert"}:
            raise KalshiAPIError(0, "authentication material cannot enter economic intent storage")
        target = _p("transport_intents.json")
        with root_lock(target):
            issue = recovery_problem(target)
            if issue or active_transaction(target) or not PersistenceSentinel.healthy():
                raise KalshiAPIError(0, issue or "authoritative state unavailable")
            identity = account_identity("kalshi", self.env, getattr(CFG, "BROKER_ACCOUNT_ID", None))
            authority = getattr(self, "continuity_authority", None)
            if identity is None or authority is None:
                raise KalshiAPIError(0, "account identity/external continuity unproven")
            configure_authority(target, identity, authority)
            m = manifest(target)
            prior = challenge(identity, m["generation"], hashlib.sha256(
                dumps(m, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            try:
                if authority.verify_current(prior) != prior:
                    raise ValueError("stale checkpoint")
            except Exception as exc:
                raise KalshiAPIError(0, "external continuity unproven") from exc
            body = copy.deepcopy(kwargs)
            payload = {"identity": identity, "operation": str(verb).upper(),
                       "path": path, "request": body}
            digest = hashlib.sha256(dumps(payload, sort_keys=True).encode()).hexdigest()
            rows = JsonStore.load(target, {})
            if not isinstance(rows, dict) or rows:
                raise KalshiAPIError(0, "mutation unresolved or replayed")
            row = {**payload, "digest": digest, "generation": m["generation"] + 1,
                   "state": "PREPARED"}
            rows[digest] = row
            if not JsonStore.save(target, rows) or JsonStore.load(target, None) != rows:
                raise KalshiAPIError(0, "complete transport intent not durable")
            after = manifest(target)
            candidate = challenge(identity, after["generation"], hashlib.sha256(
                dumps(after, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            try:
                if authority.verify_current(candidate) != candidate:
                    raise ValueError("checkpoint not committed")
            except Exception as exc:
                PersistenceSentinel.record_failure(target, "ambiguous external checkpoint")
                raise KalshiAPIError(0, "external checkpoint commit uncertain") from exc
            if recovery_problem(target) or JsonStore.load(target, None) != rows:
                raise KalshiAPIError(0, "transport intent changed before handoff")
            # Mutable caller dictionaries cannot replace the confirmed body.
            # An HTTP mutation has no generic safe retry, including DELETE.
            return method(self, verb, path, retries=0, **copy.deepcopy(body))
    return request
