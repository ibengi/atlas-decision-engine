"""Complete-write, torn-tail-safe, cross-process-serialized appends.

SHADOW ONLY: no execution, broker or CAPITAL authority. Imports `errno`,
`fcntl`, `os` and `contextlib` and nothing else.

AA-12 -- SHORT WRITES AND TORN TAILS
    `os.write()` is permitted to write fewer bytes than it was handed. Every
    append-only writer in this subsystem called it once and ignored the return
    value, so a short write produced a truncated line that the reader then
    classified as "torn tail, crash during append" and skipped -- a silent,
    permanent loss of one ledger row that looked exactly like a clean crash.

    Two rules here, and the second is the one that matters for history:

      1. `write_all` loops until every byte is accepted, retrying on EINTR.
      2. `append_line` REPAIRS A TORN TAIL BY SEPARATION, NEVER BY TRUNCATION.
         If the file does not end in a newline, the last record is incomplete.
         Appending directly would splice the new row onto the broken one and
         destroy both. Truncating back to the last newline would destroy
         evidence -- and a partially written row may be the only record that a
         write was attempted at all. So a single newline is appended first:
         the damaged fragment becomes its own line, the reader reports it as
         unparseable instead of skipping it as a tail, and every historical
         byte is still on disk.

AA-14 -- MULTI-WRITER RACES
    A check-then-append sequence ("is this prediction already recorded? no ->
    append it") is not atomic across processes. Two Alpha writers could both
    read "no" and both append. `exclusive_lock` serializes the WHOLE
    check-and-append critical section on a sidecar lock file, so the check and
    the write that depends on it cannot be interleaved.

    `fcntl.flock` is advisory and per-host: it makes concurrent writers on ONE
    machine safe, which is the deployment this subsystem actually has (one
    Alpha service, one volume). It does NOT make two hosts sharing a network
    filesystem safe, and this module does not pretend otherwise -- see
    `docs/design/alpha-writer-model.md` for the documented writer model.
"""

import contextlib
import errno
import os

try:
    import fcntl
except ImportError:                                    # pragma: no cover
    fcntl = None                                       # non-POSIX platform

LOCK_SUFFIX = ".lock"

#: V4-RA-05. Pathnames whose parent-directory barrier this PROCESS has
#: confirmed. See `docs/design/budget-durability-protocol.md` §1.
#:
#: WHY A SET IS ENOUGH, AND WHY IT MAY BE PROCESS-LOCAL
#:   It is only ever consulted to SKIP a barrier that has already succeeded,
#:   never to skip one that has not. So every way of losing it -- a restart, a
#:   crash, a race between two threads -- costs a REDUNDANT barrier and can
#:   never cause a missing one. That is the opposite of the memory-only latch
#:   V4-RA-06 is about, where losing the state restored admission; the two
#:   look alike and differ in the sign of their failure, which is why this one
#:   is safe in memory and that one is not.
#:
#:   No lock is taken for the same reason. `set.add` and `set.discard` of a
#:   single element are atomic under the GIL, and the only interleaving a
#:   reader can observe is "not yet proven", which costs an extra fsync. A
#:   lock here would also mean importing `threading` into a module whose
#:   import list is pinned by `tests/test_research_feed_boundary.py`, and
#:   widening a pinned boundary to buy nothing is not a trade worth making.
_PROVEN_PATHNAMES = set()


def _pathname_key(path: str) -> str:
    return os.path.abspath(path)


def pathname_durability_proven(path: str) -> bool:
    """Has THIS PROCESS confirmed the directory barrier for `path`?

    V4-RA-05. The question `os.path.exists` was standing in for, asked
    honestly. Existence answers "are there bytes under this name"; this
    answers "did a parent-directory fsync that covers this name actually
    succeed", and only a successful `fsync_directory` can make it true.
    """
    return _pathname_key(path) in _PROVEN_PATHNAMES


