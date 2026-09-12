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
import re
import stat
import threading
import time

from durable_append import (DurabilityUnknown, exclusive_lock,
                            fsync_directory)

log = logging.getLogger("RESEARCH_FEED")

#: Suffix for records still being written. Named so that startup cleanup can
#: recognise files this producer owns and NEVER touch anything else (AA-11).
TEMP_SUFFIX = ".partial"
RECORD_SUFFIX = ".json"

#: AA-11 (re-audit). A partial carries the PID of the process writing it, so
#: recovery can tell three cases apart that the old age-only rule could not:
#:
#:   ours, owner dead     -> a crashed write. Reclaim it NOW.
#:   ours, owner alive    -> a write in flight. Leave it; it holds a slot.
#:   not ours at all      -> somebody else's file on a shared volume. Never
#:                           touched, at any age, for any reason.
#:
#: Waiting for `max_age_s` (six hours by default) before reclaiming the first
#: case meant a crash kept its slots through the window in which the next
#: crash happens.
OWNED_TEMP_RE = re.compile(r"\.(\d+)" + re.escape(TEMP_SUFFIX) + r"$")

#: The sidecar the capacity reservation is serialized on, held across
#: scan -> decide -> write so two writers cannot both spend the last slot.
#:
#: It lives INSIDE the spool directory, on purpose. "The producer writes only
#: under its own spool directory, never anywhere else on the shared volume"
#: is the AA-10/AA-11 containment property, asserted by
#: `test_it_writes_only_inside_its_own_directory`, and a lock file placed
#: beside the directory would have broken it to make a different test pass.
#: It carries neither suffix the spool recognises, so `_scan` classifies it
#: as neither a record nor a partial and it is never counted, pruned or
#: recovered.
RESERVATION_LOCK = ".capacity"


