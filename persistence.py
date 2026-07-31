"""Atomic JSON persistence layer with checksums and backup rotation."""

import os
import json
import hashlib
import shutil
import logging

from config import CFG

log = logging.getLogger("PERSISTENCE")


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
