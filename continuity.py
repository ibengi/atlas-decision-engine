"""Append-only continuity chain: the independent anti-rollback authority.

Audit finding A01. Before this module the only record of "how much history
has been durably observed" was the ``journal_watermark`` field *inside*
``equity_ledger.json``. That made the evidence exactly as rewindable as the
thing it protected: restoring the ledger restored the watermark, deleting
the field read as "no history", and a zero counter disabled the check
entirely. A checksum written beside the same bytes shares the same fate.

The chain fixes that by putting the evidence somewhere the rewind does not
reach and by making a rewind *structurally* detectable:

  * **Separate file.** ``equity_continuity.log`` is not a ``JsonStore``
    file: it has no ``.bak`` rotation and no sidecar checksum, so the
    backup/restore machinery that rewinds the ledger does not rewind it.
  * **Append-only.** Records are appended with ``O_APPEND`` and fsynced.
    Nothing in the engine ever truncates or rewrites the file.
  * **Hash-chained.** Every record carries the hash of its predecessor, so
    a truncation, a reordering or an edited record breaks the chain and is
    refused rather than believed.
  * **Monotonic sequence.** ``seq`` increases by exactly one. A gap or a
    repeat is a broken chain.
  * **Strict schema.** A record missing a field, carrying a non-finite
    number or an unknown kind invalidates the chain. Absence of a field is
    never read as a benign default.

The chain is a *floor*, never a source of truth for values: it says what
was once durably true, so that anything claiming less is refused. The
ledger must always be at or above the chain. Ledger below chain, journal
below chain, or a consumed token missing from the ledger while the chain
holds it, all mean the same thing: state was rolled back. That is a
blocking recovery condition, not a number to keep computing with.

Known limitation, stated rather than papered over: a restore that also
removes or rewinds this file cannot be detected from inside the process,
because no purely local artefact can outrank a wholesale rewind of the
disk it lives on. What the chain guarantees is that any rollback which
leaves it in place — the coordinated journal+ledger restore, the stale
concurrent writer, the crash between two commits, the backup recovery of
an older ledger — fails closed. Detecting the wholesale case needs an
authority outside this filesystem (broker funding records, an append-only
remote log); the ledger's ``UNRECONCILED``/attestation path is where such
external evidence enters.
"""

import fcntl
import hashlib
import json
import logging
import math
import os

log = logging.getLogger("CONTINUITY")

CONTINUITY_FILE = "equity_continuity.log"
CHAIN_VERSION = 1

#: Record kinds. An unknown kind invalidates the chain: a future writer's
#: record must not be silently skipped by an older reader that would then
#: believe less history than actually exists.
KIND_EVIDENCE = "evidence"        # journal + ledger history durably observed
KIND_TOKEN = "token_consumed"     # a single-use authorization was consumed
KIND_RECOVERY = "recovery"        # an operator re-established continuity
KINDS = (KIND_EVIDENCE, KIND_TOKEN, KIND_RECOVERY)

GENESIS = "0" * 64


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and math.isfinite(float(x))


class ChainError(Exception):
    """The chain on disk cannot be trusted. Never swallowed into a default."""


