#!/usr/bin/env python3
"""Health monitoring for the Atlas Decision Engine (P4.3).

Central registry of named, on-demand health checks, aggregate status
computation (healthy / degraded / unhealthy) and JSON serialization for the
``/health`` endpoint served by ``dashboard_web.py``.

Built-in checks
---------------
- ``api_connectivity``        bounded single-attempt probe of the Kalshi API
- ``position_reconciliation`` health of the broker reconciliation
                              (state files, no API call)
- ``recent_errors``           errors in the last N pipeline runs (recorded
                              in-process by the engine via record_run())
- ``last_run_age``            age of the last successful pipeline run

All checks are lightweight and non-blocking by design:
- file reads only for reconciliation,
- a single HTTP probe with a short bounded timeout for API connectivity,
- in-memory counters for runs/errors.

Integration
-----------
- ``ExecutionEngine`` calls ``HEALTH.set_client(client)`` at startup and
  ``HEALTH.record_run(ok, run_id, cycle)`` after each cycle.
- ``dashboard_web.py`` imports ``get_health_json()`` and serves it on
  ``GET /health``.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from config import CFG

log = logging.getLogger("BOT")


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def now_iso() -> str:
    """Current UTC time as ISO-8601 (same convention as the engine logs)."""
    return datetime.now(timezone.utc).isoformat()


def _result(status: str, message: str) -> dict:
    """One check result: {"status": pass|warn|fail, "message", "last_checked"}."""
    return {"status": status, "message": message,
            "last_checked": now_iso()}


def _load_json(path: str):
    """Read a JSON file; return None if missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────
# Individual check functions (module-level, testable standalone)
# ─────────────────────────────────────────────────────────────────────────
def check_api_connectivity(client=None, timeout=None) -> dict:
    """Bounded single-attempt probe of the Kalshi API.

    Uses the client's own base_url when one is provided (so the correct
    environment is probed), otherwise the configured demo endpoint. The
    probe hits the public, unauthenticated ``/markets`` endpoint with a
    short timeout — it never goes through the client's retry/backoff
    machinery, so it cannot block for more than ``timeout`` seconds.
    """
    import requests  # lazy: keep module import side-effect free
    timeout = timeout if timeout is not None else float(
        os.getenv("HEALTH_API_PROBE_TIMEOUT_S", "3.0"))
    base = getattr(client, "base_url", None) if client is not None else None
    if not base:
        base = CFG.DEMO_URL
    url = base.rstrip("/") + "/markets"
    started = time.monotonic()
    try:
        r = requests.get(url, params={"limit": 1}, timeout=timeout)
        ms = int((time.monotonic() - started) * 1000)
        if r.status_code == 200:
            return _result("pass", f"Kalshi API reachable (HTTP 200 in {ms}ms)")
        return _result("fail",
                       f"Kalshi API answered HTTP {r.status_code} ({ms}ms)")
    except Exception as e:                                # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        return _result("fail",
                       f"Kalshi API unreachable: {type(e).__name__} ({ms}ms)")


def check_position_reconciliation(data_dir=None, stale_seconds: int = 86400) -> dict:
    """File-based reconciliation health (no API call).

    - positions_state.json missing  -> nothing to reconcile -> pass
    - positions_state.json corrupt  -> fail
    - ghost_local_only positions    -> warn (local/broker divergence)
    - reconciliation_report.json with recent rebuilt/ghost entries -> warn
    - otherwise                     -> pass
    """
    data_dir = data_dir or CFG.DATA_DIR
    pos_path = os.path.join(data_dir, CFG.POSITIONS_FILE)
    rep_path = os.path.join(data_dir, "reconciliation_report.json")

    positions = _load_json(pos_path)
    if positions is None:
        if os.path.exists(pos_path):
            return _result("fail", "positions_state.json unreadable/corrupt")
        return _result("pass", "no positions state file — nothing to reconcile")

    ghosts = [tid for tid, p in (positions or {}).items()
              if isinstance(p, dict) and p.get("state") == "ghost_local_only"]
    if ghosts:
        return _result("warn",
                       f"{len(ghosts)} ghost_local_only position(s) present: "
                       f"{ghosts[:5]}{'...' if len(ghosts) > 5 else ''}")

    if os.path.exists(rep_path):
        report = _load_json(rep_path)
        if report is None:
            return _result("warn", "reconciliation_report.json unreadable")
        rebuilt = report.get("rebuilt") or []
        ghosted = report.get("ghost") or []
        if rebuilt or ghosted:
            try:
                age_h = (time.time() - os.path.getmtime(rep_path)) / 3600.0
            except OSError:
                age_h = None
            if age_h is not None and age_h < stale_seconds / 3600.0:
                return _result(
                    "warn",
                    f"broker divergence at last pass "
                    f"(rebuilt={len(rebuilt)}, ghosts={len(ghosted)})")
            return _result("pass", "historical divergence resolved")
        return _result("pass", "reconciliation OK, no divergence reported")
    return _result("pass", "positions OK, no divergence reported")


