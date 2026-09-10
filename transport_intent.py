"""Durable transport lifecycle; no implicit retries or inferred broker absence.

Terminal rows remain audit evidence. Presence is read independently by immutable
order identity; absence needs independently verified final evidence. The normal
adapter supplies no atomic absence guarantee, so empty/404 stays UNKNOWN.
"""
import copy
import functools
import hashlib
import os
import time
import uuid
from decimal import Decimal, InvalidOperation

from config import CFG, _p
from continuity_authority import account_identity, challenge, credential_fingerprint
from persistence import JsonStore, PersistenceSentinel, file_fingerprint
from state_authority import (root_lock, manifest, recovery_problem,
                             active_transaction, configure_authority, root_of)
from strict_data import dumps, loads, finite_number, validate_tree

FILE = "transport_intents.json"
SCHEMA = 2
TERMINAL = frozenset({"CONFIRMED_APPLIED", "CONFIRMED_NOT_APPLIED", "TERMINAL_FAILED"})
TRANSITIONS = {
    "PREPARED": frozenset({"SENT", "CONFIRMED_NOT_APPLIED", "TERMINAL_FAILED"}),
    "SENT": frozenset({"ACKNOWLEDGED", "UNKNOWN", "CONFIRMED_NOT_APPLIED"}),
    "ACKNOWLEDGED": frozenset({"RECONCILING", "UNKNOWN"}),
    "UNKNOWN": frozenset({"RECONCILING"}),
    "RECONCILING": frozenset({"UNKNOWN", *TERMINAL}),
    **{state: frozenset() for state in TERMINAL},
}


class BeforeSendFailure(Exception):
    """Raised only by the adapter before invoking its network dispatch method."""


def _digest(value):
    return hashlib.sha256(dumps(value, sort_keys=True).encode()).hexdigest()


def _payload(row):
    return {key: row[key] for key in ("identity", "operation", "path", "request")}


def _validate(rows):
    validate_tree(rows)
    if not isinstance(rows, dict):
        raise ValueError("malformed transport intent collection")
    for key, row in rows.items():
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            raise ValueError("legacy/unknown transport intent requires explicit recovery")
        if (not isinstance(key, str) or not key or row.get("intent_id") != key
                or row.get("state") not in TRANSITIONS
                or row.get("digest") != _digest(_payload(row))
                or not isinstance(row.get("identity"), dict)
                or row.get("operation") not in ("POST", "DELETE", "PUT", "PATCH")
                or not isinstance(row.get("path"), str) or not row["path"].startswith("/")
                or not isinstance(row.get("request"), dict)
                or type(row.get("generation")) is not int or row["generation"] < 1):
            raise ValueError("incomplete or changed transport identity/payload")
        identity = row["identity"]
        if identity != account_identity(identity.get("broker"), identity.get("environment"), identity.get("account_id")):
            raise ValueError("malformed transport account binding")
        finite_number(row["created_at"], "intent time", minimum=0)
        history = row.get("history")
        if not isinstance(history, list) or not history:
            raise ValueError("transport transition history missing")
        previous = None
        for index, event in enumerate(history):
            if not isinstance(event, dict) or event.get("from") != previous:
                raise ValueError("invalid transport transition chain")
            state = event.get("to")
            if ((index == 0 and state != "PREPARED") or
                    (index > 0 and state not in TRANSITIONS.get(previous, ()))):
                raise ValueError("illegal transport transition")
            finite_number(event["at"], "transition time", minimum=0)
            previous = state
        if previous != row["state"]:
            raise ValueError("transport state disagrees with history")
    return rows


def has_unresolved_transport(path=None):
    """Read-only guard: terminal history is not an unresolved intention."""
    path = path or _p(FILE)
    try:
        if recovery_problem(path):
            return True
        if not os.path.exists(path):
            return False
        with open(path, "rb") as stream:
            rows = _validate(loads(stream.read().decode()))
        return any(row["state"] not in TERMINAL for row in rows.values())
    except (OSError, ValueError, TypeError, KeyError):
        return True


def _load(path):
    rows = _validate(JsonStore.load(path, {}))
    if not PersistenceSentinel.healthy() or recovery_problem(path):
        raise ValueError("transport state is not current authority")
    return rows