def owner_is_alive(pid: int) -> bool:
    """Is `pid` a live process on THIS host?

    Answers conservatively: anything we cannot establish reads as ALIVE, so
    an uncertain partial is kept rather than deleted. Reclaiming a slot is
    worth less than destroying a write that is still happening.
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # exists, owned by someone else
    except OSError:
        return True
    return True


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
                      "temp_recovered": 0, "reservation_timeouts": 0}
        self._sequence = 0
        # AA-11 (re-audit): recovery happens HERE, at construction, rather
        # than in a method nothing called. A producer that starts with its
        # own crashed partials still occupying the budget has inherited the
        # previous run's failure as a smaller spool.
        try:
            self.recover_owned_partials()
        except Exception as exc:                              # noqa: BLE001
            # Startup recovery is an optimisation of capacity, never a
            # precondition for running. A spool that cannot be recovered is
            # still a spool that fails closed when it is full.
            log.warning(f"[RESEARCH_SPOOL] startup partial recovery failed: "
                        f"{type(exc).__name__}: {exc}")

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
            # RA-04 -- ONE OBSERVATION, NOT TWO.
            #
            # This used to ask `os.path.isfile(path)` here, which stats the
            # SAME path a second time. Two ways that went wrong, both of them
            # in the unsafe direction:
            #
            #   * `os.path.isfile` swallows every `OSError` and returns False.
            #     So the one case the stat above is careful to fail closed on
            #     -- metadata we cannot read -- silently became "not a file,
            #     do not count it" whenever it happened on the second look.
            #   * between the two calls the entry can change. A record the
            #     first stat counted could be absent from the second, and it
            #     then occupied the volume while being invisible to every
            #     budget meant to bound it.
            #
            # The mode is already in hand. It is used, and an entry carrying
            # one of OUR suffixes that is not a regular file is metadata this
            # class cannot account for, so capacity FAILS CLOSED rather than
            # skipping it.
            ours = name.endswith(TEMP_SUFFIX) or name.endswith(RECORD_SUFFIX)
            if not stat.S_ISREG(st.st_mode):
                if ours:
                    raise SpoolCapacityUnknown(
                        f"{name} carries a spool suffix but is not a regular "
                        f"file (mode {st.st_mode:#o}); how much of the budget "
                        f"it occupies cannot be established")
                continue
            entry = (name, st.st_size, st.st_mtime)
            if name.endswith(TEMP_SUFFIX):
                temp.append(entry)
            elif name.endswith(RECORD_SUFFIX):
                records.append(entry)
        records.sort()
        temp.sort()
        return records, temp

    def capacity(self) -> dict:
        """What the directory actually holds, in every unit that bounds it.

        AA-11 (re-audit): the old check asked `len(records) >= max_records`
        and counted COMPLETE files only, while partials were counted in the
        byte budget alone. So N interrupted writes let the spool hold
        `max_records + N` files -- the bound developed a hole at exactly the
        moment writes were being interrupted. `occupied` is the number this
        class bounds: complete records PLUS partials, because a partial is a
        file on the volume whether or not it ever becomes a record.
        """
        records, temp = self._scan()
        record_bytes = sum(size for _n, size, _m in records)
        partial_bytes = sum(size for _n, size, _m in temp)
        return {"records": len(records), "partials": len(temp),
                "occupied": len(records) + len(temp),
                "record_bytes": record_bytes, "partial_bytes": partial_bytes,
                "used_bytes": record_bytes + partial_bytes}

    def recover_owned_partials(self) -> int:
        """Reclaim slots held by partials whose owner is gone (AA-11).

        Called at construction, so a restart begins with an accurate picture
        of its own capacity instead of one inflated by the crash that caused
        the restart.

        The three-way test is in `OWNED_TEMP_RE` and `owner_is_alive`: a file
        must carry OUR temp suffix AND a PID that is no longer running. A
        file we cannot parse as ours is never removed here at any age -- this
        directory sits on a volume the engine shares, and a research
        component that deletes unrecognised files is a worse failure than a
        full spool.
        """
        try:
            _records, temp = self._scan()
        except SpoolCapacityUnknown as exc:
            log.warning(f"[RESEARCH_SPOOL] partial recovery skipped: {exc}")
            return 0
        removed = 0
        for name, _size, _mtime in temp:
            match = OWNED_TEMP_RE.search(name)
            if match is None:
                continue                       # not ours; not ours to remove
            if owner_is_alive(int(match.group(1))):
                continue                       # a write still in flight
            if self._remove(name):
                removed += 1
        self.stats["temp_recovered"] += removed
        if removed:
            log.info(f"[RESEARCH_SPOOL] reclaimed {removed} partial write(s) "
                     f"left by a crashed producer")
        return removed

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
        except OSError as exc:
            self.stats["capacity_unknown"] += 1
            log.warning(f"[RESEARCH_SPOOL] spool directory unusable: {exc}")
            return False

        # AA-11 (re-audit) -- CAPACITY IS RESERVED, NOT MERELY CHECKED.
        #
        # `scan -> decide -> create` was three steps with nothing between
        # them. Two writers both read "one slot free" and both took it, and
        # the bound was exceeded by exactly the number of writers racing. A
        # check whose result can go stale before it is acted on is not a
        # bound, it is an observation.
        #
        # The whole sequence -- prune, scan, decide, create, write, fsync,
        # rename -- happens under one advisory lock, so the slot a writer
        # counts is the slot it takes. Holding the lock across the fsync is
        # deliberate and it is safe HERE and only here: this runs on the
        # research writer thread. AA-10 is what makes that true -- the engine
        # hands its record to a queue and never waits for this function, so
        # serializing writers costs research latency and nothing else.
        #
        # `exclusive_lock` is a generator-based context manager: calling it
        # runs nothing, so every failure it can have -- opening the sidecar,
        # acquiring the lock, timing out -- surfaces at `__enter__`, inside
        # the `with`. Catching around the call would have been dead code.
        try:
            with exclusive_lock(os.path.join(self.directory,
                                             RESERVATION_LOCK), timeout=5.0):
                return self._reserve_and_write(record, payload)
        except TimeoutError:
            # Another writer is holding the spool. Refuse rather than queue
            # behind it: research is the thing that gives way.
            self.stats["reservation_timeouts"] += 1
            log.warning("[RESEARCH_SPOOL] another writer holds the spool; "
                        "this record is NOT spooled")
            return False
        except OSError as exc:
            # An unopenable lock file is an unknown capacity, and unknown
            # capacity fails closed like any other.
            self.stats["capacity_unknown"] += 1
            log.warning(f"[RESEARCH_SPOOL] cannot take the reservation "
                        f"lock: {exc}")
            return False

    def _reserve_and_write(self, record: dict, payload: bytes) -> bool:
        """Under the reservation lock: decide, take the slot, write it."""
        try:
            self.prune()
            capacity = self.capacity()
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
        self.stats["temp_files"] = capacity["partials"]
        self.stats["temp_bytes"] = capacity["partial_bytes"]
        # AA-11: COMPLETE files, PARTIAL files and BYTES, all three.
        if capacity["occupied"] >= self.max_records or \
                capacity["used_bytes"] + len(payload) > self.max_bytes:
            self.stats["dropped_full"] += 1
            log.warning(
                f"[RESEARCH_SPOOL] spool full ({capacity['records']} records "
                f"+ {capacity['partials']} partial(s) against "
                f"{self.max_records}, {capacity['used_bytes']}/"
                f"{self.max_bytes} bytes) -- {record.get('contract_id')} NOT "
                f"spooled")
            return False
        name = f"{str(record.get('emitted_at_utc', '')).replace(':', '')}-" \
               f"{str(record.get('record_sha256', ''))[:16]}{RECORD_SUFFIX}"
        path = os.path.join(self.directory, name)
        if os.path.exists(path):
            return False                    # identical observation, same second
        # The temp name carries OUR pid, so a crash here leaves a partial that
        # the next startup can recognise as ours and reclaim immediately.
        self._sequence += 1
        tmp = f"{path}.{os.getpid()}{TEMP_SUFFIX}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            self._fsync_directory()
        except (OSError, DurabilityUnknown) as exc:
            self.stats["write_errors"] += 1
            log.warning(f"[RESEARCH_SPOOL] write failed: {exc}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self.stats["written"] += 1
        return True

    def _fsync_directory(self) -> None:
        """Make the RENAME durable, not just the bytes.

        An fsync on the file persists its contents; the directory entry that
        gives those contents a name is a separate write. Without this a crash
        can leave a spool whose records are on the platter under no name at
        all -- which reads, on restart, as evidence that was never produced.

        RA-05: this used to swallow both the open failure and the fsync
        failure, so a record whose NAME never reached the device was counted
        as written. It now delegates to `durable_append.fsync_directory`,
        which raises `DurabilityUnknown`; `_reserve_and_write` catches it and
        reports the write as FAILED, which is what "we cannot prove this is
        durable" has to mean everywhere in this subsystem.
        """
        fsync_directory(self.directory)


#: Queue item kinds. A diagnostic travels the SAME bounded queue as a record,
#: so the engine thread has exactly one non-blocking hand-off to make and one
#: pressure policy to obey.
ITEM_RECORD = "record"
ITEM_NOTE = "note"

#: RA-03. An ADMITTED candidate: type-checked by the observer, not yet
#: assembled, hashed or validated. The finalizer runs on THIS thread.
ITEM_CANDIDATE = "candidate"


class ResearchWriter:
    """Bounded queue plus one isolated writer thread.

    `offer()` is what the engine calls. It is non-blocking by construction:
    `put_nowait` on a bounded queue either succeeds immediately or raises
    `queue.Full`, which is recorded as a DROP. Research work is the thing that
    gives way under pressure -- never the decision cycle.

    AA-10 (re-audit) -- DIAGNOSTICS TRAVEL THE SAME WAY AS DATA
        The first remediation moved `write`, `fsync` and `prune` onto this
        thread and left `log.info`/`log.warning` on the caller's. That is not
        a smaller version of the same problem, it is the same problem:
        `logging.Handler.emit` takes a lock and writes synchronously, and the
        research logger's handler writes to the very volume the fsync was
        moved off. A stalled volume therefore still stalled the decision
        cycle -- through the diagnostics instead of through the data.

        So `note()` is the only way the producer says anything, it is
        `put_nowait` on this same bounded queue, and the `log` call happens on
        the writer thread. Under pressure a diagnostic is DROPPED, counted,
        and the drop itself is reported later -- the engine never waits to be
        told something.
    """

    def __init__(self, spool: BoundedSpool, *, max_queue=256,
                 max_queue_bytes=8 * 1024 * 1024, start=True):
        self.spool = spool
        #: RA-03. Called ON THIS THREAD to turn an admitted candidate into a
        #: spool record: JSON serialization, sha256 and the full contract
        #: walk, none of which the engine's observer may pay for. `None`
        #: means nobody wired one, and an admitted candidate is then DROPPED
        #: rather than written unvalidated.
        self.finalizer = None
        self.max_queue = int(max_queue)
        self.max_queue_bytes = int(max_queue_bytes)
        self._queue = queue.Queue(maxsize=self.max_queue)
        self._queued_bytes = 0
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.stats = {"offered": 0, "queued": 0, "dropped_queue_full": 0,
                      "dropped_queue_bytes": 0, "drained": 0,
                      # AA-10: diagnostics that never reached the writer.
                      # Counted rather than logged, because logging a dropped
                      # log on the engine thread would be the original bug.
                      "notes": 0, "dropped_notes": 0,
                      # RA-03: admitted candidates the contract refused after
                      # the writer finalized them, and ones that could not be
                      # finalized at all because no finalizer was wired.
                      "finalized": 0, "refused_by_contract": 0,
                      "unfinalizable": 0}
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
                self._queue.put_nowait((ITEM_RECORD, record, size))
            except queue.Full:
                self.stats["dropped_queue_full"] += 1
                return False
            self._queued_bytes += size
            self.stats["queued"] += 1
        return True

    def offer_candidate(self, candidate: dict, *,
                        approx_bytes: int = 0) -> bool:
        """Hand an ADMITTED candidate to the writer (RA-03).

        Same bound, same drop policy and the same non-blocking guarantee as
        `offer`; the only difference is how much work has been done to the
        payload before it got here, and that is the entire point.
        """
        self.stats["offered"] += 1
        size = int(approx_bytes or 0)
        with self._lock:
            if self._queued_bytes + size > self.max_queue_bytes:
                self.stats["dropped_queue_bytes"] += 1
                return False
            try:
                self._queue.put_nowait((ITEM_CANDIDATE, candidate, size))
            except queue.Full:
                self.stats["dropped_queue_full"] += 1
                return False
            self._queued_bytes += size
            self.stats["queued"] += 1
        return True

    def note(self, level: int, message: str) -> bool:
        """Say something, later, on the writer's thread (AA-10).

        NEVER logs here. The message is a plain string built by the caller;
        formatting it is cheap and in memory, emitting it is not, and only the
        second one is allowed to touch a device the engine shares.

        A dropped note is counted, never retried and never escalated: a
        research subsystem that will not stay quiet under pressure is a
        research subsystem that can stall the money path to be heard.
        """
        size = len(message) + 64
        with self._lock:
            if self._queued_bytes + size > self.max_queue_bytes:
                self.stats["dropped_notes"] += 1
                return False
            try:
                self._queue.put_nowait((ITEM_NOTE, (int(level), message), size))
            except queue.Full:
                self.stats["dropped_notes"] += 1
                return False
            self._queued_bytes += size
            self.stats["notes"] += 1
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
        kind, payload, size = item
        try:
            if kind == ITEM_NOTE:
                # AA-10: the ONLY place the producer's diagnostics are
                # emitted. On the writer's thread, where a slow handler costs
                # research latency and nothing else.
                level, message = payload
                log.log(level, message)
            elif kind == ITEM_CANDIDATE:
                # RA-03: serialization, hashing, contract validation and the
                # formatting of every diagnostic all happen HERE.
                if self.finalizer is None:
                    self.stats["unfinalizable"] += 1
                    log.warning("[RESEARCH_WRITER] an admitted candidate "
                                "arrived with no finalizer wired; it is "
                                "DROPPED rather than spooled unvalidated")
                else:
                    record = self.finalizer(payload)
                    self.stats["finalized"] += 1
                    if record is None:
                        self.stats["refused_by_contract"] += 1
                    else:
                        self.spool.write(record)
            else:
                self.spool.write(payload)
        except Exception as exc:                              # noqa: BLE001
            # The writer thread must outlive any single bad record.
            log.warning(f"[RESEARCH_WRITER] {kind} dropped: "
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