def check_recent_errors(monitor=None, window: int = 10) -> dict:
    """Errors in the last N pipeline runs, from in-process run history."""
    mon = monitor or HEALTH
    runs = mon.recent_runs(window)
    if not runs:
        return _result("warn",
                       "no pipeline run recorded since monitor start")
    n = len(runs)
    errs = [r for r in runs if not r.get("ok")]
    if errs:
        last = errs[-1]
        detail = last.get("error") or "unknown error"
        return _result("fail",
                       f"{len(errs)} run(s) in error out of last {n} "
                       f"(latest: {detail})")
    return _result("pass", f"last {n} run(s) without error")


def check_last_run_age(monitor=None, data_dir=None,
                       warn_seconds: int = None, fail_seconds: int = None) -> dict:
    """Age of the last successful pipeline run.

    In-memory timestamp when the engine records runs; falls back to the
    mtime of ``cycle_report.json`` so a standalone dashboard still reports
    something honest.
    """
    warn_s = warn_seconds if warn_seconds is not None else int(
        os.getenv("HEALTH_RUN_STALE_WARN_S", "300"))
    fail_s = fail_seconds if fail_seconds is not None else int(
        os.getenv("HEALTH_RUN_STALE_FAIL_S", "900"))
    mon = monitor or HEALTH
    ts = mon.last_run_ts
    if ts is None:
        try:
            ts = os.path.getmtime(
                os.path.join(data_dir or CFG.DATA_DIR, "cycle_report.json"))
        except OSError:
            return _result("fail",
                           "no pipeline run recorded (no state file yet)")
    age = time.time() - ts
    age_s = f"{int(age)}s"
    if age <= warn_s:
        return _result("pass", f"last successful run {age_s} ago")
    if age <= fail_s:
        return _result("warn",
                       f"last successful run {age_s} ago "
                       f"(warn threshold {warn_s}s)")
    return _result("fail",
                   f"last successful run {age_s} ago "
                   f"(critical threshold {fail_s}s)")


