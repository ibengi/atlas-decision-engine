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
from durable_append import serialized_append

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
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


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


class BudgetLedger:
    """Durable record of every dollar the shadow service estimates it spent.

    Append-only, like the calibration ledger and for the same reason: a
    spend total that can be edited is a spend total that cannot bound
    anything. Windows are recomputed from the rows on every check, so a
    restart cannot reset the daily cap -- which is the failure mode a
    purely in-memory counter has.

    RA-06 -- THE SAME DURABLE APPEND PROTOCOL AS EVERY OTHER LEDGER
        AA-12 and AA-14 were applied to two of the three append-only files in
        this subsystem. The calibration ledger learned the protocol; the
        processed store learned it in v3; this one kept its own `os.open` plus
        a SINGLE `os.write` plus `fsync`, with no short-write loop, no
        torn-tail separation and no writer lock -- in the one file every cost
        cap is enforced against.

        The consequences were not cosmetic:

          * `os.write` may write short. A truncated row read back as a torn
            tail, which `rows()` treated as the end of the file.
          * with no torn-tail separation, the NEXT row was spliced onto the
            broken one, so both became one unparsable line -- and because it
            was the LAST line, `rows()` `break`-ed and silently dropped the
            spend. The daily cap was then enforced against a number that was
            too small.
          * two writers could interleave the check-then-append the torn-tail
            test performs.

        Under-counting spend is not a conservative failure: it is the one
        direction in which a budget guard stops guarding. So this now goes
        through `durable_append.serialized_append`, and `rows()` refuses to
        return a total it cannot defend.
    """

    def __init__(self, path: str = None):
        self.path = path or _p(BUDGET_LEDGER_FILE)
        self._lock = threading.Lock()

    def record(self, row: dict) -> dict:
        entry = {"at": _iso(), "ts": _now(), **row}
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str) + "\n"
        try:
            # The in-process lock AND the cross-process one: `flock` is what
            # makes two writers safe, and holding the thread lock outside it
            # keeps one process from queueing on its own file descriptor.
            with self._lock:
                with serialized_append(self.path) as append:
                    append(line)
        except (OSError, TimeoutError) as e:
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
                # RA-06 -- AN UNREADABLE ROW MAKES THE TOTAL UNKNOWN, NOT
                # SMALLER.
                #
                # Before, a torn LAST line ended the read (`break`) and an
                # unparsable line anywhere else was logged and SKIPPED. Both
                # produce the same thing: a spend total that is too low, in
                # the file the daily cap is enforced against. A guard that
                # under-counts is a guard that does not bind.
                #
                # `check()` already refuses when the ledger cannot be READ.
                # It has to refuse just as firmly when the ledger can be read
                # and not believed, so this raises the same exception.
                raise RuntimeError(
                    f"budget ledger row {i + 1} of {self.path} is not "
                    f"readable JSON, so total spend cannot be established; "
                    f"no provider call is made until an operator reconciles "
                    f"it. The row is PRESERVED, never rewritten.")
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
        #: RA-06. Set when money was spent and the row recording it could NOT
        #: be made durable. Sticky for the life of the process on purpose:
        #: the spend really happened, the ledger really does not know about
        #: it, and every total computed from that ledger is understated until
        #: an operator reconciles. Clearing it on the next successful write
        #: would hide exactly the gap it exists to report.
        self.accounting_uncertain = ""

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

        if self.accounting_uncertain:
            # RA-06: a spend we could not record is a spend no cap can see.
            result.update(
                allowed=False, reason=REASON_BUDGET,
                detail=f"a previous provider call was made and its cost row "
                       f"could not be made durable ({self.accounting_uncertain}); "
                       f"recorded spend is now known to be understated, so no "
                       f"further call is made")
            return result
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
        """Charge what was really spent, after the answer arrived.

        `api_cost_usd` on the ledger row is the BUDGETED figure -- the
        vendor's billed amount when it supplied one, our estimate otherwise
        -- so the cap is enforced against the money that will actually be
        invoiced. The raw token and tool counts travel with it so any row
        can be re-costed under a later rate card.
        """
        try:
            self.ledger.record({
                "provider": cost_row.get("provider"),
                "model": cost_row.get("model"),
                "input_tokens": cost_row.get("input_tokens", 0),
                "cached_input_tokens": cost_row.get("cached_input_tokens", 0),
                "output_tokens": cost_row.get("output_tokens", 0),
                "tool_calls": cost_row.get("tool_calls", 0),
                "search_queries": cost_row.get("search_queries", 0),
                "api_cost_usd": budgeted_cost(cost_row),
                "estimated_cost_usd": cost_row.get("api_cost_usd", 0.0),
                "billed_cost_usd": cost_row.get("billed_cost_usd"),
                "billed_cost_raw": cost_row.get("billed_cost_raw"),
                "cost_source": ("vendor_billed"
                                if cost_row.get("billed_cost_usd") is not None
                                else "rate_card_estimate"),
                "cost_reconciliation": cost_row.get("cost_reconciliation"),
                "cost_priced": cost_row.get("cost_priced", False),
                "pricing_version": cost_row.get("pricing_version", ""),
                "pricing_source": cost_row.get("pricing_source", ""),
                "pricing_asof": cost_row.get("pricing_asof", ""),
                "latency_ms": cost_row.get("latency_ms", 0),
                "outcome": cost_row.get("outcome", ""),
            })
        except (OSError, TimeoutError) as exc:
            # RA-06: already logged, and now REMEMBERED. Swallowing this made
            # money that had genuinely been spent invisible to every later
            # check, which is the under-count the caps cannot survive.
            self.accounting_uncertain = f"{type(exc).__name__}: {exc}"
            log.error(f"[ALPHA_BUDGET] a provider call was made and its cost "
                      f"row is NOT durable ({exc}); further calls are refused "
                      f"until an operator reconciles the budget ledger")

    def snapshot(self) -> dict:
        try:
            return {"spent_today_usd": self.ledger.spent_today(),
                    "caps": {
                        "per_analysis_usd": float(CFG.ALPHA_MAX_COST_PER_ANALYSIS_USD),
                        "provider_hourly_usd": float(CFG.ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD),
                        "daily_usd": float(CFG.ALPHA_MAX_COST_PER_DAY_USD)},
                    "pricing_version": self.pricing.version,
                    "priced_models": sorted(self.pricing.configured_models()),
                    "expired_models": self.pricing.expired_models(),
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
