"""Atomic JSON persistence layer with checksums, fencing and backups.

Crash semantics (audit finding A01, A09):

  * ``save()`` writes ``path.tmp``, fsyncs it, rotates backups, then
    ``os.replace``s it over ``path`` and fsyncs the directory. After the
    directory fsync the new content is durable; before it, a crash
    leaves the OLD content, never a torn one.
  * The ``.sha256`` sidecar is written AFTER the replace. A crash in
    that window leaves new data with a stale checksum, which ``load()``
    used to answer by silently falling back to an OLDER backup -- a
    rewind dressed as a repair. It now reports the fallback so
    continuity-critical callers can re-verify against the continuity
    chain instead of believing the older bytes (see continuity.py).
  * ``expect_generation`` fences concurrent writers: a process that
    loaded generation N refuses to overwrite a file that has since
    moved to N+1. An old writer can no longer clobber newer state.
"""

import os
import json
import hashlib
import shutil
import logging

from config import CFG, _p
from state_tx import (FenceError, NonFiniteValue, all_finite, durable_write_all,
                      fence_for, fsync_dir)

log = logging.getLogger("PERSISTENCE")

#: State files whose loss or non-persistence disarms a financial safety
#: mechanism. A failed write to any of these trips the sentinel below and
#: the engine stops submitting orders fail-closed. Observability files
#: (dashboards, reports, curves) are deliberately absent: their loss is
#: annoying, not dangerous.
CRITICAL_BASENAMES = frozenset({
    "submission_guard.json",   # anti-duplicate lock (2026-07-25 incident)
    "orders_state.json",       # in-flight orders
    "kalshi_trades.json",      # journal = source of ALL risk history
    "risk_state.json",         # half-open circuit-breaker claim
    "positions_state.json",    # open positions / slot accounting
    "state_epoch.json",        # persistent-state continuity marker
    "equity_ledger.json",      # F2 risk-equity baseline, flows, HWM, rebases
    "pending_intents.json",    # A09: the proof a submission was intended
})


class PersistenceSentinel:
    """Latch that records the FIRST critical persistence failure.

    JsonStore.save() historically failed soft (ERROR log + False) and no
    caller checked the result: on a read-only or full disk the engine kept
    trading while silently persisting nothing — strictly worse than having
    no disk, because every restart guarantee (dedup guard, risk history)
    was believed to hold when it did not. The latch never un-trips at
    runtime: only a process restart with a healthy disk (or reset() in
    tests) clears it, because state written after the first failure may
    already be inconsistent.
    """

    _failure = None   # {"path": str, "reason": str}

    @classmethod
    def record_failure(cls, path: str, reason: str) -> None:
        if cls._failure is None:
            cls._failure = {"path": path, "reason": reason}
            log.critical(
                f"[PERSISTENCE_HALT] ecriture critique impossible: {path} "
                f"({reason}) -- soumissions d'ordres BLOQUEES fail-closed "
                f"jusqu'a redemarrage sur un disque sain.")

    @classmethod
    def healthy(cls) -> bool:
        return cls._failure is None

    @classmethod
    def failure(cls):
        return cls._failure

    @classmethod
    def reset(cls) -> None:
        """Tests only."""
        cls._failure = None


def file_fingerprint(path: str) -> str:
    """Content fingerprint used to bind a decision to the exact bytes it was
    taken on (audit finding A03). ``absent`` is a value like any other: a
    file that disappears between validation and commit is a change."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "absent"


def read_generation(path: str, key: str = "generation"):
    """The generation stamped inside a persisted object, or None. Reads the
    file directly: the point of a fence is to see what is on disk NOW, not
    what this process remembers."""
    try:
        with open(path, "rb") as fh:
            data = strict_loads(fh.read())
    except (OSError, ValueError, NonFiniteValue):
        return None
    if isinstance(data, dict):
        gen = data.get(key)
        if isinstance(gen, int) and gen >= 0:
            return gen
    return None


def _reject_constant(name):
    """json.loads accepts NaN/Infinity/-Infinity by default. Persisted state
    is economic state: a non-finite value read back as a float propagates
    into every comparison, and NaN compares False against every threshold
    (audit finding A16). Parsing refuses it instead."""
    raise NonFiniteValue(f"persisted state contains the non-finite literal "
                         f"{name!r}")


def strict_loads(raw):
    """json.loads that refuses non-finite literals and duplicate keys.

    A duplicate key is not a formatting quirk: last-wins silently discards
    the first value, so a file can say two contradictory things and be read
    as the more convenient one (audit finding A08)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return json.loads(raw, parse_constant=_reject_constant,
                      object_pairs_hook=_no_duplicate_keys)


def _no_duplicate_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key {k!r}: the document states "
                             f"two values for one field")
        seen[k] = v
    return seen


