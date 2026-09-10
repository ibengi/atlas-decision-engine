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
import contextlib
from strict_data import loads, dumps
from state_authority import (root_lock, durable_replace, fsync_dir, Transaction,
    active_transaction, recovery_problem, remember_file)

from config import CFG, _p

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
    "seen_fill_ids.json",
    "transport_intents.json",
    "recovery_receipts.json",
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
            data = loads(fh.read().decode())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        gen = data.get(key)
        if type(gen) is int and gen >= 0:
            return gen
    return None


def _fsync_dir(parent: str) -> None:
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
        # Explicit first-volume initialization proves empty collections together.
        # Existing economic files preclude this initialization path.
        names = {"kalshi_trades.json": [], "positions_state.json": {},
                 "orders_state.json": {}, "pending_intents.json": {},
                 "submission_guard.json": {}, "seen_fill_ids.json": []}
        if not any(os.path.exists(_p(name)) for name in names):
            try:
                with Transaction(marker, set(names) | {"state_epoch.json"}):
                    for name, value in names.items():
                        if not JsonStore.save(_p(name), value):
                            return False
            except Exception as exc:
                PersistenceSentinel.record_failure(marker, str(exc))
                return False
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
             generation_key: str = "generation", expect_fingerprint=None) -> bool:
        critical = os.path.basename(path) in CRITICAL_BASENAMES
        tx = None
        try:
            path = os.path.abspath(path)
            with root_lock(path):
                tx = active_transaction(path)
                scope = (Transaction(path, {os.path.basename(path)})
                         if critical and tx is None else contextlib.nullcontext(tx))
                with scope as tx:
                    if expect_generation is not None:
                        on_disk = read_generation(path, generation_key)
                        if tx and tx.stage_json:
                            on_disk = tx.staged_generations.get(path, on_disk)
                        if ((on_disk is None and (expect_generation != 0 or os.path.exists(path)))
                                or (on_disk is not None and on_disk != expect_generation)):
                            raise ValueError("STALE_INSTANCE: generation fence refused")
                        if isinstance(data, dict):
                            data = dict(data)
                            data[generation_key] = expect_generation + 1
                    if expect_fingerprint is not None and file_fingerprint(path) != expect_fingerprint:
                        raise ValueError("STALE_INSTANCE: content fence refused")
                    payload = dumps(data, indent=1, ensure_ascii=False).encode()
                    if os.path.basename(path) == "transport_intents.json":
                        from transport_intent import validate_collection_update
                        previous = {}
                        if os.path.isfile(path):
                            with open(path, "rb") as fh:
                                previous = loads(fh.read())
                        validate_collection_update(previous, data)
                    if os.path.basename(path) == "kalshi_trades.json" and os.path.isfile(path):
                        # A journal commit cannot erase or rewrite a completed
                        # economic event, even before the ledger has observed it.
                        # Corrections are new linked events, never replacements.
                        with open(path, "rb") as fh:
                            previous = loads(fh.read())
                        if not isinstance(previous, list) or not isinstance(data, list):
                            raise ValueError("journal must remain a collection")
                        completed = [row for row in previous if isinstance(row, dict)
                                     and (row.get("state") == "settled" or row.get("record_type") == "correction")]
                        successor = iter(data)
                        for row in completed:
                            if not any(candidate == row for candidate in successor):
                                raise ValueError("completed economic history is immutable")
                    if tx is not None and tx.stage_json:
                        tx.prepare_json(cls, path, loads(payload), expect_generation,
                                        generation_key, expect_fingerprint)
                        return True
                    if tx is not None:
                        tx.writing(path)
                    # Backups are diagnostic evidence, never permission to rewind.
                    if os.path.exists(path):
                        for i in range(CFG.BACKUPS - 1, 0, -1):
                            src, dst = f"{path}.bak{i}", f"{path}.bak{i+1}"
                            if os.path.exists(src):
                                shutil.copy2(src, dst)
                        shutil.copy2(path, f"{path}.bak1")
                    durable_replace(path, payload)
                    _fsync_dir(os.path.dirname(path))
                    durable_replace(path + ".sha256", cls._sha(payload).encode())
                    _fsync_dir(os.path.dirname(path))
                    with open(path, "rb") as fh:
                        if fh.read() != payload:
                            raise ValueError("durable payload readback mismatch")
                    if critical:
                        remember_file(path, payload)
                    cls.recovered_from_backup.pop(path, None)
            return True
        except Exception as exc:
            if tx is not None:
                tx.failed = True
            log.error(f"JsonStore.save({path}): {exc}")
            if critical:
                PersistenceSentinel.record_failure(path, str(exc))
            return False

    @classmethod
    def load(cls, path: str, default):
        critical = os.path.basename(path) in CRITICAL_BASENAMES
        issue = None if active_transaction(path) else recovery_problem(path)
        if critical and issue:
            PersistenceSentinel.record_failure(path, issue)
        candidates = [path] + [f"{path}.bak{i}" for i in range(1, CFG.BACKUPS + 1)]
        for cand in candidates:
            if not os.path.exists(cand):
                continue
            try:
                with open(cand, "rb") as fh:
                    raw = fh.read()
                data = loads(raw.decode())
                if cand == path and os.path.exists(path + ".sha256"):
                    with open(path + ".sha256") as fh:
                        want = fh.read().strip()
                    if want and want != cls._sha(raw):
                        log.warning(f"JsonStore: checksum invalide pour {path} "
                                    f"-- tentative sur backup.")
                        continue
                if cand != path:
                    if critical:
                        PersistenceSentinel.record_failure(path, "RECOVERY_REQUIRED: backup is not current authority")
                    log.warning(f"JsonStore: {path} corrompu/absent -- "
                                f"recupere depuis {cand}.")
                    cls.recovered_from_backup[os.path.abspath(path)] = cand
                else:
                    cls.recovered_from_backup.pop(os.path.abspath(path), None)
                return data
            except Exception:
                continue
        if critical and (os.path.exists(path) or os.path.exists(path + ".sha256")):
            PersistenceSentinel.record_failure(path, "RECOVERY_REQUIRED: unreadable state")
        return default
