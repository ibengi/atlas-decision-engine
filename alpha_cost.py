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
import uuid
from datetime import datetime, timezone

from config import CFG, _p
from durable_append import serialized_append, sync_path

log = logging.getLogger("ALPHA")

PRICING_SCHEMA = "atlas-alpha-pricing-v2"
BUDGET_LEDGER_FILE = "alpha_budget_ledger.jsonl"

REASON_UNPRICED = "pricing_unconfigured"
REASON_BUDGET = "budget_exhausted"
REASON_EXPIRED = "pricing_expired"


def _now() -> float:
    return time.time()


def _iso(ts: float = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  timezone.utc).isoformat(timespec="seconds")


def _finite(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)))
    except (OverflowError, ValueError, TypeError):
        return False


def _parse_utc(value):
    """A UTC datetime, or None. A malformed date is None, which makes the
    bound it was meant to express unenforceable -- so `PricingEntry.active`
    treats an unparseable window as INVALID rather than unbounded."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class PricingEntry:
    """One provider/model rate card with its provenance and validity window.

    Section 2 asks for the full provenance because a cost figure without it
    cannot be audited or recomputed: two entries for the same model with
    different `effective_from` are the normal case when a vendor changes
    prices, and the ledger has to be able to say which one produced a given
    historical row.
    """

    FIELDS = ("provider", "model", "input_per_mtok", "cached_input_per_mtok",
              "output_per_mtok", "tool_call_usd", "search_query_usd",
              "currency", "effective_from", "effective_until", "source",
              "notes")

    def __init__(self, raw: dict, version: str):
        self.raw = dict(raw or {})
        self.version = version
        self.provider = str(self.raw.get("provider") or "")
        self.model = str(self.raw.get("model") or "")
        self.currency = str(self.raw.get("currency") or "USD")
        self.input_per_mtok = self.raw.get("input_per_mtok")
        self.cached_input_per_mtok = self.raw.get("cached_input_per_mtok")
        self.output_per_mtok = self.raw.get("output_per_mtok")
        self.tool_call_usd = self.raw.get("tool_call_usd")
        self.search_query_usd = self.raw.get("search_query_usd")
        self.source = str(self.raw.get("source") or "")
        self.effective_from_raw = self.raw.get("effective_from")
        self.effective_until_raw = self.raw.get("effective_until")
        self.effective_from = _parse_utc(self.effective_from_raw)
        self.effective_until = _parse_utc(self.effective_until_raw)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def rates_present(self) -> bool:
        return _finite(self.input_per_mtok) and _finite(self.output_per_mtok)

    def window_problem(self, at: datetime):
        """Why this entry does not apply at `at`, or None."""
        if self.effective_from_raw and self.effective_from is None:
            return f"effective_from {self.effective_from_raw!r} is unreadable"
        if self.effective_until_raw and self.effective_until is None:
            return f"effective_until {self.effective_until_raw!r} is unreadable"
        if self.effective_from and at < self.effective_from:
            return (f"rate is not effective until "
                    f"{self.effective_from.isoformat()}")
        if self.effective_until and at > self.effective_until:
            # Section 1: a rate whose window has closed is NOT the current
            # rate. Using it would quietly price today at last year's
            # numbers; refusing makes the operator add the next card.
            return (f"rate expired on {self.effective_until.isoformat()}; "
                    f"add the successor entry before calling this model")
        return None

    def active(self, at: datetime) -> bool:
        return self.rates_present and self.window_problem(at) is None

    def provenance(self) -> dict:
        return {"pricing_version": self.version,
                "pricing_source": self.source,
                "currency": self.currency,
                "input_per_mtok": self.input_per_mtok,
                "cached_input_per_mtok": self.cached_input_per_mtok,
                "output_per_mtok": self.output_per_mtok,
                "tool_call_usd": self.tool_call_usd,
                "search_query_usd": self.search_query_usd,
                "effective_from": self.effective_from_raw,
                "effective_until": self.effective_until_raw}

    def cost(self, *, input_tokens: int, output_tokens: int,
             cached_input_tokens: int = 0, tool_calls: int = 0,
             search_queries: int = 0) -> float:
        billable_input = max(0, int(input_tokens) - int(cached_input_tokens))
        cached_rate = (float(self.cached_input_per_mtok)
                       if _finite(self.cached_input_per_mtok)
                       else float(self.input_per_mtok))
        usd = ((billable_input / 1e6) * float(self.input_per_mtok)
               + (int(cached_input_tokens) / 1e6) * cached_rate
               + (int(output_tokens) / 1e6) * float(self.output_per_mtok))
        if _finite(self.tool_call_usd):
            usd += int(tool_calls) * float(self.tool_call_usd)
        if _finite(self.search_query_usd):
            usd += int(search_queries) * float(self.search_query_usd)
        return round(usd, 10)


class PricingTable:
    """Time-aware rate cards with provenance (section 2).

    A model may have several entries; the one whose validity window contains
    *now* is used. When none does -- because the rates are absent, or because
    every card for that model has expired -- the model is UNPRICED and the
    budget guard refuses to call it.
    """

    def __init__(self, path: str = None):
        self.path = path or self._default_path()
        self.version = ""
        self.entries = []
        self.loaded = False
        self.error = None
        self.load()

    @staticmethod
    def _default_path() -> str:
        configured = CFG.ALPHA_PRICING_FILE
        if os.path.isabs(configured):
            return configured
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            configured)
        return repo if os.path.exists(repo) else _p(configured)

    def load(self) -> None:
        self.entries, self.loaded, self.error = [], False, None
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
                          f"{(payload or {}).get('schema')!r} "
                          f"(expected {PRICING_SCHEMA})")
            return
        rows = payload.get("entries")
        if not isinstance(rows, list):
            self.error = "pricing file has no 'entries' list"
            return
        self.version = str(CFG.ALPHA_PRICING_VERSION
                           or payload.get("version") or "unversioned")
        for row in rows:
            if isinstance(row, dict):
                self.entries.append(PricingEntry(row, self.version))
        self.loaded = True

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def entries_for(self, provider: str, model: str) -> list:
        return [e for e in self.entries
                if e.provider == provider and e.model == model]

    def active_entry(self, provider: str, model: str, at: datetime = None):
        """(entry, problem). Exactly one of the two is None."""
        at = at or datetime.now(timezone.utc)
        candidates = self.entries_for(provider, model)
        if not candidates:
            return None, (self.error
                          or f"no pricing entry for "
                             f"{self.key(provider, model)}")
        active = [e for e in candidates if e.active(at)]
        if not active:
            problems = []
            for entry in candidates:
                if not entry.rates_present:
                    problems.append("rates are null (unknown, not free)")
                else:
                    problems.append(entry.window_problem(at) or "not active")
            return None, (f"no active rate for {self.key(provider, model)}: "
                          + "; ".join(sorted(set(problems))))
        # Most recently effective card wins when several overlap.
        active.sort(key=lambda e: (e.effective_from or datetime.min.replace(
            tzinfo=timezone.utc)))
        return active[-1], None

    def price(self, provider: str, model: str, input_tokens: int,
              output_tokens: int, *, cached_input_tokens: int = 0,
              tool_calls: int = 0, search_queries: int = 0,
              at: datetime = None) -> dict:
        """Cost the usage. Always returns a row; `cost_priced` says whether
        the number means anything."""
        entry, problem = self.active_entry(provider, model, at)
        row = {
            "provider": provider, "model": model,
            "input_tokens": int(input_tokens),
            "cached_input_tokens": int(cached_input_tokens),
            "output_tokens": int(output_tokens),
            "tool_calls": int(tool_calls),
            "search_queries": int(search_queries),
            "api_cost_usd": 0.0,
            "cost_priced": False,
            "pricing_version": "",
            "pricing_asof": "",
            "priced_at": "",
            "pricing_missing_reason": problem,
        }
        if entry is None:
            return row
        row.update(entry.provenance())
        row.update({
            "api_cost_usd": round(entry.cost(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                tool_calls=tool_calls, search_queries=search_queries), 10),
            "cost_priced": True,
            "pricing_asof": entry.effective_from_raw or "",
            "priced_at": _iso(),
            "pricing_missing_reason": None,
        })
        return row

    def estimate(self, provider: str, model: str, *,
                 prompt_chars: int = None) -> dict:
        """The WORST-CASE pre-call estimate the budget is checked against.

        Section 5 says a call whose worst-case cost exceeds the remaining
        budget must be refused BEFORE dispatch, so this deliberately
        over-estimates: the real prompt length when it is known (at four
        characters per token, with a margin), and the configured maximum
        output rather than a typical one. Under-estimating here would let a
        cap be breached by exactly the calls it exists to stop.
        """
        if prompt_chars:
            input_tokens = int(prompt_chars / 4 * 1.25) + 256
        else:
            input_tokens = int(CFG.ALPHA_ESTIMATED_INPUT_TOKENS)
        row = self.price(provider, model, input_tokens,
                         int(CFG.ALPHA_MAX_OUTPUT_TOKENS))
        row["worst_case"] = True
        return row

    def configured_models(self, at: datetime = None) -> dict:
        at = at or datetime.now(timezone.utc)
        return {e.key: e.provenance() for e in self.entries if e.active(at)}

    def expired_models(self, at: datetime = None) -> dict:
        at = at or datetime.now(timezone.utc)
        out = {}
        for entry in self.entries:
            if entry.rates_present and not entry.active(at):
                out[entry.key] = entry.window_problem(at)
        return out


def reconcile_billed_cost(cost_row: dict, billed: dict) -> dict:
    """Fold a provider-reported billed cost into a usage row.

    Section 1: when a vendor supplies an authoritative billed amount, the
    token estimate must not be the only figure of record. Both are kept --
    `api_cost_usd` (ours, from the rate card) and `billed_cost_usd` (theirs)
    -- and a disagreement beyond a small tolerance is FLAGGED rather than
    resolved silently in either direction. Silently preferring theirs would
    hide a rate-card error; silently preferring ours would hide a billing
    surprise.
    """
    row = dict(cost_row)
    row.update({k: v for k, v in billed.items() if v is not None})
    ours = row.get("api_cost_usd")
    theirs = row.get("billed_cost_usd")
    row["cost_reconciled"] = False
    row["cost_reconciliation"] = None
    if not (_finite(ours) and _finite(theirs)):
        return row
    ours, theirs = float(ours), float(theirs)
    row["cost_reconciled"] = True
    if max(ours, theirs) <= 0:
        return row
    drift = abs(ours - theirs) / max(ours, theirs)
    if drift > float(CFG.ALPHA_COST_RECONCILE_TOLERANCE):
        row["cost_reconciliation"] = (
            f"estimated ${ours:.8f} vs billed ${theirs:.8f} "
            f"({drift * 100:.1f}% apart): the rate card and the vendor "
            f"disagree; check the card before trusting either")
        log.warning(f"[ALPHA_COST] {row.get('provider')}/{row.get('model')}: "
                    f"{row['cost_reconciliation']}")
    return row


def budgeted_cost(cost_row: dict) -> float:
    """The figure charged against the budget.

    The vendor's own billed amount when it supplied one, because that is
    what will appear on the invoice; our estimate otherwise.
    """
    billed = cost_row.get("billed_cost_usd")
    if _finite(billed):
        return float(billed)
    value = cost_row.get("api_cost_usd")
    return float(value) if _finite(value) else 0.0


BUDGET_EVENT_SCHEMA = "atlas-alpha-budget-v1"


def _budget_text(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_budget_row(row):
    """Validate before date filtering. Legacy usage remains immutable/readable.

    Refunds/corrections have no schema here and cannot silently reduce spend.
    """
    if not isinstance(row, dict):
        raise RuntimeError("budget row must be an object")
    if not _finite(row.get("ts")) or row["ts"] < 0:
        raise RuntimeError("budget row has an invalid timestamp")
    try:
        datetime.fromtimestamp(row["ts"], timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise RuntimeError("budget timestamp is outside the supported UTC range")
    if not _budget_text(row.get("provider")):
        raise RuntimeError("budget row has no provider identity")
    if not _finite(row.get("api_cost_usd")) or row["api_cost_usd"] < 0:
        raise RuntimeError("budget row has no nonnegative finite cost")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                "tool_calls", "search_queries", "latency_ms"):
        if key in row and (type(row[key]) is not int or row[key] < 0):
            raise RuntimeError(f"budget row has an invalid {key}")
    for key in ("estimated_cost_usd", "billed_cost_usd"):
        if key in row and row[key] is not None and (
                not _finite(row[key]) or row[key] < 0):
            raise RuntimeError(f"budget row has an invalid {key}")
    billed = row.get("billed_cost_usd")
    if billed is not None and billed != row["api_cost_usd"]:
        raise RuntimeError("effective budget cost disagrees with vendor billed cost")
    if "cost_source" in row:
        source = row["cost_source"]
        if source not in ("vendor_billed", "rate_card_estimate"):
            raise RuntimeError("unknown budget cost source")
        if source == "vendor_billed" and billed is None:
            raise RuntimeError("vendor billed cost source has no vendor amount")
        if source == "rate_card_estimate" and billed is not None:
            raise RuntimeError("rate card source contradicts vendor billing evidence")
    if "cost_priced" in row and type(row["cost_priced"]) is not bool:
        raise RuntimeError("budget row cost_priced must be a boolean")
    if "model" in row and not _budget_text(row["model"]):
        raise RuntimeError("budget row has an invalid model identity")
    if "at" in row:
        at = row["at"]
        if not isinstance(at, str) or not at.strip():
            raise RuntimeError("budget row has an invalid ISO timestamp")
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or abs(parsed.timestamp() - row["ts"]) > 1.1:
                raise ValueError("timestamp mismatch")
        except (ValueError, OverflowError, OSError):
            raise RuntimeError("budget row ISO and numeric timestamps disagree")
    if "schema" not in row and "event" not in row:
        if any(key in row for key in ("reservation_id", "owner_id", "owner_pid",
                                      "reservation_group")):
            raise RuntimeError("untagged budget event cannot become legacy usage")
        return
    if row.get("schema") != BUDGET_EVENT_SCHEMA:
        raise RuntimeError("unknown budget event schema")
    if row.get("event") not in ("RESERVATION", "USAGE"):
        raise RuntimeError("unknown budget event")
    for key in ("reservation_id", "owner_id", "reservation_group", "model"):
        if not _budget_text(row.get(key)):
            raise RuntimeError(f"budget event has no {key}")
    if type(row.get("owner_pid")) is not int or row["owner_pid"] <= 0:
        raise RuntimeError("budget event has an invalid process identity")


class BudgetLedger:
    """Append-only usage and durable pre-dispatch obligations.

    RESERVATION (durable before dispatch) -> USAGE (durable completion).
    Readability is not durability: reconstruction performs a real file and
    directory barrier under the append lock. Failed/absent usage leaves the
    reservation unresolved across restart. Historical rows are never rewritten.
    """

    def __init__(self, path: str = None):
        self.path = path or _p(BUDGET_LEDGER_FILE)
        self._lock = threading.RLock()
        self._observed_exists = False

    @staticmethod
    def _entry(row):
        now = _now()
        entry = {"at": _iso(now), "ts": now, **row}
        _validate_budget_row(entry)
        return entry

    @staticmethod
    def _line(entry):
        return json.dumps(entry, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n"

    def record(self, row: dict) -> dict:
        """Explicit historical usage import, outside provider dispatch."""
        entry = self._entry(row)
        with self._lock:
            with serialized_append(self.path) as append:
                append(self._line(entry))
                self._observed_exists = True
        return entry

    def _rows_locked(self):
        try:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    initial = os.fstat(fh.fileno())
                    raw = fh.read()
                    final = os.fstat(fh.fileno())
            except FileNotFoundError:
                try:
                    os.stat(self.path)
                except FileNotFoundError:
                    if self._observed_exists:
                        raise RuntimeError("previously observed budget history disappeared")
                    return []
                raise RuntimeError("budget history metadata is inconsistent")
            self._observed_exists = True
            # Synchronize the observed generation, then ensure that neither
            # the read nor the barrier interval admitted different bytes.
            # Capturing generation only AFTER a barrier could promote bytes
            # appended after that barrier to confirmed accounting.
            if not sync_path(self.path):
                raise RuntimeError("budget history disappeared before synchronization")
            observed = os.stat(self.path)
            generation = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            if generation(initial) != generation(final) or generation(final) != generation(observed):
                raise RuntimeError("budget history changed during synchronized read")
        except (OSError, TimeoutError, UnicodeError) as exc:
            raise RuntimeError(f"budget ledger durability is unknown: {exc}") from exc
        if raw and not raw.endswith("\n"):
            raise RuntimeError("budget ledger has an incomplete tail")
        out = []
        for i, line in enumerate(raw.splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                _validate_budget_row(row)
            except (ValueError, TypeError, RuntimeError) as exc:
                raise RuntimeError(f"budget ledger row {i + 1} is invalid: {exc}") from exc
            out.append(row)
        self._state(out)
        return out

    @staticmethod
    def _state(rows):
        reservations, completions, legacy = {}, {}, []
        identity = ("provider", "model", "owner_id", "owner_pid", "reservation_group")
        for row in rows:
            if "event" not in row:
                legacy.append(row)
                continue
            rid = row["reservation_id"]
            if row["event"] == "RESERVATION":
                if rid in reservations:
                    raise RuntimeError("duplicate budget reservation")
                reservations[rid] = row
            else:
                reserved = reservations.get(rid)
                if reserved is None or any(row[key] != reserved[key] for key in identity):
                    raise RuntimeError("budget completion does not bind its reservation")
                if rid in completions:
                    raise RuntimeError("duplicate budget completion")
                if row["ts"] < reserved["ts"]:
                    raise RuntimeError("budget usage predates its reservation")
                completions[rid] = row
        pending = {rid: row for rid, row in reservations.items() if rid not in completions}
        return pending, legacy + list(completions.values()) + list(pending.values())

    def rows(self, since_ts: float = None) -> list:
        with self._lock:
            with serialized_append(self.path):
                rows = self._rows_locked()
        return [row for row in rows if since_ts is None or row["ts"] >= since_ts]

    @staticmethod
    def _total(rows, cutoff, provider=None):
        _, charges = BudgetLedger._state(rows)
        total = sum(float(row["api_cost_usd"]) for row in charges
                    if row["ts"] >= cutoff and (provider is None or row["provider"] == provider))
        if not math.isfinite(total):
            raise RuntimeError("budget total is not finite")
        return round(total, 8)

    def spent(self, *, window_s: float, provider: str = None) -> float:
        return self._total(self.rows(), _now() - float(window_s), provider)

    def spent_today(self) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0).timestamp()
        return self._total(self.rows(), start)


class BudgetGuard:
    """Decides, BEFORE any provider call, whether it may be made."""

    def __init__(self, pricing: PricingTable = None,
                 ledger: BudgetLedger = None):
        self.pricing = pricing or PricingTable()
        self.ledger = ledger or BudgetLedger()
        #: RA-06. Set when money was spent and the row recording it could NOT
        #: be made durable. Sticky for the life of the process on purpose:
        #: the spend really happened, the ledger really does not know about
        #: it, and every total computed from that ledger is understated until
        #: an operator reconciles. Clearing it on the next successful write
        #: would hide exactly the gap it exists to report.
        self.accounting_uncertain = ""
        self._owner_id = uuid.uuid4().hex
        self._owner_pid = os.getpid()
        self._reservations = {}
        self._uncertain_reservations = set()

    def check(self, provider: str, model: str, *,
              analysis_spent_usd: float = 0.0,
              prompt_chars: int = None) -> dict:
        """`{"allowed": bool, "reason": str|None, "estimate": {...}, ...}`.

        Every refusal names which cap was hit and what the numbers were, so
        an operator reading a day of BUDGET_EXHAUSTED signals can tell a
        misconfigured cap from a genuinely expensive day.
        """
        estimate = self.pricing.estimate(provider, model,
                                         prompt_chars=prompt_chars)
        result = {"allowed": True, "reason": None, "detail": "",
                  "estimate": estimate,
                  "estimated_cost_usd": estimate["api_cost_usd"]}

        if not estimate["cost_priced"]:
            if not CFG.ALPHA_ALLOW_UNPRICED_CALLS:
                reason = (REASON_EXPIRED
                          if "expired" in str(estimate["pricing_missing_reason"])
                          else REASON_UNPRICED)
                result.update(
                    allowed=False, reason=reason,
                    detail=(f"{estimate['pricing_missing_reason']}; refusing "
                            f"to call an unpriced model because every cost cap "
                            f"would be unenforceable against a zero estimate. "
                            f"Set rates in {self.pricing.path} or set "
                            f"ALPHA_ALLOW_UNPRICED_CALLS."))
                return result
            log.warning(f"[ALPHA_BUDGET] calling UNPRICED {provider}/{model}: "
                        f"cost caps cannot bind this call")

        try:
            rows = self.ledger.rows()
            pending, _ = self.ledger._state(rows)
            if pending:
                raise RuntimeError("unresolved pre-dispatch accounting reservation")
            if self.accounting_uncertain.startswith("unreserved"):
                raise RuntimeError(self.accounting_uncertain)
            self.accounting_uncertain = ""
            spent_hour = self.ledger._total(rows, _now() - 3600.0, provider)
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                       microsecond=0).timestamp()
            spent_day = self.ledger._total(rows, start)
        except (RuntimeError, OSError, TimeoutError) as e:
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

    def reserve(self, provider: str, model: str, *,
                analysis_spent_usd: float = 0.0, prompt_chars: int = None,
                reservation_group: str = None) -> dict:
        """Atomic admission + durable obligation, BEFORE any provider call.

        Only this guard's live process can add parallel reservations to the
        same analysis. An orphan, timeout, fork or prior analysis blocks new
        admission. Estimated pending usage consumes the shared caps.
        """
        group = reservation_group or uuid.uuid4().hex
        estimate = self.pricing.estimate(provider, model, prompt_chars=prompt_chars)
        result = {"allowed": False, "reason": REASON_BUDGET, "detail": "",
                  "estimate": estimate, "estimated_cost_usd": estimate.get("api_cost_usd", 0.0)}
        try:
            if self._owner_pid != os.getpid():
                raise RuntimeError("budget reservation authority does not survive fork")
            if self.accounting_uncertain.startswith("unreserved"):
                raise RuntimeError(self.accounting_uncertain)
            if not all(_budget_text(v) for v in (provider, model, group)):
                raise RuntimeError("invalid budget reservation identity")
            if not estimate.get("cost_priced") and not CFG.ALPHA_ALLOW_UNPRICED_CALLS:
                reason = (REASON_EXPIRED if "expired" in str(estimate.get("pricing_missing_reason"))
                          else REASON_UNPRICED)
                result.update(reason=reason, detail="pricing_unconfigured; no provider call is made")
                return result
            estimated = estimate.get("api_cost_usd")
            if not _finite(estimated) or estimated < 0 or not _finite(analysis_spent_usd) or analysis_spent_usd < 0:
                raise RuntimeError("provider cost cannot be bounded")
            with self.ledger._lock:
                with serialized_append(self.ledger.path) as append:
                    rows = self.ledger._rows_locked()
                    pending, charges = self.ledger._state(rows)
                    self._uncertain_reservations.intersection_update(pending)
                    if self._uncertain_reservations:
                        raise RuntimeError("usage accounting remains unresolved")
                    group_spend = sum(float(row["api_cost_usd"]) for row in charges
                                      if row.get("reservation_group") == group)
                    for rid, row in pending.items():
                        if (rid not in self._reservations or row["owner_id"] != self._owner_id
                                or row["owner_pid"] != self._owner_pid
                                or row["reservation_group"] != group):
                            raise RuntimeError("unresolved pre-dispatch accounting reservation")
                        if row["provider"] == provider and row["model"] == model:
                            raise RuntimeError("duplicate unresolved provider reservation")
                    hourly = self.ledger._total(rows, _now() - 3600.0, provider)
                    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                               microsecond=0).timestamp()
                    daily = self.ledger._total(rows, start)
                    for name, cap, projected in (
                        ("per-analysis", CFG.ALPHA_MAX_COST_PER_ANALYSIS_USD,
                         max(analysis_spent_usd, group_spend) + estimated),
                        (f"{provider} hourly", CFG.ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD, hourly + estimated),
                        ("daily", CFG.ALPHA_MAX_COST_PER_DAY_USD, daily + estimated)):
                        if not _finite(projected):
                            raise RuntimeError("projected budget total is not finite")
                        if not _finite(cap) or cap < 0:
                            raise RuntimeError("invalid budget cap")
                        if cap > 0 and projected > cap:
                            result["detail"] = f"{name} cap would be exceeded; no provider call is made"
                            return result
                    rid = uuid.uuid4().hex
                    entry = self.ledger._entry({
                        "schema": BUDGET_EVENT_SCHEMA, "event": "RESERVATION",
                        "reservation_id": rid, "provider": provider, "model": model,
                        "owner_id": self._owner_id, "owner_pid": self._owner_pid,
                        "reservation_group": group, "api_cost_usd": estimated})
                    append(self.ledger._line(entry))
                    self.ledger._observed_exists = True
                    self._reservations[rid] = entry
                    result.update(allowed=True, reason=None, reservation_id=rid,
                                  spent_hour_usd=hourly, spent_today_usd=daily)
        except (RuntimeError, OSError, TimeoutError, TypeError, ValueError) as exc:
            result["detail"] = f"budget reservation refused: {exc}"
        return result

    def record_actual(self, cost_row: dict, *, reservation_id: str = None) -> dict:
        """Complete an exact durable reservation; never create one after spend.

        Unreserved usage is refused by this API. Historical imports use
        BudgetLedger.record explicitly, outside provider dispatch. A failed
        completion leaves the already-durable obligation outstanding; even
        a zero-byte write and immediate process death cannot erase it.
        """
        if (self._owner_pid != os.getpid() or not reservation_id
                or reservation_id not in self._reservations):
            self.accounting_uncertain = "unreserved actual usage refused: durable pre-dispatch reservation required"
            return {"recorded": False, "reason": "reservation_required",
                    "detail": self.accounting_uncertain}
        try:
            reserved = self._reservations[reservation_id]
            if not isinstance(cost_row, dict) or any(cost_row.get(key) != reserved[key]
                                                     for key in ("provider", "model")):
                raise RuntimeError("usage identity does not match reservation")
            amount = cost_row.get("billed_cost_usd")
            if amount is None:
                amount = cost_row.get("api_cost_usd")
            if not _finite(amount) or amount < 0:
                raise RuntimeError("actual usage has no known nonnegative cost")
            payload = {key: value for key, value in cost_row.items()
                       if key not in ("at", "ts")}
            payload.update({key: reserved[key] for key in (
                "schema", "reservation_id", "owner_id", "owner_pid", "reservation_group")})
            payload.update(event="USAGE", api_cost_usd=amount,
                           estimated_cost_usd=cost_row.get("api_cost_usd"),
                           cost_source="vendor_billed" if cost_row.get("billed_cost_usd") is not None
                           else "rate_card_estimate")
            entry = self.ledger._entry(payload)
            with self.ledger._lock:
                with serialized_append(self.ledger.path) as append:
                    rows = self.ledger._rows_locked()
                    prior = next((row for row in rows if row.get("event") == "USAGE"
                                  and row.get("reservation_id") == reservation_id), None)
                    if prior is not None:
                        comparable = lambda row: {k: v for k, v in row.items() if k not in ("at", "ts")}
                        if comparable(prior) != comparable(entry):
                            raise RuntimeError("conflicting duplicate usage completion")
                        return {"recorded": True, "reservation_id": reservation_id, "recovered": True}
                    pending, _ = self.ledger._state(rows)
                    if pending.get(reservation_id) != reserved:
                        raise RuntimeError("durable reservation is missing or changed")
                    if entry["ts"] < reserved["ts"]:
                        raise RuntimeError("usage timestamp precedes reservation")
                    append(self.ledger._line(entry))
                    self.accounting_uncertain = ""
                    self._uncertain_reservations.discard(reservation_id)
            return {"recorded": True, "reservation_id": reservation_id}
        except (RuntimeError, OSError, TimeoutError, ValueError, TypeError) as exc:
            self._uncertain_reservations.add(reservation_id)
            self.accounting_uncertain = f"{type(exc).__name__}: {exc}"
            return {"recorded": False, "reason": "accounting_uncertain",
                    "detail": self.accounting_uncertain}

    def snapshot(self) -> dict:
        try:
            rows = self.ledger.rows()
            pending, _ = self.ledger._state(rows)
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                       microsecond=0).timestamp()
            spent = self.ledger._total(rows, start)
            uncertain = bool(pending or self.accounting_uncertain.startswith("unreserved"))
            daily_cap = float(CFG.ALPHA_MAX_COST_PER_DAY_USD)
            return {"spent_today_usd": spent,
                    "accounting_uncertain": uncertain,
                    "unresolved_reservations": len(pending),
                    "exhausted": uncertain or (daily_cap > 0 and spent >= daily_cap),
                    "caps": {
                        "per_analysis_usd": float(CFG.ALPHA_MAX_COST_PER_ANALYSIS_USD),
                        "provider_hourly_usd": float(CFG.ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD),
                        "daily_usd": float(CFG.ALPHA_MAX_COST_PER_DAY_USD)},
                    "pricing_version": self.pricing.version,
                    "priced_models": sorted(self.pricing.configured_models()),
                    "expired_models": self.pricing.expired_models(),
                    "pricing_error": self.pricing.error}
        except (RuntimeError, OSError, TimeoutError) as e:
            return {"error": str(e), "exhausted": True,
                    "accounting_uncertain": True}


def recost(rows, pricing: PricingTable) -> dict:
    """Re-cost historical usage under a different pricing table.

    Section 4's requirement that today's prices must not be baked into
    yesterday's conclusions. Returns both totals and the rows that could not
    be re-costed, rather than quietly treating them as free.
    """
    total, unpriced = 0.0, []
    for row in rows:
        priced = pricing.price(
            row.get("provider", ""), row.get("model", ""),
            int(row.get("input_tokens") or 0),
            int(row.get("output_tokens") or 0),
            cached_input_tokens=int(row.get("cached_input_tokens") or 0),
            tool_calls=int(row.get("tool_calls") or 0),
            search_queries=int(row.get("search_queries") or 0))
        if priced["cost_priced"]:
            total += priced["api_cost_usd"]
        else:
            unpriced.append(PricingTable.key(row.get("provider", ""),
                                             row.get("model", "")))
    return {"pricing_version": pricing.version,
            "total_usd": round(total, 8), "rows": len(rows),
            "unpriced_models": sorted(set(unpriced)),
            "complete": not unpriced}
