"""Append-only transactional evidence. External anchors detect tail loss.

SQLite constraints defend normal application writes, not a hostile DBA. Durable
volume backup plus an independently retained manifest is required for admission.
No existing V1 state is loaded or migrated by this module.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from functools import wraps

from .domain import Refused, canonical, digest, now, utc


def synchronized(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self.mutex:
            return function(self, *args, **kwargs)
    return wrapped


class Store:
    def __init__(self, path):
        self.path = Path(path)
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.mutex = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if not existed:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX events_kind ON events(kind, seq);
                CREATE TRIGGER events_no_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
                CREATE TRIGGER events_no_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
                PRAGMA user_version=1;
                COMMIT;
            """)
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise Refused("unrecognized database; explicit migration required")
        self.verify()

    @contextmanager
    def transaction(self):
        with self.mutex:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    @synchronized
    def get(self, event_id):
        return self._decode(self.db.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())

    @synchronized
    def latest(self, kind):
        return self._decode(self.db.execute(
            "SELECT * FROM events WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,)).fetchone())

    @synchronized
    def events(self, kind=None):
        query = "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY seq"
        return [self._decode(row) for row in self.db.execute(query, (kind,) if kind else ())]

    def append(self, event_id, kind, payload, effective_at=None):
        with self.mutex:
            if not self.db.in_transaction:
                with self.transaction():
                    return self._append(event_id, kind, payload, effective_at)
            return self._append(event_id, kind, payload, effective_at)

    def _append(self, event_id, kind, payload, effective_at):
        if not all(isinstance(x, str) and x for x in (event_id, kind)):
            raise Refused("event identity required")
        raw = canonical(payload).decode()
        prior = self.get(event_id)
        if prior:
            if prior["kind"] != kind or canonical(prior["payload"]).decode() != raw:
                raise Refused("event identity collision")
            if effective_at is not None and prior["effective_at"] != effective_at:
                raise Refused("event timestamp collision")
            return prior
        recorded_at = now()
        effective_at = effective_at or recorded_at
        utc(effective_at)
        tail = self.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (tail["seq"] + 1, tail["hash"]) if tail else (1, "0" * 64)
        value = dict(seq=seq, event_id=event_id, kind=kind, effective_at=effective_at,
                     recorded_at=recorded_at, payload=payload, previous_hash=previous)
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                        (seq, event_id, kind, effective_at, recorded_at, raw, previous, digest(value)))
        return self.get(event_id)

    @synchronized
    def anchor(self):
        tail = self.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return {"schema": 1, "seq": tail["seq"] if tail else 0,
                "hash": tail["hash"] if tail else "0" * 64}

    @synchronized
    def verify(self, external_anchor=None):
        previous, seq = "0" * 64, 0
        found = external_anchor is None or external_anchor == {"schema": 1, "seq": 0, "hash": previous}
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            event = self._decode(row)
            saved_hash = event.pop("hash")
            if event["seq"] != seq + 1 or event["previous_hash"] != previous or digest(event) != saved_hash:
                raise Refused("evidence chain corrupted")
            seq, previous = event["seq"], saved_hash
            if external_anchor and seq == external_anchor["seq"]:
                found = external_anchor == {"schema": 1, "seq": seq, "hash": saved_hash}
        if not found:
            raise Refused("external anchor absent/mismatched; truncated or replaced history")
        return self.anchor()

    def close(self):
        self.db.close()