def forget_pathname_durability(path: str = None) -> None:
    """Return `path` (or everything) to UNPROVEN.

    Called when a barrier fails, so a proof recorded earlier cannot outlive
    the evidence for it, and available to tests that need to simulate the
    restart in which every pathname is unproven again.
    """
    if path is None:
        _PROVEN_PATHNAMES.clear()
        return
    _PROVEN_PATHNAMES.discard(_pathname_key(path))


class DurabilityUnknown(OSError):
    """We cannot establish whether an append is durable, so we do not say it is.

    RA-05 -- UNCERTAINTY IS NOT SUCCESS
        `append_line`'s contract is the strongest promise in this subsystem:
        it "either completes the whole sequence or raises -- there is no
        outcome in which a caller is told 'written' without durability having
        been attempted AND confirmed". Everything above it -- PREPARE, the
        COMMIT receipt, the processed acknowledgement -- is built on that one
        sentence being true.

        Three paths inside it made the sentence false by swallowing the
        uncertainty instead of reporting it:

          `tail_is_torn`      returned False on ANY `OSError`. An unreadable
                              tail is not an intact tail: if we cannot see
                              whether the last record is complete, appending
                              directly may splice the new row onto a broken
                              one -- the exact AA-12 loss the separator
                              exists to prevent -- and the caller was told it
                              succeeded.

          directory OPEN      returned silently, so the bytes became durable
                              under a name that was not.

          directory FSYNC     passed silently, with the same consequence: a
                              crash then reads as a ledger that never
                              existed, and the caller had already published a
                              terminal acknowledgement for it.

        It subclasses `OSError` deliberately. Every caller in this subsystem
        already treats an `OSError` from an append as a failure to be
        reported and retried, so closing these holes could not silently turn
        a swallowed error into an unhandled crash somewhere -- while a caller
        that wants to tell "the device refused" from "we cannot tell" still
        can, by type.
    """


def write_all(fd, payload: bytes) -> int:
    """Write every byte or raise. See AA-12 above."""
    view = memoryview(payload)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise
        if count <= 0:
            raise OSError(f"short write: {written}/{len(view)} bytes written "
                          f"and the device accepted no more")
        written += count
    return written


def tail_is_torn(path: str) -> bool:
    """True when the file exists, is non-empty and does not end in a newline.

    That is the signature of an append interrupted partway: every complete row
    this module writes ends in `\\n`.

    Raises `DurabilityUnknown` when the question cannot be answered (RA-05).
    An unreadable tail is not an intact one.
    """
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return False                 # no file, so certainly no torn tail
    except OSError as exc:
        # RA-05: the file exists and we cannot measure it. That is not "no
        # tail"; it is "we do not know", and the difference decides whether
        # the next append splices onto a broken row.
        raise DurabilityUnknown(
            f"cannot size {path} to check its tail: {exc}") from exc
    if size == 0:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError as exc:
        raise DurabilityUnknown(
            f"cannot read the last byte of {path}: {exc}") from exc