def strict_dumps(data) -> bytes:
    """json.dumps that refuses to serialise a non-finite number.

    ``allow_nan=True`` (the default) emits the literals ``NaN``/``Infinity``,
    which are not JSON and which this module's own loader would then refuse
    -- writing a file only readable as an error. Refusing at write time keeps
    the non-finite value out of durable state entirely (A16)."""
    return json.dumps(data, indent=1, ensure_ascii=False,
                      allow_nan=False).encode()


def _fsync_dir(parent: str) -> None:
    """Directory fsync. Raises OSError on a real failure: a rename that was
    never made durable is not a completed write (audit finding A18)."""
    fsync_dir(parent)


def verify_state_root() -> bool:
    """Boot-time continuity check for LIVE-capable deployments.

    With REQUIRE_PERSISTENT_STATE=true a missing state marker means the
    disk is fresh or was wiped: every restart guarantee (dedup guard, risk
    history, positions) is silently void, so trading must NOT resume as if
    healthy. ALLOW_FRESH_STATE=true is the explicit one-time operator
    acknowledgement that an empty state directory is intentional (first
    deployment onto a new volume). Default config leaves both flags off,
    preserving today's DEMO behaviour exactly.
    """
    if not CFG.REQUIRE_PERSISTENT_STATE:
        return True
    marker = _p("state_epoch.json")
    if os.path.exists(marker):
        data = JsonStore.load(marker, {})
        data["last_boot_probe"] = data.get("last_boot_probe", 0) + 1
        if not JsonStore.save(marker, data):
            # save() already tripped the sentinel (critical basename), but
            # be explicit in case the failure path changes.
            PersistenceSentinel.record_failure(
                marker, "state marker present but not writable")
            return False
        return True
    if CFG.ALLOW_FRESH_STATE:
        import datetime
        ok = JsonStore.save(marker, {
            "initialized_at":
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_boot_probe": 0,
        })
        if not ok:
            PersistenceSentinel.record_failure(
                marker, "fresh state marker could not be written")
        return ok
    PersistenceSentinel.record_failure(
        marker,
        "REQUIRE_PERSISTENT_STATE=true mais state_epoch.json absent "
        "(disque neuf ou EFFACE) et ALLOW_FRESH_STATE non positionne")
    return False