def _legacy_valid(key, row):
    try:
        return (isinstance(row, dict) and row.get("schema", 1) == 1
                and row.get("state") == "PREPARED" and row.get("digest") == key
                and row["digest"] == _digest(_payload(row))
                and type(row.get("generation")) is int and row["generation"] > 0
                and row["identity"] == account_identity(row["identity"]["broker"],
                    row["identity"]["environment"], row["identity"]["account_id"]))
    except (KeyError, ValueError, TypeError):
        return False


def validate_collection_update(expected, rows):
    """Persistence-level append-only contract, including lossless v1 migration."""
    _validate(rows)
    if not isinstance(expected, dict):
        raise ValueError("unreadable previous transport collection")
    for key, old in expected.items():
        if key not in rows:
            raise ValueError("transport evidence cannot be deleted")
        new = rows[key]
        if old.get("schema") == SCHEMA:
            if old["state"] in TERMINAL and new != old:
                raise ValueError("terminal transport evidence is immutable")
            for field, value in old.items():
                if field not in ("state", "history") and new.get(field) != value:
                    raise ValueError("immutable transport field changed")
            if (new["history"][:len(old["history"])] != old["history"] or
                    len(new["history"]) not in (len(old["history"]), len(old["history"]) + 1)):
                raise ValueError("transport history must advance exactly one transition")
        else:
            if (not _legacy_valid(key, old) or new.get("legacy_evidence") != old
                    or new["state"] != "UNKNOWN" or _payload(new) != _payload(old)
                    or [e["to"] for e in new["history"]] != ["PREPARED", "SENT", "UNKNOWN"]):
                raise ValueError("legacy transport migration must preserve unknown dispatch evidence")
    for key in rows.keys() - expected.keys():
        if rows[key]["state"] != "PREPARED" or len(rows[key]["history"]) != 1:
            raise ValueError("new transport evidence must begin PREPARED")


def _save(path, rows, expected):
    validate_collection_update(expected, rows)
    if JsonStore.load(path, {}) != expected:
        PersistenceSentinel.record_failure(path, "stale transport transition")
        raise ValueError("stale transport transition")
    fingerprint = file_fingerprint(path)
    if not JsonStore.save(path, rows, expect_fingerprint=fingerprint) or JsonStore.load(path, None) != rows:
        PersistenceSentinel.record_failure(path, "transport transition not durably readable")
        raise ValueError("transport transition not durably readable")


def _move(path, rows, key, state, reason, evidence=None):
    row = rows[key]
    if state not in TRANSITIONS[row["state"]]:
        raise ValueError("illegal transport transition: " + row["state"] + " -> " + state)
    draft = copy.deepcopy(rows)
    event = {"from": row["state"], "to": state, "at": time.time(), "reason": reason}
    if evidence is not None:
        event["evidence"] = copy.deepcopy(evidence)
    draft[key]["history"].append(event)
    draft[key]["state"] = state
    if state == "SENT":
        draft[key]["sent_at"] = event["at"]
    _save(path, draft, expected=rows)
    return draft


def _identity_and_authority(client, path):
    from continuity_authority import verify_current, account_identity_proven
    lease = getattr(client, "_engine_writer_lease", None)
    if lease is not None and (not lease.valid() or lease.root != root_of(path)):
        raise ValueError("engine writer lease is not owned by this process")
    environment = client.env
    account_id = getattr(CFG, "BROKER_ACCOUNT_ID", None)
    identity = account_identity("kalshi", environment, account_id)
    authority = getattr(client, "continuity_authority", None)
    credential = credential_fingerprint(client)
    if (identity is None or authority is None or
            not account_identity_proven(authority, identity, credential_identity=credential)):
        raise ValueError("independent broker/account identity unproven")
    configure_authority(path, identity, authority)
    current = manifest(path)
    request = challenge(identity, current["generation"], _digest_manifest(current))
    if not verify_current(authority, request):
        raise ValueError("authenticated current continuity unproven")
    # Proof acquisition invokes external callbacks. Their successful response
    # cannot confer authority on a different lease, account or credential that
    # replaced the one independently bound by the challenge. In particular,
    # close() during a callback must revoke permission before network handoff.
    if (getattr(client, "_engine_writer_lease", None) is not lease or
            (lease is not None and (not lease.valid() or lease.root != root_of(path))) or
            root_of(_p(FILE)) != root_of(path) or client.env != environment or
            getattr(CFG, "BROKER_ACCOUNT_ID", None) != account_id or
            credential_fingerprint(client) != credential or
            getattr(client, "continuity_authority", None) is not authority):
        raise ValueError("transport authority binding changed during proof verification")
    return identity, authority