class ContinuityChain:
    """Append-only hash chain of durably observed economic evidence."""

    def __init__(self, path: str):
        self.path = path
        self._records = None      # parsed cache
        self._error = None
        self._stat = None         # (size, mtime_ns) the cache was read at

    def _current_stat(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def _invalidate_if_changed(self) -> None:
        """The chain is append-only but NOT single-writer: another process,
        or another ledger object in this one, may have appended since this
        instance last read. A cache that survives that is how a stale reader
        computes the wrong `seq`/`prev` and corrupts the very file it is
        supposed to protect."""
        stat = self._current_stat()
        if stat != self._stat:
            self._records = None
            self._error = None
            self._stat = None

    # ── reading ─────────────────────────────────────────────────────────
    @staticmethod
    def record_hash(rec: dict) -> str:
        return _sha(_canonical({"seq": rec["seq"], "prev": rec["prev"],
                                "kind": rec["kind"], "at": rec["at"],
                                "payload": rec["payload"],
                                "version": rec["version"]}))

    @classmethod
    def _validate_record(cls, rec, expected_seq: int, expected_prev: str) -> None:
        if not isinstance(rec, dict):
            raise ChainError(f"record {expected_seq} is not an object")
        for field in ("seq", "prev", "kind", "at", "payload", "version", "hash"):
            if field not in rec:
                raise ChainError(f"record {expected_seq} misses '{field}' "
                                 f"(a missing field is never a default)")
        if rec["version"] != CHAIN_VERSION:
            raise ChainError(f"record {expected_seq} has unknown chain version "
                             f"{rec['version']!r}")
        if rec["seq"] != expected_seq:
            raise ChainError(f"sequence break: expected {expected_seq}, "
                             f"found {rec['seq']!r}")
        if rec["prev"] != expected_prev:
            raise ChainError(f"hash chain break at record {expected_seq}")
        if rec["kind"] not in KINDS:
            raise ChainError(f"record {expected_seq} has unknown kind "
                             f"{rec['kind']!r}")
        if not isinstance(rec["payload"], dict):
            raise ChainError(f"record {expected_seq} payload is not an object")
        if cls.record_hash(rec) != rec["hash"]:
            raise ChainError(f"record {expected_seq} hash does not match "
                             f"its content (edited record)")

    def records(self) -> list:
        """Every validated record, oldest first. Raises ChainError when the
        file exists but cannot be trusted."""
        self._invalidate_if_changed()
        if self._records is not None:
            return self._records
        if self._error is not None:
            raise self._error
        stat = self._current_stat()
        out = []
        if not os.path.exists(self.path):
            self._records, self._stat = out, stat
            return out
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            self._error = ChainError(f"continuity chain unreadable: {e}")
            raise self._error
        seq, prev = 1, GENESIS
        for i, line in enumerate(lines):
            if not line.strip():
                # A torn tail (crash mid-append) is only tolerable as the
                # very last line: anything else is a hole in the history.
                if i == len(lines) - 1:
                    break
                self._error = ChainError(f"blank record at line {i + 1}")
                raise self._error
            try:
                rec = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    log.warning("[CONTINUITY] torn last record ignored "
                                "(crash during append); chain head unchanged")
                    break
                self._error = ChainError(f"unparsable record at line {i + 1}")
                raise self._error
            try:
                self._validate_record(rec, seq, prev)
            except ChainError as e:
                self._error = e
                raise
            out.append(rec)
            prev = rec["hash"]
            seq += 1
        self._records, self._stat = out, stat
        return out

    def head(self):
        recs = self.records()
        return recs[-1] if recs else None

    def head_pointer_safe(self) -> dict:
        """`head_pointer` for callers that must not raise -- reporting a
        broken chain is not the moment to raise from the reporter."""
        try:
            return self.head_pointer()
        except ChainError:
            return {"seq": None, "hash": None, "unreadable": True}

    def healthy(self) -> tuple:
        """(ok, reason). Never raises: callers use it to fail closed."""
        try:
            self.records()
            return True, None
        except ChainError as e:
            return False, str(e)

    # ── writing ─────────────────────────────────────────────────────────
    def append(self, kind: str, payload: dict, at: str) -> dict:
        """Append one record durably. Raises ChainError on any failure so a
        caller can never mistake "not written" for "written"."""
        if kind not in KINDS:
            raise ChainError(f"refusing to append unknown kind {kind!r}")
        parent = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(parent, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError as e:
            raise ChainError(f"continuity record not durable: {e}")
        try:
            # Serialize appenders. Reading the tail and writing the next
            # record must be one step: two writers that each read seq=3 and
            # each write seq=4 leave a chain that validates as broken -- and
            # a broken chain fails everything closed, which is safe but
            # self-inflicted.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:            # pragma: no cover - platform dependent
                pass
            self._records, self._error, self._stat = None, None, None
            try:
                recs = self.records()
            except ChainError as e:
                raise ChainError(f"refusing to append onto a broken chain: {e}")
            prev = recs[-1]["hash"] if recs else GENESIS
            rec = {"version": CHAIN_VERSION, "seq": len(recs) + 1, "prev": prev,
                   "kind": kind, "at": at, "payload": payload}
            rec["hash"] = self.record_hash(rec)
            line = json.dumps(rec, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False) + "\n"
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            except OSError as e:
                raise ChainError(f"continuity record not durable: {e}")
            self._fsync_dir(parent)
        finally:
            os.close(fd)
        self._records, self._error, self._stat = None, None, None
        return rec

    @staticmethod
    def _fsync_dir(parent: str) -> None:
        """Make the append visible after a crash, not just the bytes."""
        try:
            dfd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:            # pragma: no cover - platform dependent
            pass

    # ── derived views ───────────────────────────────────────────────────
    def evidence_floor(self):
        """The strongest evidence ever recorded, as a floor:

          settled_count   the most settled rows ever observed
          digest          the digest of that many rows at the time
          strategy_equity the LOWEST strategy equity ever observed at or
                          after the deepest history (a loss, once seen,
                          bounds equity from above forever)
          realized_pnl_cum the cumulative realized PnL at that point

        Returns None when nothing was ever recorded. Raises ChainError on a
        broken chain: an unreadable floor is not a zero floor.
        """
        best = None
        for rec in self.records():
            if rec["kind"] != KIND_EVIDENCE:
                continue
            p = rec["payload"]
            n = p.get("settled_count")
            if not isinstance(n, int) or n < 0:
                raise ChainError(f"record {rec['seq']}: settled_count "
                                 f"{n!r} is not a count")
            eq = p.get("strategy_equity")
            if eq is not None and not _finite(eq):
                raise ChainError(f"record {rec['seq']}: strategy_equity "
                                 f"{eq!r} is not finite")
            if best is None or n > best["settled_count"]:
                best = {"settled_count": n, "digest": p.get("digest"),
                        "strategy_equity": eq,
                        "realized_pnl_cum": p.get("realized_pnl_cum"),
                        "seq": rec["seq"]}
            elif n == best["settled_count"]:
                best["digest"] = best["digest"] or p.get("digest")
                if eq is not None and (best["strategy_equity"] is None
                                       or eq < best["strategy_equity"]):
                    best["strategy_equity"] = eq
                best["seq"] = rec["seq"]
        return best

    def consumed_tokens(self) -> set:
        """Hashes of every single-use token the chain ever saw consumed."""
        out = set()
        for rec in self.records():
            if rec["kind"] == KIND_TOKEN:
                h = rec["payload"].get("token_sha256")
                if not isinstance(h, str) or len(h) != 64:
                    raise ChainError(f"record {rec['seq']}: token_sha256 "
                                     f"{h!r} is not a sha256")
                out.add(h)
        return out

    def head_pointer(self) -> dict:
        """What a ledger stores so a later load can tell whether the chain
        it sees is the chain it was written against."""
        head = self.head()
        return {"seq": head["seq"] if head else 0,
                "hash": head["hash"] if head else GENESIS}