class JsonStore:
    """Ecriture atomique, checksum sha256, rotation de sauvegardes,
    et lecture avec reprise automatique sur backup en cas de corruption."""

    @staticmethod
    def _sha(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    #: Set by load() when the primary file was unusable and an OLDER backup
    #: answered instead. Keyed by path. A rewind that repairs a checksum
    #: crash is still a rewind: continuity-critical readers must re-verify
    #: against an authority the backup could not rewind (continuity.py).
    recovered_from_backup = {}

    @classmethod
    def save(cls, path: str, data, expect_generation=None,
             generation_key: str = "generation") -> bool:
        """Fenced, atomic, checksummed, fully-durable write.

        The whole check-and-set runs while holding the writer fence for this
        path (audit finding A13). Reading the generation and replacing the
        file used to be two independent statements: two processes that had
        both loaded generation N each read N, each found it equal to what
        they held, and each committed N+1 -- one of them silently destroying
        the other's state. Under the fence the second process cannot read
        until the first has published, so it sees N+1 and is refused.

        A refusal mutates nothing: no temp file is promoted, no backup is
        rotated, and the caller's in-memory state is untouched (A15).
        """
        fence = None
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            # A16: refuse non-finite economic values before anything is
            # written or rotated. Serialising them would emit the non-JSON
            # literals NaN/Infinity that this module's loader then refuses.
            payload = strict_dumps(data if expect_generation is None
                                   else data)
        except (TypeError, ValueError, NonFiniteValue) as e:
            log.error(f"JsonStore.save({path}): refusing non-finite or "
                      f"unserialisable state: {e}")
            if os.path.basename(path) in CRITICAL_BASENAMES:
                PersistenceSentinel.record_failure(path, f"non-finite state: {e}")
            return False
        except OSError as e:
            log.error(f"JsonStore.save({path}): {e}")
            if os.path.basename(path) in CRITICAL_BASENAMES:
                PersistenceSentinel.record_failure(path, str(e))
            return False

        try:
            fence = fence_for(path)
            fence.acquire()
        except FenceError as e:
            # Not being able to fence is not a reason to write unfenced.
            log.critical(f"[FENCE_UNAVAILABLE] {path}: {e} -- write REFUSED")
            if os.path.basename(path) in CRITICAL_BASENAMES:
                PersistenceSentinel.record_failure(path, f"writer fence: {e}")
            return False

        try:
            if expect_generation is not None:
                on_disk = read_generation(path, generation_key)
                if on_disk is not None and int(on_disk) != int(expect_generation):
                    log.critical(
                        f"[FENCE_REFUSED] {path}: generation on disk "
                        f"{on_disk} != {expect_generation} held by this "
                        f"writer -- write REFUSED (stale writer).")
                    return False
                if isinstance(data, dict):
                    data = dict(data)
                    data[generation_key] = int(expect_generation) + 1
                payload = strict_dumps(data)
            tmp = path + ".tmp"
            # A18: "written" means every intended byte is durable, not that
            # a write call returned. The buffered writer raises rather than
            # returning short, os.fsync makes it durable, and the size is
            # then VERIFIED against the payload: a filesystem that accepted
            # fewer bytes (quota, ENOSPC on flush, a truncating layer) is
            # caught here instead of promoting a truncated temp file.
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            written = os.path.getsize(tmp)
            if written != len(payload):
                raise OSError(f"short write: {written}/{len(payload)} bytes "
                              f"durable in {tmp}")
            # rotation des backups AVANT remplacement
            if os.path.exists(path):
                for i in range(CFG.BACKUPS - 1, 0, -1):
                    src_b, dst_b = f"{path}.bak{i}", f"{path}.bak{i+1}"
                    if os.path.exists(src_b): shutil.copy2(src_b, dst_b)
                shutil.copy2(path, f"{path}.bak1")
            os.replace(tmp, path)
            # A18: a rename nobody made durable is a rename a crash undoes.
            # A failing directory fsync is reported, not ignored.
            _fsync_dir(parent)
            sha_tmp = path + ".sha256.tmp"
            digest = cls._sha(payload).encode()
            with open(sha_tmp, "wb") as f:
                f.write(digest)
                f.flush()
                os.fsync(f.fileno())
            if os.path.getsize(sha_tmp) != len(digest):
                raise OSError(f"short write: checksum sidecar {sha_tmp} "
                              f"is truncated")
            os.replace(sha_tmp, path + ".sha256")
            _fsync_dir(parent)
            cls.recovered_from_backup.pop(os.path.abspath(path), None)
            return True
        except Exception as e:
            log.error(f"JsonStore.save({path}): {e}")
            if os.path.basename(path) in CRITICAL_BASENAMES:
                PersistenceSentinel.record_failure(path, str(e))
            return False
        finally:
            if fence is not None:
                try:
                    fence.release()
                except FenceError:
                    pass

    @classmethod
    def load_reporting(cls, path: str, default):
        """``(value, problem)``: like ``load`` but says WHY it answered.

        Audit finding A17. ``load`` returns the caller's default for every
        outcome -- file absent, unreadable, unparsable, every backup bad --
        so "there are no pending intents" and "I cannot tell you whether
        there are pending intents" arrive as the same empty dict. On an
        execution-capable path those two must never be the same answer.

        ``problem`` is None when the file was genuinely absent or read
        cleanly, and a sentence otherwise.
        """
        exists = os.path.exists(path) or any(
            os.path.exists(f"{path}.bak{i}") for i in range(1, CFG.BACKUPS + 1))
        if not exists:
            return default, None
        errors = []
        candidates = [path] + [f"{path}.bak{i}" for i in range(1, CFG.BACKUPS + 1)]
        for cand in candidates:
            if not os.path.exists(cand):
                continue
            try:
                raw = open(cand, "rb").read()
                data = strict_loads(raw)
            except Exception as e:                          # noqa: BLE001
                errors.append(f"{os.path.basename(cand)}: {e}")
                continue
            if cand == path and os.path.exists(path + ".sha256"):
                try:
                    want = open(path + ".sha256").read().strip()
                except OSError as e:
                    errors.append(f"{os.path.basename(cand)}.sha256: {e}")
                    want = ""
                if want and want != cls._sha(raw):
                    errors.append(f"{os.path.basename(cand)}: checksum mismatch")
                    continue
            if cand != path:
                cls.recovered_from_backup[os.path.abspath(path)] = cand
                return data, (f"primary unreadable, answered from {cand} "
                              f"({'; '.join(errors)})")
            cls.recovered_from_backup.pop(os.path.abspath(path), None)
            return data, None
        return default, (f"present but unreadable ({'; '.join(errors) or 'no candidate parsed'})")

    @classmethod
    def load(cls, path: str, default):
        candidates = [path] + [f"{path}.bak{i}" for i in range(1, CFG.BACKUPS + 1)]
        for cand in candidates:
            if not os.path.exists(cand):
                continue
            try:
                raw = open(cand, "rb").read()
                data = strict_loads(raw)
                if cand == path and os.path.exists(path + ".sha256"):
                    want = open(path + ".sha256").read().strip()
                    if want and want != cls._sha(raw):
                        log.warning(f"JsonStore: checksum invalide pour {path} "
                                    f"-- tentative sur backup.")
                        continue
                if cand != path:
                    log.warning(f"JsonStore: {path} corrompu/absent -- "
                                f"recupere depuis {cand}.")
                    cls.recovered_from_backup[os.path.abspath(path)] = cand
                else:
                    cls.recovered_from_backup.pop(os.path.abspath(path), None)
                return data
            except Exception:
                continue
        return default
