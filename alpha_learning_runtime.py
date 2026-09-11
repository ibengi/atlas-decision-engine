"""Runtime helpers for Atlas Alpha Learning v1. SHADOW ONLY.

This module turns the immutable AlphaLedger into a durable learning report and
compact prior-case context. It has no broker imports, no execution authority,
and never mutates historical predictions.
"""

import json
import os
import tempfile
from datetime import datetime, timezone

from alpha_learning import learning_report, similar_cases

DEFAULT_REPORT_FILE = "alpha_learning_report.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def learning_snapshot(ledger, *, astra_selector="astra",
                      baseline_selector="atlas_quant",
                      subscription_cost_usd=100.0,
                      memory_limit=8,
                      market_class=None) -> dict:
    report = learning_report(
        ledger,
        astra_selector=astra_selector,
        baseline_selector=baseline_selector,
        subscription_cost_usd=subscription_cost_usd,
    )
    report["generated_at"] = _now_iso()
    report["learning_version"] = "astra-alpha-learning-v1"
    report["selected_memory"] = similar_cases(
        report.get("memory") or [], market_class=market_class,
        limit=memory_limit)
    return report


#: AA-16. Ledger files a derived report must never be able to replace. A
#: report is a VIEW; an `os.replace` onto a source ledger would destroy the
#: append-only history the whole subsystem rests on, in one syscall, with no
#: trace and no recovery.
def _protected_source_paths(ledger, directory) -> dict:
    from config import CFG
    paths = {}
    for label, attr in (("prediction ledger", "log"),
                        ("cost ledger", "cost_log")):
        source = getattr(ledger, attr, None)
        path = getattr(source, "path", None)
        if path:
            paths[os.path.realpath(path)] = label
    processed = os.path.join(directory, CFG.ALPHA_STATE_FILE)
    paths.setdefault(os.path.realpath(processed), "processed ledger")
    return paths


def _assert_not_a_source_ledger(target, ledger, directory) -> None:
    """Refuse to publish a report over a ledger (AA-16).

    Both paths are canonicalised with `realpath` first, so a symlink, a `..`
    segment or a relative path cannot be used to reach a ledger under a name
    that merely looks different. Where the target already exists, the check is
    repeated on INODE identity, which catches a hard link -- the one alias
    `realpath` cannot see through.
    """
    protected = _protected_source_paths(ledger, directory)
    resolved = os.path.realpath(target)
    if resolved in protected:
        raise ValueError(
            f"refusing to publish the learning report over the "
            f"{protected[resolved]} at {resolved}: a derived report never "
            f"replaces a source ledger")
    try:
        target_stat = os.stat(resolved)
    except OSError:
        return                       # does not exist yet; no alias possible
    for path, label in protected.items():
        try:
            source_stat = os.stat(path)
        except OSError:
            continue
        if (target_stat.st_dev, target_stat.st_ino) == \
                (source_stat.st_dev, source_stat.st_ino):
            raise ValueError(
                f"refusing to publish the learning report over the {label}: "
                f"{resolved} and {path} are the same file")


def write_learning_report(ledger, directory, *, filename=DEFAULT_REPORT_FILE,
                          **kwargs) -> dict:
    """Atomically publish a derived report without editing ledger history."""
    report = learning_snapshot(ledger, **kwargs)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, filename)
    _assert_not_a_source_ledger(target, ledger, directory)
    fd, tmp = tempfile.mkstemp(prefix=".alpha-learning-", suffix=".tmp",
                               dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        # Persist the directory entry where supported.
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return report


def memory_context(ledger, *, astra_selector="astra", market_class=None,
                   limit=5) -> str:
    """Compact JSON context suitable for a future Astra research prompt.

    It contains only prior resolved cases; no broker action, side, size, or
    execution instruction is generated here.
    """
    report = learning_report(ledger, astra_selector=astra_selector,
                             baseline_selector="atlas_quant",
                             subscription_cost_usd=0.0)
    cases = similar_cases(report.get("memory") or [],
                          market_class=market_class, limit=limit)
    payload = {
        "purpose": "forecast_calibration_memory",
        "market_class": market_class,
        "cases": cases,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
