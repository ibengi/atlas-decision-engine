"""Atomic JSON persistence layer with checksums and backup rotation."""

import os
import json
import hashlib
import shutil
import logging

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

    @classmethod
    def save(cls, path: str, data) -> bool:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            payload = json.dumps(data, indent=1, ensure_ascii=False).encode()
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            # rotation des backups AVANT remplacement
            if os.path.exists(path):
                for i in range(CFG.BACKUPS - 1, 0, -1):
                    src, dst = f"{path}.bak{i}", f"{path}.bak{i+1}"
                    if os.path.exists(src): shutil.copy2(src, dst)
                shutil.copy2(path, f"{path}.bak1")
            os.replace(tmp, path)
            sha_tmp = path + ".sha256.tmp"
            with open(sha_tmp, "w", encoding="utf-8") as f:
                f.write(cls._sha(payload)); f.flush(); os.fsync(f.fileno())
            os.replace(sha_tmp, path + ".sha256")
            return True
        except Exception as e:
            log.error(f"JsonStore.save({path}): {e}")
            if os.path.basename(path) in CRITICAL_BASENAMES:
                PersistenceSentinel.record_failure(path, str(e))
            return False

    @classmethod
    def load(cls, path: str, default):
        candidates = [path] + [f"{path}.bak{i}" for i in range(1, CFG.BACKUPS + 1)]
        for cand in candidates:
            if not os.path.exists(cand):
                continue
            try:
                raw = open(cand, "rb").read()
                data = json.loads(raw.decode())
                if cand == path and os.path.exists(path + ".sha256"):
                    want = open(path + ".sha256").read().strip()
                    if want and want != cls._sha(raw):
                        log.warning(f"JsonStore: checksum invalide pour {path} "
                                    f"-- tentative sur backup.")
                        continue
                if cand != path:
                    log.warning(f"JsonStore: {path} corrompu/absent -- "
                                f"recupere depuis {cand}.")
                return data
            except Exception:
                continue
        return default
