# -*- coding: utf-8 -*-
"""AIR-001 Wave 4 (DE-P0-005) — durable order write-ahead intent journal.

Reproduced defect: nothing durable existed BEFORE the order POST. A
crash (or lost response) between the POST and the local record left an
order living at the broker with no local trace: the submission guard
and orders_state.json are only written AFTER a successful response, so
restart-time recovery had nothing to reconcile and the same signal
could be re-submitted.

Policy:
- A fsynced ORDER_INTENT_PREPARED record precedes every POST. The
  journal is append-only JSONL; state is a pure fold of events.
- An ambiguous submission result (network failure — the request may or
  may not have reached the exchange) is recorded as SUBMIT_AMBIGUOUS
  and is NEVER retried by re-POST: resolution happens exclusively by
  querying the broker for the deterministic client_order_id.
- On startup, any unresolved intent puts order submission in
  TRADING_BLOCKED_RECONCILING: no new order may be placed until every
  in-flight intent is resolved against the broker (fail-closed — if the
  broker cannot be queried, trading stays blocked).
- A same-host duplicate engine process is refused via an exclusive
  file lock on the data directory.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from config import _p

log_api = logging.getLogger("API")

JOURNAL_FILE = "order_intents.jsonl"
LOCK_FILE = "engine_orders.lock"

# Event types (append-only; state is a fold, never an overwrite)
INTENT_PREPARED = "INTENT_PREPARED"
SUBMIT_ACKNOWLEDGED = "SUBMIT_ACKNOWLEDGED"
SUBMIT_REJECTED = "SUBMIT_REJECTED"        # HTTP response received: definite
SUBMIT_AMBIGUOUS = "SUBMIT_AMBIGUOUS"      # network: order MAY exist
INTENT_CLOSED = "INTENT_CLOSED"            # terminal, with outcome

# Fold states
S_PREPARED = "PREPARED"                    # intent written, no submit outcome
S_ACKNOWLEDGED = "ACKNOWLEDGED"            # broker returned an order_id
S_AMBIGUOUS = "AMBIGUOUS"                  # submit outcome unknown
S_CLOSED = "CLOSED"                        # terminal

_UNRESOLVED = {S_PREPARED, S_ACKNOWLEDGED, S_AMBIGUOUS}


class DuplicateProcessError(RuntimeError):
    """Another engine process holds the order-submission lock."""


# Same-process re-entry is legitimate (tests, sub-components sharing one
# DATA_DIR); cross-process concurrency is not. flock conflicts between
# separate fds even within one process, so held paths are registered.
_HELD_LOCKS: dict[str, int] = {}


def acquire_order_submission_lock(path: Optional[str] = None) -> None:
    """Exclusive advisory lock; raises DuplicateProcessError if another
    process holds it. Auto-released by the OS on process death, so a
    crash never wedges the engine."""
    lock_path = os.path.realpath(path or _p(LOCK_FILE))
    if lock_path in _HELD_LOCKS:
        return
    import fcntl
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            raise DuplicateProcessError(
                f"order submission lock {lock_path} held by another "
                "process — duplicate engine instance refused") from exc
        raise
    os.write(fd, f"{os.getpid()}\n".encode())
    _HELD_LOCKS[lock_path] = fd


def release_order_submission_lock(path: Optional[str] = None) -> None:
    lock_path = os.path.realpath(path or _p(LOCK_FILE))
    fd = _HELD_LOCKS.pop(lock_path, None)
    if fd is not None:
        os.close(fd)                       # closing releases the flock


class OrderIntentJournal:
    """Append-only fsynced JSONL journal of order intents."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or _p(JOURNAL_FILE)

    # -- write ----------------------------------------------------------
    def _append(self, event: dict) -> None:
        line = json.dumps(event, sort_keys=True, default=str)
        # fsync BEFORE the caller may POST: the durability of the intent
        # is the whole point of the write-ahead record.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                     0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def prepare(self, *, ticker: str, side: str, count: int,
                limit_cents: int, client_order_id: str,
                decision_id: Optional[str] = None,
                validated_execution_intent_hash: Optional[str] = None,
                risk_proof_hash: Optional[str] = None,
                engine_commit: Optional[str] = None) -> str:
        intent_id = f"oi_{uuid.uuid4().hex[:16]}"
        self._append({
            "event": INTENT_PREPARED, "intent_id": intent_id,
            "ticker": ticker, "side": side, "count": int(count),
            "limit_cents": int(limit_cents),
            "client_order_id": client_order_id,
            "decision_id": decision_id,
            "validated_execution_intent_hash":
                validated_execution_intent_hash,
            "risk_proof_hash": risk_proof_hash,   # None until Wave 6 —
            "engine_commit": engine_commit,       # absent, never invented
            "created_at": time.time(),
        })
        return intent_id

    def acknowledged(self, intent_id: str, order_id: str,
                     http_status: Any = None) -> None:
        self._append({"event": SUBMIT_ACKNOWLEDGED,
                      "intent_id": intent_id, "order_id": order_id,
                      "http_status": http_status, "ts": time.time()})

    def rejected(self, intent_id: str, status: Any, error: str) -> None:
        self._append({"event": SUBMIT_REJECTED, "intent_id": intent_id,
                      "status": status, "error": str(error)[:500],
                      "ts": time.time()})

    def ambiguous(self, intent_id: str, error: str) -> None:
        self._append({"event": SUBMIT_AMBIGUOUS, "intent_id": intent_id,
                      "error": str(error)[:500], "ts": time.time()})

    def closed(self, intent_id: str, outcome: str,
               **fields: Any) -> None:
        event = {"event": INTENT_CLOSED, "intent_id": intent_id,
                 "outcome": outcome, "ts": time.time()}
        event.update(fields)
        self._append(event)

    # -- read (pure fold) ----------------------------------------------
    def events(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # A torn final line is expected after a crash
                    # mid-append; it is reported, never silently eaten.
                    log_api.error(
                        f"[INTENT_JOURNAL] torn/corrupt line ignored in "
                        f"{self.path}: {line[:120]!r}")
        return out

    def states(self) -> dict[str, dict]:
        folded: dict[str, dict] = {}
        for ev in self.events():
            iid = ev.get("intent_id")
            if not iid:
                continue
            kind = ev.get("event")
            if kind == INTENT_PREPARED:
                folded[iid] = dict(ev, state=S_PREPARED)
            elif iid not in folded:
                # An event for an unknown intent is itself a finding —
                # the PREPARED record must always precede everything.
                folded[iid] = dict(ev, state=S_AMBIGUOUS,
                                   missing_prepared=True)
            elif kind == SUBMIT_ACKNOWLEDGED:
                folded[iid].update(ev, state=S_ACKNOWLEDGED)
            elif kind == SUBMIT_REJECTED:
                folded[iid].update(ev, state=S_CLOSED,
                                   outcome="rejected")
            elif kind == SUBMIT_AMBIGUOUS:
                folded[iid].update(ev, state=S_AMBIGUOUS)
            elif kind == INTENT_CLOSED:
                folded[iid].update(ev, state=S_CLOSED)
        return folded

    def unresolved(self) -> list[dict]:
        return [s for s in self.states().values()
                if s["state"] in _UNRESOLVED]


def resolve_unresolved_intents(journal: OrderIntentJournal, client
                               ) -> dict:
    """Resolve every in-flight intent against the broker. Fail-closed:
    an intent that cannot be proven either way stays unresolved and the
    caller must keep order submission blocked.

    Resolution is exclusively by broker query for the deterministic
    client_order_id — NEVER by re-POST.
    """
    report = {"resolved": [], "adopted": [], "still_unresolved": []}
    unresolved = journal.unresolved()
    if not unresolved:
        return report
    log_api.warning(f"[INTENT_RECOVERY] {len(unresolved)} in-flight "
                    "order intent(s) at startup — order submission "
                    "BLOCKED until each is resolved against the broker")
    broker_orders = None
    try:
        broker_orders = client.get_orders()
    except Exception as exc:               # noqa: BLE001 — fail closed
        log_api.error(f"[INTENT_RECOVERY] broker order listing failed: "
                      f"{exc} — intents stay unresolved, trading stays "
                      "blocked")
    by_client_id = {}
    if broker_orders is not None:
        for o in broker_orders:
            cid = (o or {}).get("client_order_id")
            if cid:
                by_client_id[str(cid)] = o

    for st in unresolved:
        iid = st["intent_id"]
        cid = st.get("client_order_id")
        if st["state"] == S_ACKNOWLEDGED:
            # order_id is known — tracking belongs to orders_state /
            # reconcile_startup; the intent itself is complete.
            journal.closed(iid, "ADOPTED_FOR_RECOVERY",
                           order_id=st.get("order_id"))
            report["adopted"].append(
                {"intent_id": iid, "order_id": st.get("order_id")})
            continue
        if broker_orders is None:
            report["still_unresolved"].append(iid)
            continue
        found = by_client_id.get(str(cid)) if cid else None
        if found is not None:
            order_id = str(found.get("order_id") or found.get("id")
                           or "")
            journal.closed(iid, "FOUND_AT_BROKER", order_id=order_id)
            report["adopted"].append(
                {"intent_id": iid, "order_id": order_id,
                 "ticker": st.get("ticker"), "side": st.get("side"),
                 "count": st.get("count"),
                 "limit_cents": st.get("limit_cents")})
            log_api.warning(f"[INTENT_RECOVERY] intent {iid} "
                            f"({st.get('ticker')}) FOUND at broker as "
                            f"order {order_id} — adopted for tracking")
        else:
            journal.closed(iid, "NOT_SUBMITTED_CONFIRMED")
            report["resolved"].append(iid)
            log_api.info(f"[INTENT_RECOVERY] intent {iid} "
                         f"({st.get('ticker')}) absent from broker — "
                         "confirmed never submitted")
    return report
