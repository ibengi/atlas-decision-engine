"""Read-only research evidence exposure (Program 008E).

This module exposes evidence the engine already writes. It computes nothing the
engine has not already computed, and it is imported by exactly two places: the
dashboard's HTTP handler, which serves it, and the pipeline, which asks it for
a lineage stamp.

Three properties are load-bearing.

**It never touches the trading path.** Everything here reads files that were
already written and closed. It is served from the dashboard's daemon thread,
which was already off the critical path before this program existed. No
function in this module is called during market evaluation, ordering, risk
evaluation or settlement.

**It never recomputes.** Where the engine has produced a number — a
probability, an edge, a PnL — this module hands back exactly what was written.
If two records disagree, the disagreement is exposed. Correcting history in an
exposure layer is how the corrected figure becomes the one people quote.

**Identity is derived from the artifact, not from the clock.** `model_identity`
hashes the source of the modules that define the model, so a restart produces
the same identity and a real change produces a different one. Stamping a new
version on every boot would make lineage useless in exactly the way lineage
exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Iterator, Optional

from config import CFG

RESEARCH_CONTRACT_VERSION = 1

#: Datasets are versioned independently. P0 adds two NEW datasets and changes
#: no existing one, so `decisions` and `settlements` keep their versions and a
#: v1 consumer that never asks for the new routes is unaffected.
SCHEMA_VERSIONS = {"decisions": 1, "settlements": 2,
                   "cycles": 1, "funnel_rejections": 1}

#: Modules whose content defines the probability model. Hashing their source
#: gives an identity that changes when the model changes and not otherwise.
MODEL_SOURCES = ("btc_probability_model.py", "strategy_router.py",
                 "pattern_engine.py")
CALIBRATION_SOURCES = ("calibration.py", "model_calibration.py",
                       "model_gatekeeper.py")

#: The rotating decision log and its historical generations, oldest first.
DECISION_FILES = ("decisions.jsonl.3", "decisions.jsonl.2",
                  "decisions.jsonl.1", "decisions.jsonl")

#: P0 rotating-jsonl families, oldest generation first (same convention as
#: DECISION_FILES so cursor ordering is identical across all three datasets).
CYCLE_FILES = ("cycles.jsonl.3", "cycles.jsonl.2",
               "cycles.jsonl.1", "cycles.jsonl")
FUNNEL_FILES = ("funnel_rejections.jsonl.3", "funnel_rejections.jsonl.2",
                "funnel_rejections.jsonl.1", "funnel_rejections.jsonl")

MAX_PAGE = 500
DEFAULT_PAGE = 100

_HERE = os.path.dirname(os.path.abspath(__file__))
_identity_cache: dict[str, Any] = {}


# ── identity ───────────────────────────────────────────────────────────────

def _hash_sources(names: tuple[str, ...]) -> tuple[str, list[str]]:
    """SHA-256 over the concatenated source of the named modules."""
    digest = hashlib.sha256()
    present: list[str] = []
    for name in names:
        path = os.path.join(_HERE, name)
        try:
            with open(path, "rb") as handle:
                digest.update(name.encode("utf-8"))
                digest.update(handle.read())
            present.append(name)
        except OSError:
            continue
    return digest.hexdigest()[:16], present


def model_identity() -> dict:
    """Deterministic identity for the model definition in this build."""
    if "model" in _identity_cache:
        return _identity_cache["model"]
    declared = ""
    try:
        from btc_probability_model import MODEL_VERSION
        declared = str(MODEL_VERSION)
    except Exception:                                    # noqa: BLE001
        declared = ""
    artifact, present = _hash_sources(MODEL_SOURCES)
    identity = {
        "model_version": declared or f"unversioned-{artifact}",
        "model_artifact_sha256": artifact,
        "model_sources": present,
        "declared_version_present": bool(declared),
        "derivation": ("SHA-256 over the source of the modules that define the "
                       "model. Stable across restarts; changes only when the "
                       "definition changes"),
    }
    _identity_cache["model"] = identity
    return identity


def calibration_identity() -> dict:
    """Deterministic identity for the calibration definition in this build."""
    if "calibration" in _identity_cache:
        return _identity_cache["calibration"]
    artifact, present = _hash_sources(CALIBRATION_SOURCES)
    identity = {
        "calibration_version": f"cal-{artifact}",
        "calibration_artifact_sha256": artifact,
        "calibration_sources": present,
        "behaviour_changed": False,
        "derivation": ("SHA-256 over the source of the calibration modules. "
                       "This identifies calibration; it does not alter it"),
    }
    _identity_cache["calibration"] = identity
    return identity


def engine_commit() -> str:
    """The commit this process is running, if the platform supplies it."""
    for variable in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_COMMIT",
                     "ENGINE_COMMIT"):
        value = os.getenv(variable, "")
        if value:
            return value
    return ""


def lineage() -> dict:
    """The non-behavioural stamp attached to each new decision record.

    Cached: computed once per process. Called from the pipeline, so it must
    cost nothing after the first call.
    """
    if "lineage" in _identity_cache:
        return _identity_cache["lineage"]
    model = model_identity()
    calibration = calibration_identity()
    # Deliberately minimal. This dict is written on every decision row of a
    # rotating 5 MB log, so each byte costs retained history: the first version
    # grew a row by 33% and cut retained rows per generation by a quarter.
    # `feature_version` and `research_contract_version` were dropped because
    # both are derivable — the first from the artifact hash, the second is a
    # constant served by /capabilities. Nothing that cannot be recovered from
    # what remains was removed.
    stamp = {
        "engine_commit": engine_commit(),
        "model_version": model["model_version"],
        "model_artifact_sha256": model["model_artifact_sha256"],
        "calibration_version": calibration["calibration_version"],
    }
    _identity_cache["lineage"] = stamp
    return stamp


# ── authorisation ──────────────────────────────────────────────────────────

def research_token() -> str:
    return os.getenv("RESEARCH_API_TOKEN", "")


def authorised(header_value: Optional[str]) -> bool:
    """Constant-time bearer check.

    Absent configuration means refusal, not open access: an evidence endpoint
    that serves everything when nobody set a token is worse than one that
    serves nothing.
    """
    expected = research_token()
    if not expected:
        return False
    presented = (header_value or "").strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


# ── reading persisted evidence ─────────────────────────────────────────────

def _decision_lines(data_dir: str) -> Iterator[tuple[str, str]]:
    """Every retained decision line, oldest generation first.

    Yields (record_id, raw_line). The id is a content hash, so it is stable
    across the rotation that renames the file underneath it.
    """
    for name in DECISION_FILES:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if text:
                        yield (hashlib.sha256(
                            text.encode("utf-8")).hexdigest()[:20], text)
        except OSError:
            continue


def _page(items: list, cursor: str, limit: int, key) -> dict:
    """Resume after `cursor`, or report that the cursor has aged out."""
    limit = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    start = 0
    cursor_state = "start"
    if cursor:
        found = next((i for i, item in enumerate(items)
                      if key(item) == cursor), None)
        if found is None:
            # The row the caller last saw is gone. Rotation removed it. Saying
            # "here is the beginning again" would duplicate history; saying
            # nothing would hide a gap.
            cursor_state = "expired"
            start = 0
        else:
            cursor_state = "resumed"
            start = found + 1
    window = items[start:start + limit]
    return {
        "rows": window,
        "next_cursor": key(window[-1]) if window else cursor,
        "has_more": start + len(window) < len(items),
        "cursor_state": cursor_state,
        "cursor_expired_note": (
            "the cursor referred to a row that is no longer retained; the "
            "rotating decision log discarded it. Evidence between the last "
            "cursor and the oldest retained row is unavailable"
            if cursor_state == "expired" else ""),
        "available_rows": len(items),
    }


def _jsonl_rows(data_dir: str, names: tuple[str, ...]) -> tuple[list, int]:
    """Parse a rotating jsonl family, oldest generation first.

    Same content-hash record_id as the decision reader, so cursors stay
    stable across the rotation that renames files underneath them.
    """
    rows: list[dict] = []
    unreadable = 0
    for name in names:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        unreadable += 1
                        continue
                    if not isinstance(parsed, dict):
                        unreadable += 1
                        continue
                    parsed["record_id"] = hashlib.sha256(
                        text.encode("utf-8")).hexdigest()[:20]
                    rows.append(parsed)
        except OSError:
            continue
    return rows, unreadable


def cycles(data_dir: str, cursor: str = "", limit: int = DEFAULT_PAGE) -> dict:
    """P0: one row per engine cycle, including cycles that never scanned.

    This is the dataset that makes a quiet period explainable. A row whose
    `blocking_global_guard` is non-null says a global guard stopped the cycle
    before evaluation; a row with `scan_executed: false` says no funnel
    counters exist for that cycle because the scan never ran, which is
    different from a scan that returned nothing.
    """
    rows, unreadable = _jsonl_rows(data_dir, CYCLE_FILES)
    page = _page(rows, cursor, limit, key=lambda r: r["record_id"])
    return {
        "dataset": "cycles",
        "schema_version": SCHEMA_VERSIONS["cycles"],
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "engine_commit": engine_commit(),
        "unreadable_lines": unreadable,
        "recomputed": False,
        "absent_funnel_note": (
            "funnel counters are omitted, never zeroed, when scan_executed "
            "is false: the scanner did not run, so there is no population to "
            "report and a zero would assert a measurement nobody made"),
        **page,
    }


def funnel_rejections(data_dir: str, cursor: str = "",
                      limit: int = DEFAULT_PAGE) -> dict:
    """P0: per-market scanner-stage eliminations.

    Markets dropped by the scanner never reach the model, so they can never
    appear in the decisions dataset. Before P0 they existed only as aggregate
    counts, which made "the universe was eliminated upstream" unprovable at
    market granularity.
    """
    rows, unreadable = _jsonl_rows(data_dir, FUNNEL_FILES)
    page = _page(rows, cursor, limit, key=lambda r: r["record_id"])
    return {
        "dataset": "funnel_rejections",
        "schema_version": SCHEMA_VERSIONS["funnel_rejections"],
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "engine_commit": engine_commit(),
        "unreadable_lines": unreadable,
        "recomputed": False,
        **page,
    }


def decisions(data_dir: str, cursor: str = "", limit: int = DEFAULT_PAGE) -> dict:
    """Historical decision records, exactly as they were written."""
    rows: list[dict] = []
    unreadable = 0
    for record_id, text in _decision_lines(data_dir):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            unreadable += 1
            continue
        parsed["record_id"] = record_id
        rows.append(parsed)
    page = _page(rows, cursor, limit, key=lambda r: r["record_id"])
    return {
        "dataset": "decisions",
        "schema_version": SCHEMA_VERSIONS["decisions"],
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "engine_commit": engine_commit(),
        "unreadable_lines": unreadable,
        "recomputed": False,
        **page,
    }


def _trades(data_dir: str) -> list[dict]:
    """The authoritative trade ledger, exactly as TradeLogger writes it.

    Reads CFG.TRADES_FILE — the configured name the engine's settlement
    writer persists to. (T7-B: this previously probed two filenames no
    engine code ever wrote, so the settlement surface served zero rows.)
    """
    path = os.path.join(data_dir, CFG.TRADES_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        # Vue effective: un evenement ``ledger_correction`` n'est pas un
        # trade et ne doit jamais etre servi comme une resolution
        # supplementaire; son economie (PnL, frais, quantite) est repliee
        # dans le trade qu'il corrige, lequel porte alors ``corrected`` et
        # ``correction_ids`` pour l'audit.
        from trade_logger import fold_corrections
        return fold_corrections([t for t in data if isinstance(t, dict)])
    return []


def _settled_rows(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Settlement evidence = settled trades only, in settlement order.

    A row qualifies only when the ledger says the trade is terminal AND it
    carries both a result and a settlement time. An open position is a live
    trade, not an outcome, and is skipped silently; anything else that fails
    to qualify is excluded AND reported — neither silently served nor
    silently dropped. Ordering is (settled_at, trade_id): settlement is a
    LATER state transition on a row created at open time, so an export
    ordered by open time can hide a resolution behind a consumer's cursor
    forever. Settlement-time ordering makes every new resolution appear
    after everything already consumed.
    """
    rows: list[dict] = []
    excluded: list[dict] = []
    for t in trades:
        state = str(t.get("state") or "")
        if state == "open":
            continue
        if state != "settled":
            excluded.append({"trade_id": t.get("trade_id"),
                             "note": f"unrecognized state {state!r}; not "
                                     f"served as settlement evidence"})
            continue
        if not t.get("result") or not t.get("settled_at"):
            excluded.append({"trade_id": t.get("trade_id"),
                             "note": "settled without result/settled_at; "
                                     "cannot be interpreted as an outcome; "
                                     "not served"})
            continue
        rows.append(t)
    rows.sort(key=lambda t: (str(t.get("settled_at") or ""),
                             str(t.get("trade_id") or "")))
    return rows, excluded