@contextlib.contextmanager
def exclusive_lock(path: str, *, timeout: float = 10.0):
    """Hold an exclusive advisory lock for `path` (AA-14).

    The lock lives in a SIDECAR file rather than on the ledger itself, so
    taking it never opens the ledger for writing and a crash holding the lock
    cannot leave the ledger in a half-open state. On a platform without
    `fcntl` the lock degrades to a no-op and the caller is single-writer by
    assumption -- stated rather than silently assumed.
    """
    if fcntl is None:                                  # pragma: no cover
        yield None
        return
    lock_path = path + LOCK_SUFFIX
    parent = os.path.dirname(os.path.abspath(lock_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if deadline <= 0:
                    raise TimeoutError(
                        f"could not acquire the writer lock for {path} "
                        f"within {timeout}s; another writer is active")
                import time as _time
                _time.sleep(0.02)
                deadline -= 0.02
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_line(path: str, line: str) -> None:
    """Durably append one newline-terminated line. Never rewrites history.

    RETURNS ONLY AFTER `fsync` HAS SUCCEEDED (AA-13 re-audit). `write()`
    returning means the bytes are in the page cache, from which they read
    back perfectly while still being one power cut away from never having
    existed. Only a successful `fsync` makes a read-back meaningful, so this
    function either completes the whole sequence or raises -- there is no
    outcome in which a caller is told "written" without durability having
    been attempted AND confirmed.

    Caller holds `exclusive_lock` when the append depends on a prior read.
    The torn-tail check below is itself such a read, so `serialized_append`
    is what every writer should actually call.
    """
    if not line.endswith("\n"):
        line += "\n"
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # V4-RA-05 -- EXISTENCE IS NOT PROOF THAT THE NAME WAS EVER PERSISTED.
    #
    # This used to be `created = not os.path.exists(path)`, and the barrier
    # below ran only when `created` was true. The bytes reach the disk BEFORE
    # the barrier, so the sequence
    #
    #     append #1: file created, bytes fsynced, dir fsync FAILS -> raise
    #     append #2: os.path.exists(path) is now True -> created=False
    #                -> no barrier is even ATTEMPTED -> returns "durable"
    #
    # acknowledged an append whose pathname had never been persisted, while
    # the very fault that prevented it was still present. The file existing
    # was evidence that append #1 wrote bytes; it was never evidence that
    # append #1's directory entry survived.
    #
    # So the obligation is tracked as what it is: a barrier this process has
    # CONFIRMED. A pathname that does not exist yet certainly owes one; a
    # pathname that exists but has never been proven owes one too, which is
    # exactly the retry above and exactly the state every pathname is in
    # after a restart.
    created = not os.path.exists(path)
    needs_barrier = bool(parent) and (created
                                      or not pathname_durability_proven(path))
    separator = b"\n" if tail_is_torn(path) else b""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if separator:
            # AA-12: close the damaged fragment WITHOUT removing it. The old
            # bytes stay exactly where they are; they simply stop being able
            # to swallow the row we are about to write.
            write_all(fd, separator)
        write_all(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if needs_barrier:
        # The bytes are durable; the NAME they live under is a separate
        # write. Without this a crash can leave a fsynced file that no
        # directory entry points at, which reads afterwards as a ledger that
        # never existed. RA-05: a failure here RAISES, because a name that is
        # not durable is not a durable append.
        #
        # The proof is dropped FIRST. If the barrier raises, the pathname must
        # be left UNPROVEN -- and if it was somehow proven earlier, that proof
        # is now contradicted by evidence and must not survive the failure.
        forget_pathname_durability(path)
        fsync_directory(parent)
        _PROVEN_PATHNAMES.add(_pathname_key(path))


def fsync_directory(parent: str) -> None:
    """Persist a DIRECTORY ENTRY, or raise `DurabilityUnknown` (RA-05).

    An fsync on a file persists its contents; the name those contents live
    under is a separate write in the parent directory. Both halves have to
    land, and this used to swallow the failure of either -- so a caller could
    be told an append succeeded while the file it wrote was reachable under no
    name at all.

    Every failure is reported, including the ones a platform might consider
    benign. A filesystem that genuinely cannot fsync a directory has not made
    the name durable, and this module's whole job is to refuse to pretend
    otherwise.
    """
    try:
        fd = os.open(parent, os.O_RDONLY)
    except OSError as exc:
        raise DurabilityUnknown(
            f"cannot open {parent} to persist the directory entry: {exc}"
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise DurabilityUnknown(
            f"cannot fsync {parent}; the bytes are durable but the NAME they "
            f"live under is not: {exc}") from exc
    finally:
        os.close(fd)


#: The private spelling kept as an alias: `research_spool` and the mutation
#: probe both name it, and a rename is not what RA-05 is about.
_fsync_directory = fsync_directory


@contextlib.contextmanager
def serialized_append(path: str, *, timeout: float = 10.0):
    """Hold the writer lock for a check-then-append on `path` (AA-14).

    Yields a callable that appends one line. Every appender to a shared file
    goes through this, including the ones that "only append": the torn-tail
    check inside `append_line` is a READ of the file's last byte, and two
    unsynchronized writers can both observe an intact tail and then both
    write, or observe a torn one and both separate it.
    """
    with exclusive_lock(path, timeout=timeout):
        yield lambda line: append_line(path, line)