def _digest_manifest(current):
    return hashlib.sha256(dumps(current, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid broker quantity/price")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid broker quantity/price") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("invalid broker quantity/price")
    return result


def _unique_number(order, fields):
    values = [_number(order[name]) for name in fields if name in order]
    if not values or any(x != values[0] for x in values):
        raise ValueError("missing/conflicting broker immutable quantity or price")
    return values[0]


def _order_matches(row, order):
    """Exact immutable create semantics; never infer original count from remaining."""
    try:
        body = row["request"].get("json")
        if not isinstance(order, dict) or not isinstance(body, dict):
            return False
        if not isinstance(order.get("order_id"), str) or not order["order_id"]:
            return False
        if (order.get("client_order_id") != body.get("client_order_id")
                or not body.get("client_order_id") or order.get("ticker") != body.get("ticker")):
            return False
        wanted_side = body.get("side")
        side = order.get("side")
        if side in ("yes", "no"):
            if order.get("action") != "buy":
                return False
            side = "bid" if side == "yes" else "ask"
        if side != wanted_side or wanted_side not in ("bid", "ask"):
            return False
        if order.get("side") in ("bid", "ask") and "action" in order:
            if order["action"] != ("buy" if wanted_side == "bid" else "sell"):
                return False
        if _unique_number(order, ("initial_count", "initial_count_fp", "count", "count_fp")) != _number(body["count"]):
            return False
        prices = []
        if "price" in order:
            prices.append(_number(order["price"]))
        for field, scale, complement in (("yes_price", 100, False), ("yes_price_dollars", 1, False),
                                         ("no_price", 100, True), ("no_price_dollars", 1, True)):
            if field in order:
                value = _number(order[field]) / scale
                prices.append(1 - value if complement else value)
        if not prices or any(price != _number(body["price"]) for price in prices):
            return False
        # Supplied immutable execution constraints cannot contradict the request.
        for field in ("time_in_force", "self_trade_prevention_type"):
            if field in order and order[field] != body.get(field):
                return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _independent_presence(client, row):
    operation, path = row["operation"], row["path"]
    if operation == "POST" and path == "/portfolio/events/orders":
        body = row["request"].get("json", {})
        cid, ticker = body.get("client_order_id"), body.get("ticker")
        lookup = getattr(client, "find_orders_by_client_order_id", None)
        if not cid or not ticker or not callable(lookup):
            return None
        orders = lookup(cid, ticker=ticker)
        if not isinstance(orders, list) or len(orders) != 1 or not _order_matches(row, orders[0]):
            return None
        return {"source": "independent_order_read", "order": copy.deepcopy(orders[0])}
    if operation == "DELETE" and path.startswith("/portfolio/events/orders/"):
        oid = path.rsplit("/", 1)[-1]
        get_order = getattr(client, "get_order", None)
        if not oid or not callable(get_order):
            return None
        order = get_order(oid)
        if (isinstance(order, dict) and order.get("order_id") == oid
                and str(order.get("status", "")).lower() in ("canceled", "cancelled")
                and _unique_number(order, ("remaining_count", "remaining_count_fp")) == 0):
            return {"source": "independent_cancel_read", "order": copy.deepcopy(order)}
    return None


def _signed_outcome(client, path, row):
    """Optional independently pinned final broker-history/rejection authority.

    There is no such production provider in this repository. Empty listings do
    not enter this path. Claims promise no later acceptance of this same intent.
    """
    from continuity_authority import verify_evidence
    provider = getattr(client, "transport_evidence_provider", None)
    observe = getattr(provider, "observe", None)
    if not callable(observe):
        return None
    current = manifest(path)
    request = challenge(row["identity"], current["generation"], row["digest"])
    response = copy.deepcopy(observe(request, copy.deepcopy(row)))
    claims = getattr(response, "claims", None)
    if claims is None and hasattr(response, "payload"):
        claims = response.payload.get("claims")
    if not isinstance(claims, dict):
        return None
    outcome = claims.get("outcome")
    if outcome not in TERMINAL or claims.get("final") is not True:
        return None
    expected = {"intent_id": row["intent_id"], "request_digest": row["digest"],
                "operation": row["operation"], "outcome": outcome, "final": True}
    if outcome in ("CONFIRMED_NOT_APPLIED", "TERMINAL_FAILED"):
        expected.update(no_future_acceptance=True, complete_history=True,
                        observed_through=claims.get("observed_through"))
        if (claims.get("no_future_acceptance") is not True or claims.get("complete_history") is not True
                or _number(claims.get("observed_through")) < _number(row.get("sent_at", row["created_at"]))):
            return None
    if not verify_evidence(provider, request, response, purpose="transport_outcome", expected_claims=expected):
        return None
    return outcome, {"source": "authenticated_final_outcome", "claims": copy.deepcopy(claims),
                     "signed_payload": copy.deepcopy(response.payload), "signature": response.signature,
                     "proof_digest": _digest(response.payload)}


def _reconcile_one(client, path, rows, key):
    row = rows[key]
    if row["state"] in TERMINAL:
        return rows
    if row["state"] == "PREPARED":
        # Only schema-2 PREPARED proves dispatch never began: SENT is durable first.
        return _move(path, rows, key, "CONFIRMED_NOT_APPLIED", "restart_before_dispatch")
    if row["state"] in ("SENT", "RECONCILING"):
        rows = _move(path, rows, key, "UNKNOWN", "restart_or_interrupted_handoff")
    rows = _move(path, rows, key, "RECONCILING", "independent_read_started")
    row = rows[key]
    bound = manifest(path)
    state, reason, evidence = "UNKNOWN", "broker_outcome_not_proven", None
    try:
        evidence = _independent_presence(client, row)
        if evidence:
            state, reason = "CONFIRMED_APPLIED", "exact_broker_presence"
        else:
            outcome = _signed_outcome(client, path, row)
            if outcome:
                state, evidence = outcome
                reason = "authenticated_broker_outcome"
    except Exception as exc:
        reason = "evidence_unavailable:" + type(exc).__name__
    # Reentrant callback writers cannot replace any authority in our read set.
    if manifest(path) != bound or _load(path) != rows:
        PersistenceSentinel.record_failure(path, "authority changed during broker observation")
        raise ValueError("authority changed during broker observation")
    _identity_and_authority(client, path)
    if manifest(path) != bound or _load(path) != rows:
        PersistenceSentinel.record_failure(path, "authority changed during proof verification")
        raise ValueError("authority changed during proof verification")
    return _move(path, rows, key, state, reason, evidence)


def _upgrade_legacy(client, path):
    raw = JsonStore.load(path, {})
    if not isinstance(raw, dict):
        raise ValueError("malformed transport intent collection")
    legacy = {key: row for key, row in raw.items() if not isinstance(row, dict) or row.get("schema") != SCHEMA}
    if not legacy:
        return
    identity, _ = _identity_and_authority(client, path)
    if any(not _legacy_valid(key, row) or row["identity"] != identity for key, row in legacy.items()):
        raise ValueError("unknown legacy intent cannot be automatically migrated")
    draft, now = copy.deepcopy(raw), time.time()
    for key, old in legacy.items():
        draft[key] = {**copy.deepcopy(old), "schema": SCHEMA, "intent_id": key,
            "created_at": now, "sent_at": now, "state": "UNKNOWN",
            "legacy_evidence": copy.deepcopy(old), "history": [
                {"from": None, "to": "PREPARED", "at": now, "reason": "legacy_intent_retained"},
                {"from": "PREPARED", "to": "SENT", "at": now, "reason": "legacy_dispatch_may_have_occurred"},
                {"from": "SENT", "to": "UNKNOWN", "at": now, "reason": "legacy_dispatch_phase_unproven"}]}
    _save(path, draft, expected=raw)


def reconcile_transport_intents(client):
    """Startup and cycle reconciliation: read broker, persist transitions, never mutate broker."""
    path = _p(FILE)
    try:
        with root_lock(path):
            if recovery_problem(path) or active_transaction(path) or not PersistenceSentinel.healthy():
                raise ValueError("transport recovery requires healthy authority")
            _upgrade_legacy(client, path)
            rows = _load(path)
            if not rows or all(row["state"] in TERMINAL for row in rows.values()):
                return {key: row["state"] for key, row in rows.items()}
            identity, _ = _identity_and_authority(client, path)
            if any(row["identity"] != identity for row in rows.values()):
                raise ValueError("transport account mismatch")
            for key in list(rows):
                rows = _reconcile_one(client, path, rows, key)
            return {key: row["state"] for key, row in rows.items()}
    except Exception as exc:
        # An unavailable provider is a conservative pause, not data corruption.
        return {"RECOVERY_REQUIRED": type(exc).__name__ + ": " + str(exc)}


def outcome_for_client_order(client_order_id):
    """Read-only bridge to high-level pending-intent adoption/closure."""
    path = _p(FILE)
    try:
        rows = _load(path)
        found = [r for r in rows.values() if r["request"].get("json", {}).get("client_order_id") == client_order_id]
        if len(found) == 1:
            return copy.deepcopy(found[0])
    except (ValueError, TypeError, KeyError):
        pass
    return None


def durable_transport(method):
    @functools.wraps(method)
    def request(self, verb, path, *, retries=3, **kwargs):
        from kalshi_client import _is_mutating_method, _normalized_http_method, KalshiAPIError
        if not _is_mutating_method(verb):
            return method(self, verb, path, retries=retries, **kwargs)
        self._assert_broker_write_allowed(str(verb) + " " + str(path))
        if set(kwargs) - {"json", "params"}:
            raise KalshiAPIError(0, "only economic JSON/params may enter intent storage; authentication/transport options refused")
        target = _p(FILE)
        with root_lock(target):
            try:
                if recovery_problem(target) or active_transaction(target) or not PersistenceSentinel.healthy():
                    raise ValueError("authoritative state unavailable")
                identity, _ = _identity_and_authority(self, target)
                rows = _load(target)
                if any(row["identity"] != identity for row in rows.values()):
                    raise ValueError("transport account mismatch")
                if any(row["state"] not in TERMINAL for row in rows.values()):
                    raise ValueError("mutation unresolved; reconciliation required")
                body = copy.deepcopy(kwargs)
                payload = {"identity": identity, "operation": _normalized_http_method(verb), "path": path, "request": body}
                digest = _digest(payload)
                cid = body.get("json", {}).get("client_order_id") if isinstance(body.get("json"), dict) else None
                if any(r["digest"] == digest or (cid and r["request"].get("json", {}).get("client_order_id") == cid)
                       for r in rows.values()):
                    raise ValueError("economic request identity already consumed")
                key = uuid.uuid4().hex
                row = {**payload, "schema": SCHEMA, "intent_id": key, "digest": digest,
                       "generation": manifest(target)["generation"] + 1, "created_at": time.time(),
                       "state": "PREPARED", "history": [{"from": None, "to": "PREPARED", "at": time.time(), "reason": "intent_created"}]}
                prior_rows = copy.deepcopy(rows)
                rows[key] = row
                _save(target, rows, expected=prior_rows)
                rows = _move(target, rows, key, "SENT", "dispatch_reserved")
                # Exact durable intent and current external checkpoint precede handoff.
                bound = manifest(target)
                _identity_and_authority(self, target)
                if manifest(target) != bound or recovery_problem(target) or _load(target) != rows:
                    raise ValueError("intent changed before handoff")
            except Exception as exc:
                raise KalshiAPIError(0, str(exc)) from exc
            try:
                response = method(self, verb, path, retries=0, **copy.deepcopy(body))
            except BeforeSendFailure as exc:
                _move(target, rows, key, "CONFIRMED_NOT_APPLIED", "adapter_proved_no_dispatch")
                raise KalshiAPIError(0, "request not sent") from exc
            except Exception:
                _move(target, rows, key, "UNKNOWN", "transport_outcome_ambiguous")
                # Attempt only independent reads/verified outcome evidence.
                _reconcile_one(self, target, _load(target), key)
                raise
            try:
                response_digest = _digest(response)
            except (ValueError, TypeError):
                _move(target, rows, key, "UNKNOWN", "malformed_acknowledgement")
                raise KalshiAPIError(0, "malformed mutation acknowledgement")
            rows = _move(target, rows, key, "ACKNOWLEDGED", "response_received",
                         {"response_digest": response_digest})
            _reconcile_one(self, target, rows, key)
            return response
    return request