def settlements(data_dir: str, cursor: str = "",
                limit: int = DEFAULT_PAGE) -> dict:
    """Authoritative settlement evidence. Never recomputed here.

    Settled trades only, cursored by (settled_at, trade_id) so a trade
    opened before a consumer's cursor position still becomes discoverable
    at the moment it settles.
    """
    rows, excluded = _settled_rows(_trades(data_dir))
    discrepancies = []
    for trade in rows:
        gross, net, fees = (trade.get("gross_pnl"), trade.get("net_pnl"),
                            trade.get("fees"))
        if None in (gross, net, fees):
            continue
        try:
            if abs(float(gross) - float(fees) - float(net)) > 0.011:
                discrepancies.append({
                    "trade_id": trade.get("trade_id"),
                    "gross_pnl": gross, "fees": fees, "net_pnl": net,
                    "note": "the engine's own record does not reconcile; "
                            "exposed, not corrected"})
        except (TypeError, ValueError):
            continue
    page = _page(rows, cursor, limit,
                 key=lambda t: f"{t.get('settled_at')}|{t.get('trade_id')}")
    return {
        "dataset": "settlements",
        "schema_version": SCHEMA_VERSIONS["settlements"],
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "engine_commit": engine_commit(),
        "recomputed": False,
        "recompute_policy": ("no PnL is recomputed by this exposure layer; "
                             "the engine's figure is the authoritative one"),
        "discrepancies": discrepancies,
        "excluded_rows": excluded[:20],
        "excluded_count": len(excluded),
        **page,
    }


