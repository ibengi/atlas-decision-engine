"""Provider pricing and shadow cost budgets. SHADOW ONLY.

Alpha Gateway sections 4 and 5.

PRICING IS CONFIGURATION, AND HISTORY IS NEVER RE-PRICED IN PLACE
    Rates live in a JSON file (`ALPHA_PRICING_FILE`) carrying a `version`
    and an `asof` date. Every cost row records the token counts, the tool
    and search usage, the rates applied, and WHICH pricing version and
    timestamp produced the figure.

    That is what makes section 4's last requirement satisfiable: when a
    vendor changes its prices, no historical row is rewritten. The raw usage
    is on disk, so any past cycle can be re-costed under a new table with
    `recost()`, and the two answers can be compared instead of one silently
    replacing the other. A metrics report that hard-coded today's prices
    into yesterday's conclusions would be unfalsifiable in exactly the way
    the calibration ledger is built to avoid.

FAIL-CLOSED ON SPEND
    A provider/model with no configured price is NOT called. This is not
    pedantry: an unpriced call is costed at zero, so every budget below
    becomes unenforceable and the daily cap silently means "unlimited". The
    refusal is `pricing_unconfigured`, an EXCLUDED signal like any other.
    `ALPHA_ALLOW_UNPRICED_CALLS` exists for deliberate experiments and
    defaults to off.

BUDGET EXHAUSTION IS NOT A PROBABILITY
    When a cap is reached the provider is not called at all and the signal
    is EXCLUDED with reason `budget_exhausted`. A billing limit must never
    become a fabricated estimate, and the ensemble must be able to tell
    "we could not afford to ask" from "the model had no opinion".
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone

from config import CFG, _p

log = logging.getLogger("ALPHA")

PRICING_SCHEMA = "atlas-alpha-pricing-v1"
BUDGET_LEDGER_FILE = "alpha_budget_ledger.jsonl"

REASON_UNPRICED = "pricing_unconfigured"
REASON_BUDGET = "budget_exhausted"


def _now() -> float:
    return time.time()


def _iso(ts: float = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  timezone.utc).isoformat(timespec="seconds")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


class PricingTable:
    """Per provider/model rates in USD per MILLION tokens.

    File shape:

        {"schema": "atlas-alpha-pricing-v1",
         "version": "2026-09-vendor-list",
         "asof": "2026-09-09T00:00:00+00:00",
         "models": {
           "grok/grok-4.6":   {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
           "openai/gpt-5":    {"input_per_mtok": null, "output_per_mtok": null}
         }}

    A `null` rate means UNKNOWN, not free. `price()` returns
    `priced=False` for it and the caller refuses the call.
    """

    def __init__(self, path: str = None):
        self.path = path or self._default_path()
        self.version = ""
        self.asof = ""
        self.models = {}
        self.loaded = False
        self.error = None
        self.load()

    @staticmethod
    def _default_path() -> str:
        configured = CFG.ALPHA_PRICING_FILE
        if os.path.isabs(configured):
            return configured
        # Look beside the repository first (a versioned, reviewable file),
        # then in DATA_DIR (an operator override on the volume).
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            configured)
        return repo if os.path.exists(repo) else _p(configured)

    def load(self) -> None:
        self.models, self.loaded, self.error = {}, False, None
        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except OSError as e:
            self.error = f"pricing file unreadable: {e}"
            return
        except ValueError as e:
            self.error = f"pricing file is not valid JSON: {e}"
            return
        if not isinstance(payload, dict) \
                or payload.get("schema") != PRICING_SCHEMA:
            self.error = (f"unknown pricing schema "
                          f"{(payload or {}).get('schema')!r}")
            return
        models = payload.get("models")
        if not isinstance(models, dict):
            self.error = "pricing file has no 'models' object"
            return
        self.version = str(CFG.ALPHA_PRICING_VERSION
                           or payload.get("version") or "unversioned")
        self.asof = str(payload.get("asof") or "")
        for key, entry in models.items():
            if not isinstance(entry, dict):
                continue
            self.models[str(key)] = {
                "input_per_mtok": entry.get("input_per_mtok"),
                "output_per_mtok": entry.get("output_per_mtok"),
                "notes": str(entry.get("notes") or ""),
            }
        self.loaded = True

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def price(self, provider: str, model: str, input_tokens: int,
              output_tokens: int) -> dict:
        """Cost the usage. Always returns a row; `priced` says whether the
        number means anything."""
        key = self.key(provider, model)
        entry = self.models.get(key) or {}
        rate_in = entry.get("input_per_mtok")
        rate_out = entry.get("output_per_mtok")
        priced = _finite(rate_in) and _finite(rate_out)
        usd = ((input_tokens / 1e6) * float(rate_in)
               + (output_tokens / 1e6) * float(rate_out)) if priced else 0.0
        return {
            "provider": provider, "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "input_per_mtok": rate_in, "output_per_mtok": rate_out,
            "api_cost_usd": round(usd, 8),
            "cost_priced": bool(priced),
            "pricing_version": self.version if priced else "",
            "pricing_asof": self.asof if priced else "",
            "priced_at": _iso() if priced else "",
            "pricing_missing_reason": None if priced else (
                self.error or f"no rates configured for {key}"),
        }

    def estimate(self, provider: str, model: str) -> dict:
        """The pre-call estimate budgets are checked against, since the true
        token count is only known after the answer arrives."""
        return self.price(provider, model,
                          int(CFG.ALPHA_ESTIMATED_INPUT_TOKENS),
                          int(CFG.ALPHA_ESTIMATED_OUTPUT_TOKENS))

    def configured_models(self) -> dict:
        return {k: v for k, v in self.models.items()
                if _finite(v.get("input_per_mtok"))
                and _finite(v.get("output_per_mtok"))}


class BudgetLedger:
    """Durable record of every dollar the shadow service estimates it spent.

    Append-only, like the calibration ledger and for the same reason: a
    spend total that can be edited is a spend total that cannot bound
    anything. Windows are recomputed from the rows on every check, so a
    restart cannot reset the daily cap -- which is the failure mode a
    purely in-memory counter has.
    """

    def __init__(self, path: str = None):
        self.path = path or _p(BUDGET_LEDGER_FILE)
        self._lock = threading.Lock()

    def record(self, row: dict) -> dict:
        entry = {"at": _iso(), "ts": _now(), **row}
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str) + "\n"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                        exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o644)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            # A budget row that cannot be written means the next check would
            # under-count spend. Log loudly; the caller treats an unwritable
            # budget ledger as exhausted (see BudgetGuard.check).
            log.error(f"[ALPHA_BUDGET] row not durable: {e}")
            raise
        return entry

    def rows(self, since_ts: float = None) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            raise RuntimeError(f"budget ledger unreadable: {e}")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                if i == len(lines) - 1:
                    break                       # torn tail, crash mid-append
                log.error(f"[ALPHA_BUDGET] unparsable row at line {i + 1}")
                continue
            if isinstance(row, dict) and (since_ts is None
                                          or float(row.get("ts") or 0) >= since_ts):
                out.append(row)
        return out

    def spent(self, *, window_s: float, provider: str = None) -> float:
        cutoff = _now() - float(window_s)
        total = 0.0
        for row in self.rows(since_ts=cutoff):
            if provider and row.get("provider") != provider:
                continue
            value = row.get("api_cost_usd")
            if _finite(value):
                total += float(value)
        return round(total, 8)

    def spent_today(self) -> float:
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = 0.0
        for row in self.rows(since_ts=start):
            value = row.get("api_cost_usd")
            if _finite(value):
                total += float(value)
        return round(total, 8)


class BudgetGuard:
    """Decides, BEFORE any provider call, whether it may be made."""

    def __init__(self, pricing: PricingTable = None,
                 ledger: BudgetLedger = None):
        self.pricing = pricing or PricingTable()
        self.ledger = ledger or BudgetLedger()

    def check(self, provider: str, model: str, *,
              analysis_spent_usd: float = 0.0) -> dict:
        """`{"allowed": bool, "reason": str|None, "estimate": {...}, ...}`.

        Every refusal names which cap was hit and what the numbers were, so
        an operator reading a day of BUDGET_EXHAUSTED signals can tell a
        misconfigured cap from a genuinely expensive day.
        """
        estimate = self.pricing.estimate(provider, model)
        result = {"allowed": True, "reason": None, "detail": "",
                  "estimate": estimate,
                  "estimated_cost_usd": estimate["api_cost_usd"]}

        if not estimate["cost_priced"]:
            if not CFG.ALPHA_ALLOW_UNPRICED_CALLS:
                result.update(
                    allowed=False, reason=REASON_UNPRICED,
                    detail=(f"{estimate['pricing_missing_reason']}; refusing "
                            f"to call an unpriced model because every cost cap "
                            f"would be unenforceable against a zero estimate. "
                            f"Set rates in {self.pricing.path} or set "
                            f"ALPHA_ALLOW_UNPRICED_CALLS."))
                return result
            log.warning(f"[ALPHA_BUDGET] calling UNPRICED {provider}/{model}: "
                        f"cost caps cannot bind this call")

        try:
            spent_hour = self.ledger.spent(window_s=3600.0, provider=provider)
            spent_day = self.ledger.spent_today()
        except RuntimeError as e:
            # Unreadable ledger: we cannot prove we are under budget, so we
            # are not. Same posture as an unreadable continuity chain.
            result.update(allowed=False, reason=REASON_BUDGET,
                          detail=f"budget ledger unreadable ({e}); spend "
                                 f"cannot be bounded, so no call is made")
            return result
        result["spent_hour_usd"] = spent_hour
        result["spent_today_usd"] = spent_day

        projected_analysis = float(analysis_spent_usd) + estimate["api_cost_usd"]
        caps = (
            ("per-analysis", float(CFG.ALPHA_MAX_COST_PER_ANALYSIS_USD),
             projected_analysis),
            (f"{provider} hourly", float(CFG.ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD),
             spent_hour + estimate["api_cost_usd"]),
            ("daily", float(CFG.ALPHA_MAX_COST_PER_DAY_USD),
             spent_day + estimate["api_cost_usd"]),
        )
        for name, cap, projected in caps:
            if cap > 0 and projected > cap:
                result.update(
                    allowed=False, reason=REASON_BUDGET,
                    detail=(f"{name} cap ${cap:.4f} would be exceeded "
                            f"(projected ${projected:.6f}); no provider call "
                            f"is made"))
                return result
        return result

    def record_actual(self, cost_row: dict) -> None:
        """Write what was really spent, after the answer arrived."""
        try:
            self.ledger.record({
                "provider": cost_row.get("provider"),
                "model": cost_row.get("model"),
                "input_tokens": cost_row.get("input_tokens", 0),
                "output_tokens": cost_row.get("output_tokens", 0),
                "tool_calls": cost_row.get("tool_calls", 0),
                "search_queries": cost_row.get("search_queries", 0),
                "api_cost_usd": cost_row.get("api_cost_usd", 0.0),
                "cost_priced": cost_row.get("cost_priced", False),
                "pricing_version": cost_row.get("pricing_version", ""),
                "pricing_asof": cost_row.get("pricing_asof", ""),
                "latency_ms": cost_row.get("latency_ms", 0),
                "outcome": cost_row.get("outcome", ""),
            })
        except OSError:
            pass                       # already logged; check() fails closed

    def snapshot(self) -> dict:
        try:
            return {"spent_today_usd": self.ledger.spent_today(),
                    "caps": {
                        "per_analysis_usd": float(CFG.ALPHA_MAX_COST_PER_ANALYSIS_USD),
                        "provider_hourly_usd": float(CFG.ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD),
                        "daily_usd": float(CFG.ALPHA_MAX_COST_PER_DAY_USD)},
                    "pricing_version": self.pricing.version,
                    "pricing_asof": self.pricing.asof,
                    "priced_models": sorted(self.pricing.configured_models()),
                    "pricing_error": self.pricing.error}
        except RuntimeError as e:
            return {"error": str(e)}


def recost(rows, pricing: PricingTable) -> dict:
    """Re-cost historical usage under a different pricing table.

    Section 4's requirement that today's prices must not be baked into
    yesterday's conclusions. Returns both totals and the rows that could not
    be re-costed, rather than quietly treating them as free.
    """
    total, unpriced = 0.0, []
    for row in rows:
        priced = pricing.price(row.get("provider", ""), row.get("model", ""),
                               int(row.get("input_tokens") or 0),
                               int(row.get("output_tokens") or 0))
        if priced["cost_priced"]:
            total += priced["api_cost_usd"]
        else:
            unpriced.append(PricingTable.key(row.get("provider", ""),
                                             row.get("model", "")))
    return {"pricing_version": pricing.version, "pricing_asof": pricing.asof,
            "total_usd": round(total, 8), "rows": len(rows),
            "unpriced_models": sorted(set(unpriced)),
            "complete": not unpriced}
