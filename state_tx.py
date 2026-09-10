"""One transaction primitive for authoritative economic state.

Astra findings A13 (concurrent writers), A14 (premature publication),
A16 (non-finite data), A18 (write durability) and A19 (stale readers) were
each an instance of the same structural problem: every operation invented
its own partial transaction. A generation was validated in one statement
and committed in another; a prepared state was installed in the live object
and rolled back afterwards; ``os.write`` was called once and believed;
``fsync`` on the parent directory was allowed to fail silently; a reader
kept deciding from an authority another process had already moved past.

This module holds the four mechanisms those operations must share.

``WriterFence``
    An OS-level exclusive lock (``flock``) on a sidecar file, held across
    *read generation -> validate -> write -> publish*. Two processes that
    both loaded generation N cannot both commit N+1: the second blocks
    until the first has published, then sees N+1 and is refused. The fence
    is re-entrant inside one process (``flock`` is per open-file
    description, so a naive nested acquire would deadlock against itself)
    and its acquisition is what makes the check-and-set atomic *from the
    application's point of view* -- which is the property A13 asks for.
    Losing the fence, or failing to acquire it, is a refusal: it never
    degrades to "write anyway".

``durable_write_all`` / ``fsync_dir``
    A write is complete when every intended byte is written, not when
    ``os.write`` returns. Short writes, ``EINTR`` and partial progress are
    looped; a zero-byte return is treated as failure rather than as an
    infinite loop; and a failing directory fsync is propagated, because a
    rename nobody fsynced is a rename a crash can undo (A18).

``check_finite``
    ``NaN`` compares False against everything, so every ``>`` threshold
    silently admits it. Economic inputs are validated explicitly with
    ``math.isfinite`` at the boundary, and a non-finite value is refused --
    never clamped, defaulted or passed through (A16).

``StaleAuthority``
    The exception a reader raises when the durable generation has moved
    past the one it holds (A19).

Nothing here knows what a ledger is. It is deliberately small: the point is
that there is exactly one implementation of each rule, not a framework.
"""

import errno
import fcntl
import math
import os
import threading

__all__ = [
    "WriterFence", "FenceError", "StaleAuthority", "NonFiniteValue",
    "durable_write_all", "fsync_dir", "check_finite", "all_finite",
    "finite_or_none", "fence_for",
]


class FenceError(Exception):
    """The writer fence could not be acquired or was lost. Never swallowed:
    a write that cannot be fenced is a write that must not happen."""


class StaleAuthority(Exception):
    """This process's view of authoritative state is behind the durable
    one. Raised before a safety-critical decision, never after it."""


class NonFiniteValue(ValueError):
    """A NaN/Inf reached an economic boundary. Fail closed."""


# ── finite validation (A16) ─────────────────────────────────────────────
def check_finite(value, what: str):
    """Return ``value`` as a float, or raise ``NonFiniteValue``.

    Accepts int/float only. ``bool`` is rejected: ``True`` silently behaving
    as ``1.0`` in a cash amount is exactly the class of accident this guards.
    Strings are rejected too -- a parser that produced ``"NaN"`` must fail
    here rather than have this function launder it into a float.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NonFiniteValue(f"{what}: {value!r} is not a number")
    f = float(value)
    if not math.isfinite(f):
        raise NonFiniteValue(f"{what}: {value!r} is not finite")
    return f


def all_finite(value) -> bool:
    """True when ``value`` -- scalar, list or dict, recursively -- contains
    no non-finite number. Strings and None are not numbers and pass; the
    caller decides separately whether they are acceptable."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, dict):
        return all(all_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(all_finite(v) for v in value)
    return True


def finite_or_none(value):
    """``float(value)`` when finite, else None. For reporting paths that
    must not raise; a decision path uses ``check_finite`` instead."""
    try:
        return check_finite(value, "value")
    except NonFiniteValue:
        return None


# ── durable writing (A18) ───────────────────────────────────────────────
def durable_write_all(fd: int, payload: bytes) -> int:
    """Write every byte of ``payload`` to ``fd`` or raise ``OSError``.

    ``os.write`` may write fewer bytes than asked (pipes, signals, some
    filesystems near a quota) and may raise ``EINTR``. Believing its first
    return value is how a truncated continuity record was reported as a
    successful append. Progress is required: a zero-byte write with nothing
    left to blame is an error, not a reason to spin.
    """
    view = memoryview(payload)
    written = 0
    total = len(payload)
    while written < total:
        try:
            n = os.write(fd, view[written:])
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise
        if n <= 0:
            raise OSError(errno.EIO,
                          f"short write: {written}/{total} bytes written, "
                          f"os.write returned {n}")
        written += n
    return written


def fsync_dir(parent: str) -> None:
    """fsync a directory so a rename/append survives a crash.

    Raises ``OSError`` on failure. Platforms where directory fsync is not
    supported at all report ``EINVAL``/``ENOTSUP`` and are tolerated -- that
    is a capability statement, not an I/O error. Everything else (``EIO``,
    ``ENOSPC``, a revoked descriptor) is a durability failure and is
    propagated: the caller must report "not durable", not continue.
    """
    try:
        fd = os.open(parent, os.O_RDONLY)
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOTSUP, errno.EACCES, errno.EPERM):
            return
        raise
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno in (errno.EINVAL, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                       errno.EOPNOTSUPP):
            return
        raise
    finally:
        os.close(fd)


# ── writer fence (A13) ──────────────────────────────────────────────────
_FENCES = {}
_FENCES_LOCK = threading.Lock()


class WriterFence:
    """Exclusive, re-entrant, cross-process ownership of one state path.

    Usage::

        with fence_for(path):
            gen = read_generation(path)     # nobody else can commit now
            ...validate...
            atomic_replace(path, payload)   # still nobody else

    Correctness relies on every writer of that path taking the same fence.
    A writer that skips it is not fenced -- so ``JsonStore.save`` takes it
    unconditionally rather than only when a generation was supplied.

    ``flock`` locks an open-file description, not a process, so a second
    ``flock`` on a second descriptor from the same process would block for
    ever against itself. The per-path ``RLock`` plus a depth counter make
    re-entry inside one process safe while still excluding other processes.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.lock_path = self.path + ".lock"
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd = None

    def acquire(self, timeout: float = 30.0) -> None:
        self._rlock.acquire()
        if self._depth > 0:
            self._depth += 1
            return
        fd = None
        try:
            parent = os.path.dirname(self.lock_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            deadline = None
            if timeout is not None:
                import time
                deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    import time
                    if deadline is not None and time.monotonic() >= deadline:
                        raise FenceError(
                            f"writer fence on {self.path} still held by "
                            f"another writer after {timeout}s")
                    time.sleep(0.01)
        except FenceError:
            if fd is not None:
                os.close(fd)
            self._rlock.release()
            raise
        except OSError as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._rlock.release()
            raise FenceError(f"writer fence on {self.path} unavailable: {e}")
        self._fd = fd
        self._depth = 1

    def release(self) -> None:
        if self._depth <= 0:
            self._rlock.release()
            raise FenceError(f"writer fence on {self.path} released twice")
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None
        self._rlock.release()

    def held(self) -> bool:
        return self._depth > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def fence_for(path: str) -> WriterFence:
    """The one fence object for this path in this process."""
    key = os.path.abspath(path)
    with _FENCES_LOCK:
        f = _FENCES.get(key)
        if f is None:
            f = WriterFence(key)
            _FENCES[key] = f
        return f
