"""Bounded, non-blocking research spool. SHADOW ONLY, no execution authority.

Astra AA-10 is the finding this module exists for: the previous candidate
serialized, wrote, fsynced and pruned INSIDE the engine's decision cycle. An
fsync on a slow or stalled volume therefore stalled the money path, and a
research feed that can stall the money path has become part of it.

    ENGINE OBSERVER  ->  bounded in-memory queue  ->  isolated writer thread
       (never waits)        (drops under pressure)      (owns all the I/O)

The observer's only obligation is to hand over a dict and return. It never
waits for a lock held by the writer, never waits for disk, and never learns
whether the write succeeded -- because there is no answer it could act on
without becoming coupled to research.

AA-11 is the second half: the bound must survive a filesystem that is
misbehaving. `os.listdir` failing is NOT an empty spool -- treating it as one
is how a "bounded" spool grows without limit exactly when the disk is already
in trouble. When capacity cannot be established, research writes FAIL CLOSED.

AA-12 is the third: `os.write` is permitted to write fewer bytes than it was
given, and a partial record is indistinguishable from a truncated one. Every
write here loops to completion and retries on EINTR.
"""

import errno
import json
import logging
import os
import queue
import threading
import time

log = logging.getLogger("RESEARCH_FEED")

#: Suffix for records still being written. Named so that startup cleanup can
#: recognise files this producer owns and NEVER touch anything else (AA-11).
TEMP_SUFFIX = ".partial"
RECORD_SUFFIX = ".json"


def write_all(fd, payload: bytes) -> int:
    """Write every byte or raise. AA-12.

    A single `os.write` may return a short count on a signal, a pipe, or a
    filesystem under pressure; the previous code ignored the return value
    entirely, so a short write produced a silently truncated record that later
    parsed as a torn tail. EINTR is retried because it means "nothing was
    written yet", not "the write failed".
    """
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


class SpoolCapacityUnknown(RuntimeError):
    """The spool's size could not be established, so writing is unsafe."""


