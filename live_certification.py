# -*- coding: utf-8 -*-
"""AIR-001 Wave 8 (DE-P0-002) — content-bound live certification.

Reproduced defect: LIVE execution was armed by configuration flags
alone (SHADOW_MODE=0 + ALLOW_ORDER_SUBMISSION=1). Nothing bound "live
is allowed" to WHAT was audited: any code, config, model or dependency
change kept live enabled under a stale authorization.

Policy:
- Live order submission requires a LiveCertificationManifest whose
  bindings are recomputed BY THE RUNNING PROCESS and must match its
  measured state exactly. Any drift =>
  LIVE_DISABLED_CERTIFICATION_DRIFT (fail closed, listing the drifted
  bindings).
- A manifest whose atr_verdict is not GO never authorizes live
  (LIVE_DISABLED_ATR_NO_GO). ATR-001's standing verdict is NO_GO, so
  no valid production manifest exists today; this module provides the
  MECHANISM and does not manufacture a GO.
- No manifest at all => LIVE_DISABLED_NO_CERTIFICATION.

Bindings: engine_commit, strategy_config_hash, risk_config_hash,
model identity, calibration identity, requirements.lock digest,
.python-version, and the canonical test gate's report digest when
present (a value can be ABSENT — it must then be ABSENT in the
manifest too; absence is bound, never ignored).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from config import _p

MANIFEST_FILE = "live_certification.json"
ABSENT = "ABSENT"

LIVE_DISABLED_NO_CERTIFICATION = "LIVE_DISABLED_NO_CERTIFICATION"
LIVE_DISABLED_ATR_NO_GO = "LIVE_DISABLED_ATR_NO_GO"
LIVE_DISABLED_CERTIFICATION_DRIFT = "LIVE_DISABLED_CERTIFICATION_DRIFT"
LIVE_DISABLED_MANIFEST_UNREADABLE = "LIVE_DISABLED_MANIFEST_UNREADABLE"

BOUND_KEYS = (
    "engine_commit", "strategy_config_hash", "risk_config_hash",
    "model_id", "calibration_id", "requirements_lock_sha256",
    "python_version", "test_report_sha256",
)


def _sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return ABSENT
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def compute_current_bindings() -> dict:
    """Measure the running process's identity. Failures surface as
    explicit ABSENT markers, never as invented values."""
    from config_identity import risk_config_hash, strategy_config_hash
    try:
        import research_export
        engine_commit = research_export.engine_commit()
        model_id = research_export.model_identity().get("model_hash")
        calibration_id = research_export.calibration_identity().get(
            "calibration_version")
    except Exception:                     # noqa: BLE001 — honest ABSENT
        engine_commit, model_id, calibration_id = ABSENT, None, None
    root = _repo_root()
    return {
        "engine_commit": engine_commit or ABSENT,
        "strategy_config_hash": strategy_config_hash(),
        "risk_config_hash": risk_config_hash(),
        "model_id": model_id if model_id is not None else ABSENT,
        "calibration_id": (calibration_id
                           if calibration_id is not None else ABSENT),
        "requirements_lock_sha256": _sha256_file(
            os.path.join(root, "requirements.lock")),
        "python_version": _read_python_version(root),
        "test_report_sha256": _sha256_file(
            os.path.join(root, "test_report.json")),
    }


def _read_python_version(root: str) -> str:
    path = os.path.join(root, ".python-version")
    if not os.path.exists(path):
        return ABSENT
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip() or ABSENT


def load_manifest(path: Optional[str] = None) -> Optional[dict]:
    target = path or _p(MANIFEST_FILE)
    if not os.path.exists(target):
        return None
    with open(target, encoding="utf-8") as fh:
        return json.load(fh)


def live_certification_check(path: Optional[str] = None,
                             bindings: Optional[dict] = None
                             ) -> tuple[bool, str]:
    """(ok, reason). ok=True ONLY for a readable manifest with
    atr_verdict == GO whose every bound key matches the recomputed
    process state."""
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError) as exc:
        return False, f"{LIVE_DISABLED_MANIFEST_UNREADABLE}: {exc}"
    if manifest is None:
        return False, (f"{LIVE_DISABLED_NO_CERTIFICATION}: no "
                       f"{MANIFEST_FILE} — live order submission "
                       "requires an operator-issued, content-bound "
                       "certification")
    if str(manifest.get("atr_verdict", "")).upper() != "GO":
        return False, (f"{LIVE_DISABLED_ATR_NO_GO}: manifest verdict "
                       f"is {manifest.get('atr_verdict')!r} — the "
                       "standing ATR verdict does not authorize live "
                       "capital")
    current = bindings if bindings is not None \
        else compute_current_bindings()
    bound = manifest.get("bindings") or {}
    drifted = []
    for key in BOUND_KEYS:
        if bound.get(key) != current.get(key):
            drifted.append(
                f"{key}: certified={bound.get(key)!r} "
                f"actual={current.get(key)!r}")
    if drifted:
        return False, (f"{LIVE_DISABLED_CERTIFICATION_DRIFT}: "
                       + "; ".join(drifted))
    return True, "live certification bindings verified"


def write_manifest(*, atr_verdict: str, operator: str,
                   bindings: Optional[dict] = None,
                   path: Optional[str] = None,
                   issued_at: Optional[str] = None) -> dict:
    """Materialize a manifest binding the CURRENT process state. The
    caller supplies the verdict — this function never invents GO."""
    manifest = {
        "atr_verdict": atr_verdict,
        "operator": operator,
        "issued_at": issued_at,
        "bindings": bindings if bindings is not None
        else compute_current_bindings(),
    }
    target = path or _p(MANIFEST_FILE)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest
