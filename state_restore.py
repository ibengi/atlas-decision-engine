"""Zero-loss state restore for the persistence cutover (Phase 2B).

The migration destination (fresh persistent volume) must start from the
byte-exact Phase-2A golden backup, never from an empty ledger. The only
write path into the running container is the deployment itself, so the
backup rides in as operator-controlled environment variables:

  RESTORE_STATE_TGZ_B64  base64 of a tar.gz holding the five critical
                         state files at the archive root; may be split
                         into RESTORE_STATE_TGZ_B64 + _2, _3, ... chunks
                         (concatenated in suffix order) because some
                         provisioning paths mangle long values
  RESTORE_STATE_SHA256   JSON object {filename: 64-hex sha256} for the
                         exact bytes of each file

Whitespace inside the base64 and missing trailing padding are repaired
before decoding: neither carries data, and the per-file SHA-256 manifest
-- not base64 well-formedness -- is the integrity authority. Any real
corruption still fails the hash check and writes nothing.

No runtime state lives in git; code only. Semantics are fail-closed:

  - env vars absent          -> no-op (normal boot)
  - all five already present -> no-op (idempotent across redeploys)
  - partial state present    -> sentinel trips, nothing written
  - hash/name mismatch       -> sentinel trips, nothing written
  - restore success          -> files + .sha256 sidecars written
                                atomically, then state_epoch.json is
                                created so verify_state_root() accepts
                                the volume as continuous

A tripped sentinel blocks every order submission, so a failed restore can
never trade on a wrong ledger.
"""

import base64
import datetime
import hashlib
import io
import json
import logging
import os
import tarfile

from config import _p
from persistence import JsonStore, PersistenceSentinel

log = logging.getLogger("PERSISTENCE")

#: The five source-critical files of the Phase-2A backup. state_epoch.json
#: is destination metadata created here, never migrated.
RESTORE_BASENAMES = (
    "submission_guard.json",
    "orders_state.json",
    "kalshi_trades.json",
    "risk_state.json",
    "positions_state.json",
)


def _fail(reason: str) -> bool:
    PersistenceSentinel.record_failure("state_restore", reason)
    log.critical(f"[STATE_RESTORE] ECHEC fail-closed: {reason}")
    return False


def _atomic_write(path: str, payload: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_b64_env() -> str:
    parts = [os.getenv("RESTORE_STATE_TGZ_B64", "")]
    i = 2
    while True:
        part = os.getenv(f"RESTORE_STATE_TGZ_B64_{i}")
        if part is None:
            break
        parts.append(part)
        i += 1
    b64 = "".join("".join(p.split()) for p in parts)
    return b64 + "=" * (-len(b64) % 4) if b64 else ""


def maybe_restore_state() -> bool:
    """Runs before any manager touches state. True = state usable
    (restored, already present, or restore not requested)."""
    b64 = _read_b64_env()
    if not b64:
        return True

    present = [n for n in RESTORE_BASENAMES if os.path.exists(_p(n))]
    if len(present) == len(RESTORE_BASENAMES):
        log.info("[STATE_RESTORE] etat deja present (5/5) -- restore ignore.")
        return True
    if present:
        return _fail(
            f"etat PARTIEL sur le volume ({sorted(present)}): ni vierge ni "
            f"complet -- aucun fichier ne sera ecrit ni ecrase")

    try:
        manifest = json.loads(os.getenv("RESTORE_STATE_SHA256", ""))
        assert isinstance(manifest, dict)
    except Exception:
        return _fail("RESTORE_STATE_SHA256 absent ou illisible: la "
                     "restauration sans verification de hash est interdite")
    if sorted(manifest) != sorted(RESTORE_BASENAMES):
        return _fail(f"manifest de hash incomplet/inattendu: "
                     f"{sorted(manifest)}")

    try:
        raw = base64.b64decode(b64, validate=True)
        contents = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar.getmembers():
                name = os.path.basename(m.name)
                if not m.isfile() or name not in RESTORE_BASENAMES:
                    continue
                contents[name] = tar.extractfile(m).read()
    except Exception as e:
        return _fail(f"archive RESTORE_STATE_TGZ_B64 illisible: {e}")

    for name in RESTORE_BASENAMES:
        if name not in contents:
            return _fail(f"fichier absent de l'archive: {name}")
        got = hashlib.sha256(contents[name]).hexdigest()
        if got != str(manifest[name]).strip().lower():
            return _fail(f"hash invalide pour {name}: attendu "
                         f"{manifest[name]}, obtenu {got} -- AUCUNE ecriture")

    # Every byte verified against the operator manifest: write files +
    # sidecars atomically, marker last.
    for name in RESTORE_BASENAMES:
        payload = contents[name]
        _atomic_write(_p(name), payload)
        _atomic_write(_p(name) + ".sha256",
                      hashlib.sha256(payload).hexdigest().encode())
        log.info(f"[STATE_RESTORE] {name}: {len(payload)} octets restaures "
                 f"(sha256 verifie)")

    ok = JsonStore.save(_p("state_epoch.json"), {
        "initialized_at":
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_boot_probe": 0,
        "restored_at":
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "restore_source": "phase_2a_source_backup",
    })
    if not ok:
        return _fail("state_epoch.json impossible a ecrire apres restore")
    log.info("[STATE_RESTORE] restauration COMPLETE: 5/5 fichiers + "
             "state_epoch.json crees.")
    return True