class BoundedSpool:
    """A directory of JSON records bounded by COUNT and by BYTES.

    Counting files was never the real bound: 500 records of unknown size is
    not a disk budget, and the engine shares this volume. Both limits are
    enforced, and both are measured over complete records AND the temporary
    files of writes still in flight.
    """

    def __init__(self, directory, *, max_records, max_bytes,
                 max_record_bytes, max_age_s):
        self.directory = directory
        self.max_records = int(max_records)
        self.max_bytes = int(max_bytes)
        self.max_record_bytes = int(max_record_bytes)
        self.max_age_s = float(max_age_s)
        self.stats = {"written": 0, "pruned": 0, "dropped_full": 0,
                      "dropped_oversize": 0, "capacity_unknown": 0,
                      "write_errors": 0, "temp_bytes": 0, "temp_files": 0,
                      "temp_recovered": 0}

    # ── enumeration that refuses to guess ───────────────────────────────
    def _scan(self):
        """`(records, temp)` as `[(name, size, mtime)]`, or raise.

        AA-11: an `OSError` here is propagated as `SpoolCapacityUnknown`. The
        previous implementation returned `[]`, which reads as "the spool is
        empty, go ahead and write" at the exact moment the filesystem is
        failing.
        """
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return [], []
        except OSError as exc:
            raise SpoolCapacityUnknown(
                f"cannot enumerate {self.directory}: {exc}")
        records, temp = [], []
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue                      # pruned under us; not an error
            except OSError as exc:
                raise SpoolCapacityUnknown(
                    f"cannot stat {name}: {exc}")
            if not os.path.isfile(path):
                continue
            entry = (name, st.st_size, st.st_mtime)
            if name.endswith(TEMP_SUFFIX):
                temp.append(entry)
            elif name.endswith(RECORD_SUFFIX):
                records.append(entry)
        records.sort()
        temp.sort()
        return records, temp

    def recover_temp_files(self) -> int:
        """Remove leftover partial writes from a previous crash.

        ONLY files carrying this producer's own temp suffix are removed, and
        only after they are older than the age bound. AA-11 is explicit that
        unknown files are never deleted: this directory is on a shared volume,
        and a research component that deletes files it does not recognise is a
        worse problem than a full spool.
        """
        try:
            _records, temp = self._scan()
        except SpoolCapacityUnknown as exc:
            log.warning(f"[RESEARCH_SPOOL] temp recovery skipped: {exc}")
            return 0
        removed = 0
        cutoff = time.time() - self.max_age_s
        for name, _size, mtime in temp:
            if mtime >= cutoff:
                continue                     # possibly an in-flight write
            try:
                os.remove(os.path.join(self.directory, name))
                removed += 1
            except OSError:
                continue
        self.stats["temp_recovered"] += removed
        return removed

    def prune(self) -> int:
        """Age first, then count, then bytes. Oldest first in every pass."""
        records, _temp = self._scan()
        removed = 0
        cutoff = time.time() - self.max_age_s
        keep = []
        for name, size, mtime in records:
            if mtime < cutoff:
                if self._remove(name):
                    removed += 1
            else:
                keep.append((name, size, mtime))
        overflow = len(keep) - self.max_records
        for name, _size, _mtime in keep[:max(0, overflow)]:
            if self._remove(name):
                removed += 1
        keep = keep[max(0, overflow):]
        total = sum(size for _n, size, _m in keep)
        index = 0
        while total > self.max_bytes and index < len(keep):
            name, size, _mtime = keep[index]
            if self._remove(name):
                removed += 1
                total -= size
            index += 1
        self.stats["pruned"] += removed
        return removed

    def _remove(self, name) -> bool:
        try:
            os.remove(os.path.join(self.directory, name))
            return True
        except OSError:
            return False

    # ── the write itself ────────────────────────────────────────────────
    def write(self, record: dict) -> bool:
        """Durably append one record. Returns False when it was refused.

        Never raises for an expected condition: the caller is the writer
        thread, and its job is to keep draining the queue.
        """
        try:
            payload = json.dumps(record, sort_keys=True, ensure_ascii=False,
                                 allow_nan=False, indent=1).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.stats["write_errors"] += 1
            log.warning(f"[RESEARCH_SPOOL] record not serializable: {exc}")
            return False
        if len(payload) > self.max_record_bytes:
            self.stats["dropped_oversize"] += 1
            log.warning(f"[RESEARCH_SPOOL] record of {len(payload)} bytes "
                        f"exceeds the {self.max_record_bytes}-byte cap")
            return False
        try:
            os.makedirs(self.directory, exist_ok=True)
            self.prune()
            records, temp = self._scan()
        except SpoolCapacityUnknown as exc:
            # FAIL CLOSED. We do not know how full the spool is, so we do not
            # add to it.
            self.stats["capacity_unknown"] += 1
            log.warning(f"[RESEARCH_SPOOL] capacity unknown, refusing the "
                        f"write: {exc}")
            return False
        except OSError as exc:
            self.stats["capacity_unknown"] += 1
            log.warning(f"[RESEARCH_SPOOL] spool directory unusable: {exc}")
            return False
        temp_bytes = sum(size for _n, size, _m in temp)
        self.stats["temp_files"] = len(temp)
        self.stats["temp_bytes"] = temp_bytes
        used = sum(size for _n, size, _m in records) + temp_bytes
        if len(records) >= self.max_records or \
                used + len(payload) > self.max_bytes:
            self.stats["dropped_full"] += 1
            log.warning(
                f"[RESEARCH_SPOOL] spool full ({len(records)}/"
                f"{self.max_records} records, {used}/{self.max_bytes} bytes) "
                f"-- {record.get('contract_id')} NOT spooled")
            return False
        name = f"{str(record.get('emitted_at_utc', '')).replace(':', '')}-" \
               f"{str(record.get('record_sha256', ''))[:16]}{RECORD_SUFFIX}"
        path = os.path.join(self.directory, name)
        if os.path.exists(path):
            return False                    # identical observation, same second
        tmp = path + TEMP_SUFFIX
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError as exc:
            self.stats["write_errors"] += 1
            log.warning(f"[RESEARCH_SPOOL] write failed: {exc}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self.stats["written"] += 1
        return True


class ResearchWriter:
    """Bounded queue plus one isolated writer thread.

    `offer()` is what the engine calls. It is non-blocking by construction:
    `put_nowait` on a bounded queue either succeeds immediately or raises
    `queue.Full`, which is recorded as a DROP. Research work is the thing that
    gives way under pressure -- never the decision cycle.
    """

    def __init__(self, spool: BoundedSpool, *, max_queue=256,
                 max_queue_bytes=8 * 1024 * 1024, start=True):
        self.spool = spool
        self.max_queue = int(max_queue)
        self.max_queue_bytes = int(max_queue_bytes)
        self._queue = queue.Queue(maxsize=self.max_queue)
        self._queued_bytes = 0
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.stats = {"offered": 0, "queued": 0, "dropped_queue_full": 0,
                      "dropped_queue_bytes": 0, "drained": 0}
        if start:
            self.start()

    # ── engine side: must never block ───────────────────────────────────
    def offer(self, record: dict, *, approx_bytes: int = 0) -> bool:
        """Hand a record to the writer. Returns False when it was dropped.

        The return value exists for telemetry and tests. The engine ignores it:
        there is no action it could take on a dropped research record that
        would not couple the two paths.
        """
        self.stats["offered"] += 1
        size = int(approx_bytes or 0)
        # `self._lock` is held ONLY by offer() and by the writer's accounting,
        # never across a write or an fsync, so this can never wait on disk.
        with self._lock:
            if self._queued_bytes + size > self.max_queue_bytes:
                self.stats["dropped_queue_bytes"] += 1
                return False
            try:
                self._queue.put_nowait((record, size))
            except queue.Full:
                self.stats["dropped_queue_full"] += 1
                return False
            self._queued_bytes += size
            self.stats["queued"] += 1
        return True

    # ── writer side: owns every blocking operation ──────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="atlas-research-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._handle(item)

    def _handle(self, item) -> None:
        record, size = item
        try:
            self.spool.write(record)
        except Exception as exc:                              # noqa: BLE001
            # The writer thread must outlive any single bad record.
            log.warning(f"[RESEARCH_WRITER] record dropped: "
                        f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._queued_bytes = max(0, self._queued_bytes - size)
            self.stats["drained"] += 1
            self._queue.task_done()

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty. TESTS AND SHUTDOWN ONLY.

        Deliberately not called from the engine: waiting for the writer is the
        exact coupling AA-10 is about.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def telemetry(self) -> dict:
        with self._lock:
            queued_bytes = self._queued_bytes
        return {**self.stats, "queue_depth": self._queue.qsize(),
                "queued_bytes": queued_bytes, **self.spool.stats}
