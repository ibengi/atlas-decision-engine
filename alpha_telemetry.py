"""Alpha Shadow Service counters. SHADOW ONLY.

Alpha Gateway phase 2, section 11. Every counter named there, plus the
budget totals that make a day of BUDGET_EXHAUSTED signals diagnosable.

Counters are cumulative for the life of the process and are also snapshotted
to `alpha_telemetry.json` so a monitor can read them without attaching to
the service. The file is written whole and atomically -- it is a gauge, not
a ledger, and losing it loses nothing that the calibration and budget
ledgers do not already hold durably.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

from config import CFG, _p

log = logging.getLogger("ALPHA")

#: Section 11's list, in its order, plus the ones an operator needs to read
#: the others (a `provider_invalid` count means little without knowing how
#: many calls were attempted).
COUNTERS = (
    "snapshots_received",
    "snapshots_deduplicated",
    "provider_calls",
    "provider_success",
    "provider_timeout",
    "provider_invalid",
    "provider_stale",
    "provider_budget_refused",
    "provider_unpriced_refused",
    "p_meta_generated",
    "no_edge",
    "positive_edge_low_confidence",
    "positive_edge_high_confidence",
    "market_moved",
    "model_disagreement",
    "insufficient_data",
    "analysis_timeout",
    "stale",
    "budget_exhausted",
    "catalyst_invalidated",
    "observations_recorded",
    "resolved_predictions",
    "cycles",
    "errors",
)


class Telemetry:
    """Thread-safe counters plus per-provider cost totals."""

    def __init__(self, path: str = None):
        self.path = path or _p(CFG.ALPHA_TELEMETRY_FILE)
        self._lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        self.counters = {name: 0 for name in COUNTERS}
        self.provider_cost_usd = {}
        self.provider_calls = {}
        self.last_error = None

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            if name not in self.counters:
                # An unknown counter is a bug in the caller, not a reason to
                # lose the number: record it and let it show up in the file.
                log.warning(f"[ALPHA_TELEMETRY] unknown counter {name!r}")
            self.counters[name] = self.counters.get(name, 0) + int(by)

    def add_cost(self, provider: str, usd: float, *, calls: int = 1) -> None:
        with self._lock:
            key = str(provider or "unknown")
            self.provider_cost_usd[key] = round(
                self.provider_cost_usd.get(key, 0.0) + float(usd or 0.0), 8)
            self.provider_calls[key] = self.provider_calls.get(key, 0) + calls

    def record_error(self, detail: str) -> None:
        with self._lock:
            self.counters["errors"] += 1
            self.last_error = {"at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"), "detail": str(detail)[:300]}

    def record_state(self, state: str) -> None:
        """Map a shadow terminal state onto its section-11 counter."""
        mapping = {
            "NO_EDGE": "no_edge",
            "POSITIVE_EDGE_LOW_CONFIDENCE": "positive_edge_low_confidence",
            "POSITIVE_EDGE_HIGH_CONFIDENCE": "positive_edge_high_confidence",
            "MARKET_MOVED": "market_moved",
            "MODEL_DISAGREEMENT": "model_disagreement",
            "INSUFFICIENT_DATA": "insufficient_data",
            "ANALYSIS_TIMEOUT": "analysis_timeout",
            "STALE": "stale",
            "BUDGET_EXHAUSTED": "budget_exhausted",
        }
        counter = mapping.get(state)
        if counter:
            self.incr(counter)

    def record_signal(self, signal) -> None:
        """One provider outcome -> the provider_* counters."""
        self.incr("provider_calls")
        cost = signal.cost or {}
        self.add_cost(signal.provider or cost.get("provider"),
                      cost.get("api_cost_usd") or 0.0)
        if signal.valid:
            self.incr("provider_success")
            return
        reason = signal.rejected_reason or ""
        if reason in ("analysis_timeout", "provider_timeout"):
            self.incr("provider_timeout")
        elif reason in ("stale", "late_response"):
            self.incr("provider_stale")
        elif reason == "budget_exhausted":
            self.incr("provider_budget_refused")
        elif reason == "pricing_unconfigured":
            self.incr("provider_unpriced_refused")
        else:
            self.incr("provider_invalid")

    def snapshot(self, extra: dict = None) -> dict:
        with self._lock:
            payload = {
                "schema": "atlas-alpha-telemetry-v1",
                "started_at": self.started_at,
                "generated_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                "uptime_s": None,
                **{name: self.counters.get(name, 0) for name in COUNTERS},
                "provider_cost_usd": dict(self.provider_cost_usd),
                "provider_calls_by_provider": dict(self.provider_calls),
                "last_error": self.last_error,
            }
        payload["provider_cost_usd_total"] = round(
            sum(payload["provider_cost_usd"].values()), 8)
        payload.update(extra or {})
        return payload

    def flush(self, extra: dict = None) -> dict:
        payload = self.snapshot(extra)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, sort_keys=True, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning(f"[ALPHA_TELEMETRY] snapshot not written: {e}")
        return payload
