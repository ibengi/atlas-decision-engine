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
from alpha_persistence_paths import publication_guard

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
def _protected_source_paths(ledger, directory, processed_store=None,
                            budget_ledger=None, telemetry=None,
                            persistence_objects=()) -> dict:
    """Every append-only source file a derived report must never replace.

    AA-16 (re-audit) -- PROTECT THE PATH THE STORE ACTUALLY USES.
        The two ledgers were protected by their REAL paths, read off the
        ledger object. The processed store was protected by a reconstruction:
        `os.path.join(directory, CFG.ALPHA_STATE_FILE)`.

        `ProcessedStore` resolves its path as `_p(CFG.ALPHA_STATE_FILE)` --
        against `CFG.DATA_DIR` -- and `directory` here is wherever the report
        is being published, which need not be `DATA_DIR`. Configure a
        nondefault relative state file, or publish a report outside the data
        directory, and the guarded path was a file nobody writes while the
        file the service appends every processed mark to was left open. A
        report published over it destroys the record of every analysis
        already paid for, in one `os.replace`, with no trace.

        So: the store's own path when a store is supplied, the configured
        path resolved the way the store resolves it, and the old
        report-directory guess kept as well -- a superset is free, and each
        entry is a path something in this system genuinely opens for writing.

    RA-14 -- AND THE OTHER TWO FILES THIS SYSTEM WRITES.
        The guard knew about the prediction ledger, the cost ledger and the
        processed store. It did not know about `alpha_budget_ledger.jsonl` --
        append-only, and the file every cost cap is enforced against -- or
        about the telemetry file.

        AA-16's own argument applies unchanged. `write_learning_report`
        finishes with `os.replace(tmp, target)`, and `filename` is
        caller-supplied, so a report published as `alpha_budget_ledger.jsonl`
        -- or as anything that RESOLVES to it -- destroys every dollar the
        service has recorded spending, in one syscall, with no trace. The
        next `spent_today()` then returns 0.0 and every cap silently means
        "unlimited": RA-06's failure direction reached by deleting the
        evidence instead of by mis-reading it.

        Both are now protected by their real path when the caller has the
        object to hand, by the configured path resolved the way the object
        resolves it, and by the report-directory reading -- the same three
        spellings AA-16's re-audit settled on, for the same reason.
    """
    from config import CFG, _p
    from alpha_persistence_paths import registered_persistence_paths
    paths = {os.path.realpath(path): label for path, label in
             registered_persistence_paths().items()}
    for source in (telemetry, *persistence_objects):
        path = getattr(source, "path", None)
        if path:
            paths.setdefault(os.path.realpath(path), "runtime persistence")
    for label, attr in (("prediction ledger", "log"),
                        ("cost ledger", "cost_log")):
        source = getattr(ledger, attr, None)
        path = getattr(source, "path", None)
        if path:
            paths[os.path.realpath(path)] = label
    # The store's ACTUAL path, when the caller has one to hand.
    store_path = getattr(processed_store, "path", None)
    if store_path:
        paths.setdefault(os.path.realpath(store_path), "processed ledger")
    # The configured path, resolved exactly as `ProcessedStore` resolves it.
    paths.setdefault(os.path.realpath(_p(CFG.ALPHA_STATE_FILE)),
                     "processed ledger")
    # And the report directory reading, which is what a co-located
    # deployment produces.
    paths.setdefault(
        os.path.realpath(os.path.join(directory, CFG.ALPHA_STATE_FILE)),
        "processed ledger")

    # RA-14. The budget ledger, all three spellings: the object's own path,
    # the module default resolved the way `BudgetLedger` resolves it, and the
    # report-directory reading.
    from alpha_cost import BUDGET_LEDGER_FILE
    budget_path = getattr(budget_ledger, "path", None)
    if budget_path:
        paths.setdefault(os.path.realpath(budget_path), "budget ledger")
    paths.setdefault(os.path.realpath(_p(BUDGET_LEDGER_FILE)),
                     "budget ledger")
    paths.setdefault(
        os.path.realpath(os.path.join(directory, BUDGET_LEDGER_FILE)),
        "budget ledger")

    # RA-14. The telemetry file is rewritten rather than appended, so losing
    # it loses less -- but a report published over it is still a report
    # published over a file this system owns, and the cost of guarding it is
    # one dictionary entry.
    paths.setdefault(os.path.realpath(_p(CFG.ALPHA_TELEMETRY_FILE)),
                     "telemetry ledger")
    paths.setdefault(
        os.path.realpath(os.path.join(directory, CFG.ALPHA_TELEMETRY_FILE)),
        "telemetry ledger")
    return paths


def _assert_not_a_source_ledger(target, ledger, directory,
                                processed_store=None,
                                budget_ledger=None, telemetry=None,
                                persistence_objects=()) -> None:
    """Refuse to publish a report over a ledger (AA-16).

    Both paths are canonicalised with `realpath` first, so a symlink, a `..`
    segment or a relative path cannot be used to reach a ledger under a name
    that merely looks different. Where the target already exists, the check is
    repeated on INODE identity, which catches a hard link -- the one alias
    `realpath` cannot see through.
    """
    protected = _protected_source_paths(ledger, directory,
                                        processed_store, budget_ledger,
                                        telemetry, persistence_objects)
    resolved = os.path.realpath(target)
    if resolved in protected:
        raise ValueError(
            f"refusing to publish the learning report over the "
            f"{protected[resolved]} at {resolved}: a derived report never "
            f"replaces a source ledger")
    try:
        target_stat = os.stat(resolved)
    except FileNotFoundError:
        return                       # does not exist yet; no alias possible
    for path, label in protected.items():
        try:
            source_stat = os.stat(path)
        except FileNotFoundError:
            continue
        if (target_stat.st_dev, target_stat.st_ino) == \
                (source_stat.st_dev, source_stat.st_ino):
            raise ValueError(
                f"refusing to publish the learning report over the {label}: "
                f"{resolved} and {path} are the same file")


def write_learning_report(ledger, directory, *, filename=DEFAULT_REPORT_FILE,
                          processed_store=None, budget_ledger=None,
                          telemetry=None, persistence_objects=(),
                          **kwargs) -> dict:
    """Atomically publish a derived report without editing ledger history.

    `processed_store` and `budget_ledger` are optional because the guard also
    protects the CONFIGURED path of each; passing the object protects the path
    it is actually using, which is what AA-16's re-audit found matters when
    the two differ.
    """
    report = learning_snapshot(ledger, **kwargs)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, filename)
    _assert_not_a_source_ledger(target, ledger, directory, processed_store,
                                budget_ledger, telemetry, persistence_objects)
    fd, tmp = tempfile.mkstemp(prefix=".alpha-learning-", suffix=".tmp",
                               dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        # Re-evaluate the live path registry at the publication boundary.
        with publication_guard():
            _assert_not_a_source_ledger(target, ledger, directory, processed_store,
                                        budget_ledger, telemetry, persistence_objects)
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