# ─────────────────────────────────────────────────────────────────────────
# Registry / monitor
# ─────────────────────────────────────────────────────────────────────────
class HealthMonitor:
    """Registry of named health checks + run history for the engine.

    Thread-safe: record_run() is called by the engine thread while
    run_all()/get_health_json() are called by dashboard request threads.
    """

    def __init__(self, run_history: int = 200):
        self.checks = {}                     # name -> callable() -> dict
        self.client = None                   # set by ExecutionEngine
        self.data_dir = None
        self.api_timeout = float(os.getenv("HEALTH_API_PROBE_TIMEOUT_S", "3.0"))
        self.error_window = int(os.getenv("HEALTH_ERROR_WINDOW", "10"))
        self.run_stale_warn_s = int(os.getenv("HEALTH_RUN_STALE_WARN_S", "300"))
        self.run_stale_fail_s = int(os.getenv("HEALTH_RUN_STALE_FAIL_S", "900"))
        self._lock = threading.Lock()
        self._runs = deque(maxlen=run_history)   # {ok, ts, run_id, cycle, error}
        self.last_run_ts = None                  # epoch of last SUCCESSFUL run
        self.last_run_id = None
        self.last_run_cycle = None
        self.run_count = 0
        self.error_count = 0
        self.started = time.time()

    # -- registry ---------------------------------------------------------
    def register(self, name: str, fn) -> None:
        """Register a named check (callable returning a check-result dict)."""
        self.checks[name] = fn

    def unregister(self, name: str) -> None:
        self.checks.pop(name, None)

    def register_default_checks(self) -> None:
        """Register the four built-in checks bound to this monitor."""
        self.register("api_connectivity",
                      lambda: check_api_connectivity(
                          client=self.client, timeout=self.api_timeout))
        self.register("position_reconciliation",
                      lambda: check_position_reconciliation(
                          data_dir=self.data_dir or CFG.DATA_DIR))
        self.register("recent_errors",
                      lambda: check_recent_errors(monitor=self,
                                                  window=self.error_window))
        self.register("last_run_age",
                      lambda: check_last_run_age(
                          monitor=self, data_dir=self.data_dir or CFG.DATA_DIR,
                          warn_seconds=self.run_stale_warn_s,
                          fail_seconds=self.run_stale_fail_s))

    # -- run history ------------------------------------------------------
    def set_client(self, client) -> None:
        """Bind the engine client so api_connectivity probes the right env."""
        self.client = client

    def record_run(self, ok: bool, run_id=None, cycle=None, error=None) -> None:
        """Record one pipeline run outcome (called by ExecutionEngine.cycle)."""
        with self._lock:
            self.run_count += 1
            if not ok:
                self.error_count += 1
            self._runs.append({"ok": bool(ok), "ts": time.time(),
                               "run_id": run_id, "cycle": cycle,
                               "error": error})
            if ok:
                self.last_run_ts = self._runs[-1]["ts"]
                self.last_run_id = run_id
                self.last_run_cycle = cycle

    def recent_runs(self, window: int) -> list:
        with self._lock:
            return list(self._runs)[-window:]

    # -- evaluation -------------------------------------------------------
    def run_all(self) -> dict:
        """Run every registered check; a failing check never raises."""
        with self._lock:
            names = list(self.checks.items())
        results = {}
        for name, fn in names:
            try:
                r = fn()
            except Exception as e:                        # noqa: BLE001
                r = _result("fail",
                            f"check raised {type(e).__name__}: {e}")
            results[name] = r
        return results

    @staticmethod
    def aggregate_status(results: dict) -> str:
        """healthy = all pass ; degraded = at least one warn ; else unhealthy."""
        if any(r.get("status") == "fail" for r in results.values()):
            return "unhealthy"
        if any(r.get("status") == "warn" for r in results.values()):
            return "degraded"
        return "healthy"

    def uptime_seconds(self) -> int:
        return int(time.time() - self.started)

    def get_health_json(self, data_dir: str = None) -> dict:
        """Full health payload, ready for a JSON response."""
        if data_dir is not None:
            self.data_dir = data_dir
        results = self.run_all()
        return {
            "status": self.aggregate_status(results),
            "checks": results,
            "uptime_seconds": self.uptime_seconds(),
            "last_run_id": self.last_run_id,
            "last_run_ts": (datetime.fromtimestamp(self.last_run_ts,
                                                   tz=timezone.utc)
                            .isoformat() if self.last_run_ts else None),
            "run_count": self.run_count,
            "error_count": self.error_count,
            "generated_at": now_iso(),
        }


# ─────────────────────────────────────────────────────────────────────────
# Module-level singleton + convenience accessor
# ─────────────────────────────────────────────────────────────────────────
HEALTH = HealthMonitor()
HEALTH.register_default_checks()


def get_health_json(monitor=None, data_dir: str = None) -> dict:
    """Serialize the full health status as a dict (module-level accessor)."""
    return (monitor or HEALTH).get_health_json(data_dir=data_dir)


if __name__ == "__main__":                                # pragma: no cover
    import sys
    print(json.dumps(get_health_json(data_dir=sys.argv[1] if len(sys.argv) > 1
                                     else CFG.DATA_DIR),
                     indent=2, ensure_ascii=False, default=str))