# ── capability manifest ────────────────────────────────────────────────────

def _bounds(rows: list[dict], field: str) -> tuple[str, str]:
    stamps = sorted(str(r.get(field) or "") for r in rows if r.get(field))
    return (stamps[0], stamps[-1]) if stamps else ("", "")


def capabilities(data_dir: str) -> dict:
    """What this engine can actually serve. Declared, not inferred."""
    decision_rows = decisions(data_dir, "", MAX_PAGE)
    cycle_rows = cycles(data_dir, "", MAX_PAGE)
    funnel_rows = funnel_rejections(data_dir, "", MAX_PAGE)
    settled, _excluded = _settled_rows(_trades(data_dir))
    retained = [os.path.basename(p) for p in (
        os.path.join(data_dir, n) for n in DECISION_FILES) if os.path.exists(p)]
    earliest, latest = _bounds(settled, "settled_at")

    linked = sum(1 for r in decision_rows["rows"] if r.get("run_id"))
    with_model = sum(1 for r in decision_rows["rows"]
                     if (r.get("lineage") or {}).get("model_version"))

    return {
        "contract_version": RESEARCH_CONTRACT_VERSION,
        "engine_commit": engine_commit(),
        "model": model_identity(),
        "calibration": calibration_identity(),
        "datasets": [
            {
                "name": "decisions",
                "schema_version": SCHEMA_VERSIONS["decisions"],
                "retention": "rotating: 5 MB x 4 generations, oldest deleted",
                "permanent": False,
                "row_count": decision_rows["available_rows"],
                "retained_files": retained,
                "known_gaps": ("rows discarded by rotation are unrecoverable; "
                               "a consumer slower than the rotation rate will "
                               "miss evidence permanently"),
                "linkage_availability": {
                    "run_id": f"{linked}/{len(decision_rows['rows'])} of the "
                              f"sampled page",
                    "note": "records written before this build carry no "
                            "run_id and none is invented for them",
                },
                "model_version_availability":
                    f"{with_model}/{len(decision_rows['rows'])} of the sampled "
                    f"page; absent on records written before this build",
            },
            {
                "name": "cycles",
                "schema_version": SCHEMA_VERSIONS["cycles"],
                "retention": "rotating: 5 MB x 4 generations, oldest deleted",
                "permanent": False,
                "row_count": cycle_rows["available_rows"],
                "known_gaps": ("one row per cycle from this build onward; "
                               "cycles that ran before P0 wrote no row and "
                               "none is reconstructed for them. Funnel "
                               "counters are absent, never zero, when "
                               "scan_executed is false"),
            },
            {
                "name": "funnel_rejections",
                "schema_version": SCHEMA_VERSIONS["funnel_rejections"],
                "retention": "rotating: 5 MB x 4 generations, oldest deleted",
                "permanent": False,
                "row_count": funnel_rows["available_rows"],
                "known_gaps": ("scanner-stage eliminations only; per-cycle "
                               "detail is capped at SCANNER_REJECT_DETAIL_MAX "
                               "and the suppressed count is reported on the "
                               "cycle rather than silently dropped"),
            },
            {
                "name": "settlements",
                "schema_version": SCHEMA_VERSIONS["settlements"],
                "retention": "durable JSON store with checksum and backups",
                "permanent": True,
                "row_count": len(settled),
                "earliest": earliest,
                "latest": latest,
                "known_gaps": ("settled trades only; open positions are "
                               "never settlement evidence. Trades opened "
                               "before decision linkage existed carry no "
                               "decision_id and cannot be joined to a "
                               "decision; none is invented for them"),
                "linkage_availability": {
                    "order_id": sum(1 for t in settled if t.get("order_id")),
                    "trade_id": sum(1 for t in settled if t.get("trade_id")),
                    "decision_id": sum(1 for t in settled
                                       if t.get("decision_id")),
                },
            },
        ],
        "unavailable": {
            "risk_evaluations": "risk gate outcomes are logged, not persisted "
                                "to a durable store; they cannot be served "
                                "from disk",
            "fills": "fill detail lives inside the trade record; there is no "
                     "separate fill store",
        },
        "write_operations": [],
        "write_note": ("none. This interface defines no operation that changes "
                       "engine state"),
    }
