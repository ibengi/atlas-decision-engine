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
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size == 0:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


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
    created = not os.path.exists(path)
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
    if created and parent:
        # The bytes are durable; the NAME they live under is a separate
        # write. Without this a crash can leave a fsynced file that no
        # directory entry points at, which reads afterwards as a ledger that
        # never existed.
        _fsync_directory(parent)


def _fsync_directory(parent: str) -> None:
    try:
        fd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
