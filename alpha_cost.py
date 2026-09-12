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
from durable_append import serialized_append

log = logging.getLogger("ALPHA")

PRICING_SCHEMA = "atlas-alpha-pricing-v2"
BUDGET_LEDGER_FILE = "alpha_budget_ledger.jsonl"

REASON_UNPRICED = "pricing_unconfigured"
REASON_BUDGET = "budget_exhausted"
REASON_EXPIRED = "pricing_expired"

#: V4-RA-06/07. The versioned budget-row schema. Rows written before it exist
#: carry no `schema` key at all and are validated under the LEGACY rules --
#: they are historical `actual` charges and are never rewritten.
#: `docs/design/budget-durability-protocol.md` is the protocol these rows
#: implement.
BUDGET_SCHEMA = "atlas-alpha-budget-v1"

#: The four row kinds of the intent lifecycle.
KIND_RESERVATION = "reservation"      # announced BEFORE the provider call
KIND_ACTUAL = "actual"                # what the call really cost
KIND_VOID = "void"                    # proof the call was never made
KIND_RECONCILED = "reconciled"        # an operator closing out an orphan
BUDGET_KINDS = (KIND_RESERVATION, KIND_ACTUAL, KIND_VOID, KIND_RECONCILED)

#: The kinds that RESOLVE an open reservation.
RESOLVING_KINDS = (KIND_ACTUAL, KIND_VOID, KIND_RECONCILED)

#: Policy bound on a single row's dollar figure. A cost larger than this is
#: not a cost, it is a corrupt number, and accepting it would let one row
#: exhaust every cap forever; refusing it makes the total UNKNOWN, which is
#: the refusal an operator can act on.
MAX_ROW_USD = 1_000_000.0

#: V4-RA-06. This PROCESS INSTANCE, as a token no other process can produce.
#:
#: A reservation records the instance that wrote it, and an unresolved
#: reservation belonging to any other instance is ORPHANED. A pid will not do
#: the job: pids are reused, and `owner_is_alive()` answers "cannot tell ->
#: alive", which for a partial FILE means "keep it" (safe) and for a
#: RESERVATION would mean "still in flight, do not block" (unsafe). The
#: conservative answer has the opposite sign for the two objects.
#:
#: A restart always yields a new token, which is precisely why the RA-06
#: witness -- money spent, cost row unwritable, process restarts -- is
#: orphaned immediately rather than after a timeout that could be waited out.
INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:16]}"


class BudgetLedgerInvalid(RuntimeError):
    """Recorded budget evidence exists and cannot be believed (V4-RA-07).

    A `RuntimeError` on purpose: `BudgetGuard.check` already converts that
    into a structured refusal, and the whole point of this finding is that an
    unverifiable row must reach the same refusal as an unreadable file rather
    than escaping as a `ValueError` from `float()` somewhere in a total.
    """


def _now() -> float:
    return time.time()


def _iso(ts: float = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  timezone.utc).isoformat(timespec="seconds")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def admissible_amount(value) -> bool:
    """Would `_row_amount` accept this as a charge on an `actual` row?

    The WRITER's half of V4-RA-07, and it exists because the reader's
    strictness can otherwise be turned against the ledger. `budgeted_cost`
    prefers the vendor's own `billed_cost_usd`, which is a number a third
    party supplies: a negative or absurd one would be written faithfully and
    then REFUSED by the next read, leaving a ledger that cannot be totalled
    and whose only remedy would be editing history -- which this protocol
    forbids everywhere else.

    So a figure the reader would reject is never written. `record_actual`
    leaves the reservation OPEN instead, which blocks conservatively and
    routes the real number through `reconcile()`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and 0 <= number <= MAX_ROW_USD


def _no_duplicate_keys(pairs):
    """`json.loads` object hook that REFUSES a duplicated key (V4-RA-07).

    `{"api_cost_usd": 1.0, "api_cost_usd": 99.0}` is valid JSON and
    `json.loads` silently keeps the last value. Which one the writer meant is
    not recoverable, and "pick the last" is an arbitrary rule standing in for
    a fact we do not have -- so the row is AMBIGUOUS, and ambiguous accounting
    evidence has to make the total unknown rather than resolve itself.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicated key {key!r} in one row; which value "
                             f"was meant cannot be recovered")
        seen[key] = value
    return seen


def _row_amount(row, field, where, *, allow_negative=False):
    """One validated dollar figure from a budget row, or raise (V4-RA-07).

    Every branch refuses rather than skips. A row whose cost cannot be
    believed used to contribute nothing, which is arithmetically identical to
    contributing zero -- and a budget guard that silently under-counts is a
    budget guard that does not bind.
    """
    if field not in row:
        raise BudgetLedgerInvalid(
            f"{where}: a {row.get('kind', KIND_ACTUAL)!r} row carries no "
            f"{field!r}, so what it cost cannot be established")
    value = row[field]
    if isinstance(value, bool):
        # `True` is an `int` in Python and `float(True) == 1.0`. A boolean is
        # not a dollar amount, and costing it at $1 is a fabricated figure.
        raise BudgetLedgerInvalid(
            f"{where}: {field} is a boolean, which is not a dollar amount")
    if not isinstance(value, (int, float)):
        raise BudgetLedgerInvalid(
            f"{where}: {field} is {type(value).__name__}, not a number")
    number = float(value)
    if not math.isfinite(number):
        # NaN and +/-Infinity both survive `json.loads` and both destroy a
        # total: NaN makes every comparison false, Infinity makes every cap
        # exceeded forever.
        raise BudgetLedgerInvalid(
            f"{where}: {field} is {value!r}, which is not a finite amount")
    if number < 0 and not allow_negative:
        # The documented refund policy: a negative figure is a CORRECTION and
        # is admissible only on a `reconciled` row, which an operator writes
        # deliberately. Allowing it anywhere else would make "append a large
        # negative cost" the cheapest way to defeat every cap.
        raise BudgetLedgerInvalid(
            f"{where}: {field} is negative ({number}); a refund or correction "
            f"is only admissible on a {KIND_RECONCILED!r} row")
    if abs(number) > MAX_ROW_USD:
        raise BudgetLedgerInvalid(
            f"{where}: {field} is {number}, beyond the ${MAX_ROW_USD:.0f} "
            f"bound on a single row; that is a corrupt number rather than a "
            f"cost, and believing it would exhaust every cap permanently")
    return number


def _row_timestamp(row, where):
    """The row's validated epoch timestamp, or raise (V4-RA-07).

    `float(row.get("ts") or 0)` was the whole of this, and it raised
    `ValueError` on a string and `TypeError` on a list or a mapping -- out of
    `spent_today()`, past `BudgetGuard.check`'s `except RuntimeError`, and
    into the caller as an unhandled exception. A guard whose refusal path can
    be stepped around is not a guard.
    """
    if "ts" not in row:
        raise BudgetLedgerInvalid(
            f"{where}: no timestamp, so this row cannot be placed in any "
            f"spend window and no cap can be enforced against it")
    value = row["ts"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetLedgerInvalid(
            f"{where}: ts is {type(value).__name__}, not an epoch number")
    number = float(value)
    if not math.isfinite(number):
        raise BudgetLedgerInvalid(f"{where}: ts is {value!r}, not finite")
    if number <= 0:
        raise BudgetLedgerInvalid(
            f"{where}: ts is {number}, which is not a real instant")
    # A row stamped far in the future would sit outside every backward-looking
    # window forever, which is indistinguishable from not being counted.
    if number > _now() + 86400.0 * 366:
        raise BudgetLedgerInvalid(
            f"{where}: ts is more than a year in the future ({number}); a row "
            f"that no window can ever contain is not accounted evidence")
    return number


def _validated_row(raw, index, path, *, where=None):
    """One budget row, strictly validated, or raise (V4-RA-07).

    Returns a small normalized record -- kind, identity, timestamp, amount --
    so the resolution in `BudgetLedger.resolve` reads the lifecycle rather
    than re-deriving it from loose dictionary lookups.

    The row itself is never modified and never removed. "Unverifiable" is a
    statement about our right to compute a total, not a licence to edit
    history.
    """
    # `where` is overridden by the WRITE-side check, where there is no row
    # number yet and "row 0" would send an operator looking for a row that
    # does not exist.
    where = where or f"budget ledger row {index + 1} of {path}"
    if not isinstance(raw, dict):
        # A list, a scalar, a string, `null`. `rows()` used to filter these
        # out with `if isinstance(row, dict)`, which is a silent skip: the
        # total got smaller and nothing said so.
        raise BudgetLedgerInvalid(
            f"{where} is a {type(raw).__name__}, not an accounting object; "
            f"spend cannot be established from it and it is NOT skipped")

    schema = raw.get("schema")
    if schema is None:
        # LEGACY: written before the lifecycle existed. A historical actual
        # charge, validated under the rules that were true when it was
        # written, and preserved exactly as it is.
        return {"kind": KIND_ACTUAL, "legacy": True,
                "intent_id": f"legacy:{path}:{index + 1}",
                "ts": _row_timestamp(raw, where),
                "amount": _row_amount(raw, "api_cost_usd", where),
                "provider": raw.get("provider"),
                "owner_instance": None, "raw": raw}
    if schema != BUDGET_SCHEMA:
        raise BudgetLedgerInvalid(
            f"{where} declares schema {schema!r}; this reader validates "
            f"{BUDGET_SCHEMA!r} and refuses to guess at another")

    kind = raw.get("kind")
    if kind not in BUDGET_KINDS:
        raise BudgetLedgerInvalid(
            f"{where} has kind {kind!r}, which is not one of "
            f"{list(BUDGET_KINDS)}; its place in the intent lifecycle is "
            f"undefined")

    intent = raw.get("intent_id")
    if not isinstance(intent, str) or not intent.strip():
        raise BudgetLedgerInvalid(
            f"{where} carries no usable intent_id, so it cannot be tied to "
            f"the reservation it resolves or resolved by anything later")

    record = {"kind": kind, "legacy": False, "intent_id": intent,
              "ts": _row_timestamp(raw, where), "amount": 0.0,
              "provider": raw.get("provider"),
              "owner_instance": raw.get("owner_instance"), "raw": raw}

    if kind == KIND_RESERVATION:
        record["amount"] = _row_amount(raw, "reserved_usd", where)
        owner = raw.get("owner_instance")
        if not isinstance(owner, str) or not owner.strip():
            raise BudgetLedgerInvalid(
                f"{where} is a reservation with no owner_instance; whether it "
                f"is in flight or abandoned cannot be decided, and that "
                f"question is the whole of the restart protocol")
    elif kind == KIND_ACTUAL:
        record["amount"] = _row_amount(raw, "api_cost_usd", where)
    elif kind == KIND_RECONCILED:
        # The ONE kind that may carry a negative figure: see the refund
        # policy in `_row_amount` and in the protocol document.
        record["amount"] = _row_amount(raw, "api_cost_usd", where,
                                       allow_negative=True)
    else:                                              # KIND_VOID
        # A void asserts the provider was never contacted, so it charges
        # nothing -- and it has to say why, or "nothing was spent" is an
        # unsupported claim.
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BudgetLedgerInvalid(
                f"{where} voids a reservation without saying why; a released "
                f"charge needs a reason or it is indistinguishable from a "
                f"lost one")
    return record


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
        #: The validated lifecycle records from the last `rows()` read. Not a
        #: cache: it is refreshed on every read, because the file is the
        #: authority and a second process may have appended to it.
        self._records = []

    def record(self, row: dict) -> dict:
        entry = {"at": _iso(), "ts": _now(), **row}
        # V4-RA-07 -- NO WRITER MAY APPEND A ROW THIS READER WOULD REFUSE.
        #
        # The strict reader can otherwise be turned against the ledger: a
        # single inadmissible row makes every later total unverifiable, and
        # the only remedy -- editing history -- is forbidden everywhere else
        # in this protocol. So the strictness is enforced at the ONE choke
        # point every writer goes through, rather than at each caller, which
        # covers `record_actual`, `_reserve`, `void_reservation`,
        # `reconcile`, the smoke test and anything added later.
        #
        # Found by sweeping the writers after the implementation, not by the
        # counter-audit's witness list: a void with a blank reason and a
        # reconciliation of NaN both bricked the ledger permanently.
        #
        # Every caller already treats a `RuntimeError` from `record` as a
        # failure to report, so refusing here fails closed in the right
        # direction at each of them: the call is not made, the latch is set,
        # or the reservation simply stays open.
        try:
            _validated_row(entry, -1, self.path,
                           where=f"the row being appended to {self.path}")
        except BudgetLedgerInvalid as exc:
            log.error(f"[ALPHA_BUDGET] refusing to append a row this ledger "
                      f"could not read back ({exc}); nothing is written")
            raise
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
            self._records = []
            return []
        out, records = [], []
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            raise RuntimeError(f"budget ledger unreadable: {e}")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                # V4-RA-07: a duplicated key inside one row is refused by the
                # hook rather than silently resolved to whichever value came
                # last. It arrives here as the `ValueError` below, which is
                # the same refusal as unparseable JSON -- because it is the
                # same fact: this row does not have one meaning.
                row = json.loads(line, object_pairs_hook=_no_duplicate_keys)
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
            # V4-RA-07: EVERY row is validated, and an unverifiable one
            # raises rather than being filtered out. `if isinstance(row,
            # dict)` was a silent skip, and a skipped charge and a charge of
            # zero are the same number.
            record = _validated_row(row, i, self.path)
            if since_ts is None or record["ts"] >= since_ts:
                out.append(record["raw"])
            records.append(record)
        self._records = records
        return out

    def validated(self) -> list:
        """Every row as a validated lifecycle record, or raise (V4-RA-07)."""
        self.rows()
        return list(self._records)

    def resolve(self, *, since_ts: float = None, provider: str = None) -> dict:
        """The intent lifecycle, resolved. The one place spend is computed.

        V4-RA-06. Rows are not summed; INTENTS are. A reservation announces a
        charge, and an `actual`, `void` or `reconciled` row carrying the same
        `intent_id` settles it -- so a retry that appended the same logical
        row twice (which RA-05's append-only barrier retry can produce) is one
        charge, not two.

        An intent that is still open is charged at its RESERVED amount,
        because an announced call is one we must assume happened until
        something on disk says otherwise. An open intent belonging to another
        process instance is ORPHANED: nobody is going to resolve it, and the
        total it belongs to is uncertain rather than merely provisional.
        """
        records = self.validated()
        reservations, settlements = {}, {}
        for record in records:
            intent = record["intent_id"]
            if record["kind"] == KIND_RESERVATION:
                previous = reservations.get(intent)
                if previous is not None \
                        and previous["amount"] != record["amount"]:
                    # An IDENTICAL duplicate is expected -- RA-05's
                    # append-only retry produces one, and it resolves to a
                    # single charge. Two announcements of the SAME intent for
                    # DIFFERENT amounts is not that: `intent_id` carries a
                    # uuid4, so this is corruption or two ledgers merged, and
                    # picking either figure would be inventing one.
                    raise BudgetLedgerInvalid(
                        f"{self.path}: intent {intent!r} is reserved twice "
                        f"for different amounts (${previous['amount']} and "
                        f"${record['amount']}); which was announced cannot be "
                        f"established, so total spend cannot be. The rows are "
                        f"PRESERVED.")
                reservations[intent] = record
            elif record["kind"] in RESOLVING_KINDS:
                held = settlements.get(intent)
                if held is not None and held["kind"] == KIND_RECONCILED \
                        and record["kind"] != KIND_RECONCILED:
                    # RECONCILED IS TERMINAL. An operator closing out an
                    # orphan is a deliberate human decision about an intent
                    # nobody was going to settle; a late automatic row must
                    # not silently overturn it. Without this, a slow response
                    # landing after a reconciliation would replace the figure
                    # a human chose -- by arriving second.
                    continue
                settlements[intent] = record

        # A VOID is the only row that reduces a charge to nothing, so it is
        # the only one whose reservation must exist. Voiding an intent this
        # ledger never announced is either truncated history or two ledgers
        # mixed together, and in both cases the total cannot be defended.
        #
        # An ACTUAL with no reservation is NOT a violation: it is a charge
        # that stands on its own, which is what every row written before this
        # protocol existed is, and what `record_actual` writes when the gate
        # was bypassed. Treating those as unmatched settlements was a bug in
        # the first draft of this function -- it made every legacy ledger
        # unverifiable, which is over-refusal rather than under-counting, but
        # wrong either way.
        stray_voids = [r for intent, r in settlements.items()
                       if r["kind"] == KIND_VOID
                       and intent not in reservations]
        if stray_voids:
            raise BudgetLedgerInvalid(
                f"{self.path}: {len(stray_voids)} void row(s) release an "
                f"intent that was never reserved in this ledger; a charge "
                f"cancelled without ever being announced cannot be told from "
                f"a lost one, so total spend cannot be established. The rows "
                f"are PRESERVED.")

        def counts(record):
            if since_ts is not None and record["ts"] < since_ts:
                return False
            return True

        total, open_intents, orphaned = 0.0, [], []
        for intent, reservation in reservations.items():
            settlement = settlements.get(intent)
            charged = settlement if settlement is not None else reservation
            if settlement is None:
                open_intents.append(reservation)
                if reservation["owner_instance"] != INSTANCE_ID:
                    orphaned.append(reservation)
            if provider and reservation["provider"] != provider:
                continue
            if counts(charged):
                total += charged["amount"]
        for intent, settlement in settlements.items():
            if intent in reservations or settlement["kind"] == KIND_VOID:
                continue
            # A standalone charge: legacy history, or a spend the gate never
            # announced. It is counted at its own amount, because money that
            # was spent has to appear somewhere.
            if provider and settlement["provider"] != provider:
                continue
            if counts(settlement):
                total += settlement["amount"]
        return {"total_usd": round(total, 8),
                "open": open_intents, "orphaned": orphaned,
                "rows": len(records)}

    def spent(self, *, window_s: float, provider: str = None) -> float:
        cutoff = _now() - float(window_s)
        return self.resolve(since_ts=cutoff, provider=provider)["total_usd"]

    def spent_today(self) -> float:
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.resolve(since_ts=start)["total_usd"]

    def orphaned_intents(self) -> list:
        """Open reservations no live instance will ever settle (V4-RA-06).

        Computed over the WHOLE ledger rather than a window: an abandoned
        announcement from before midnight is still an unaccounted charge, and
        letting the daily boundary retire it would be the "restart clears the
        latch" defect wearing a calendar.
        """
        return self.resolve()["orphaned"]


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
        #:
        #: V4-RA-06: IT IS NO LONGER THE PROTECTION, ONLY A FAST PATH.
        #:   This latch lived only in memory, so the sequence "spend $2, cost
        #:   row fails, restart" produced a fresh guard with an empty latch
        #:   and an empty ledger -- and an empty ledger read as ZERO SPEND,
        #:   which restored admission against a $1 cap. Memory is exactly the
        #:   wrong place for a fact whose whole purpose is to survive the
        #:   process that learned it.
        #:
        #:   The durable protection is the RESERVATION written BEFORE the
        #:   provider is called (`check`), which stays on disk as an open
        #:   intent if the actual row never lands. `accounting_state()` reads
        #:   it back from the file, so the refusal survives restart whether or
        #:   not this attribute does.
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
            # V4-RA-06: read the DURABLE uncertainty before the caps. An
            # orphaned reservation is an announced charge that no live
            # instance will ever settle, so the total is not merely
            # provisional -- it is unknown, and it stays unknown across every
            # restart until an operator reconciles it.
            orphaned = self.ledger.orphaned_intents()
            spent_hour = self.ledger.spent(window_s=3600.0, provider=provider)
            spent_day = self.ledger.spent_today()
        except RuntimeError as e:
            # Unreadable OR unverifiable ledger (V4-RA-07 raises
            # `BudgetLedgerInvalid`, a `RuntimeError`): we cannot prove we are
            # under budget, so we are not. Same posture as an unreadable
            # continuity chain.
            result.update(allowed=False, reason=REASON_BUDGET,
                          detail=f"budget ledger unreadable ({e}); spend "
                                 f"cannot be bounded, so no call is made")
            return result
        if orphaned:
            names = ", ".join(sorted(r["intent_id"] for r in orphaned)[:4])
            reserved = round(sum(r["amount"] for r in orphaned), 8)
            result.update(
                allowed=False, reason=REASON_BUDGET,
                detail=(f"{len(orphaned)} announced provider call(s) were "
                        f"never accounted for (${reserved:.6f} reserved; "
                        f"{names}). They were announced by a process instance "
                        f"that is gone, so whether that money was spent "
                        f"cannot be established from this ledger; no further "
                        f"call is made until an operator reconciles them. "
                        f"Restarting does NOT clear this."))
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

        # V4-RA-06 -- ANNOUNCE THE CHARGE BEFORE IT CAN BE INCURRED.
        #
        # This is the last instruction before the caller dispatches (see
        # `alpha_dispatcher`: an allowed verdict is followed immediately by
        # `pool.submit`), so it is the only place a durable record can be
        # written while the money is still unspent. The previous design wrote
        # nothing until AFTER the answer arrived, which left exactly one
        # window -- call made, row unwritable -- in which the only trace was
        # an in-memory flag that a restart erased.
        #
        # If the reservation cannot be made durable, the call is REFUSED. That
        # is the whole cross-finding contract with RA-05: `serialized_append`
        # raises `DurabilityUnknown` when the pathname barrier cannot be
        # confirmed, and an announcement we cannot prove we made is not an
        # announcement, so nothing is dispatched against it. Nothing is spent,
        # so nothing is lost.
        reservation = self._reserve(provider, model,
                                    estimate["api_cost_usd"])
        if reservation is None:
            result.update(
                allowed=False, reason=REASON_BUDGET,
                detail=("the spend could not be ANNOUNCED durably before the "
                        "call, so the call is not made; a provider contacted "
                        "against an unwritable ledger is money that cannot be "
                        "accounted for afterwards"))
            return result
        result["intent_id"] = reservation
        return result

    def _reserve(self, provider: str, model: str, reserved_usd: float):
        """Append a durable RESERVATION and return its id, or None."""
        if not admissible_amount(reserved_usd):
            # V4-RA-07, writer side, same argument as `record_actual`: never
            # append a row this ledger's own reader would refuse. A NaN or
            # absurd estimate -- which a broken rate card can produce -- was
            # written faithfully as `reserved_usd`, the call was ALLOWED, and
            # the very next read of the ledger was unverifiable for good.
            # Returning None refuses the call instead: nothing is spent, so
            # nothing is lost.
            log.error(f"[ALPHA_BUDGET] refusing to announce "
                      f"{provider}/{model}: the estimate {reserved_usd!r} is "
                      f"not an amount that can be recorded, and a call whose "
                      f"cost cannot be announced is not made")
            return None
        intent_id = f"{INSTANCE_ID}:{uuid.uuid4().hex[:16]}"
        try:
            self.ledger.record({
                "schema": BUDGET_SCHEMA,
                "kind": KIND_RESERVATION,
                "intent_id": intent_id,
                "owner_instance": INSTANCE_ID,
                "provider": provider,
                "model": model,
                # The WORST CASE, which is what `estimate` already computes.
                # Reserving an average would leave the cap breachable by
                # exactly the calls that cost more than average.
                "reserved_usd": round(float(reserved_usd), 8),
            })
        except (OSError, TimeoutError, RuntimeError) as exc:
            log.error(f"[ALPHA_BUDGET] could not announce the spend for "
                      f"{provider}/{model} durably ({type(exc).__name__}: "
                      f"{exc}); the call is NOT made")
            return None
        return intent_id

    def _open_intent_for(self, provider: str, model: str):
        """The oldest unsettled reservation THIS instance made, or None.

        Read from the LEDGER rather than from memory, so an actual row that
        arrives after a restart can still settle the reservation it belongs
        to instead of being filed as an unreserved charge.
        """
        try:
            candidates = [r for r in self.ledger.resolve()["open"]
                          if r["owner_instance"] == INSTANCE_ID
                          and r["provider"] == provider
                          and (r["raw"].get("model") == model)]
        except RuntimeError:
            return None
        if not candidates:
            return None
        return min(candidates, key=lambda r: r["ts"])["intent_id"]

    def void_reservation(self, intent_id: str, reason: str) -> bool:
        """Release a reservation for a call that was provably NOT made.

        The only honest way to reduce a reserved amount: it charges nothing
        and it has to say why. Used when a provider was announced and then not
        contacted at all -- the one case where "no money was spent" is a
        claim we can actually support.
        """
        try:
            self.ledger.record({
                "schema": BUDGET_SCHEMA, "kind": KIND_VOID,
                "intent_id": intent_id, "reason": str(reason or "")[:200]})
            return True
        except (OSError, TimeoutError, RuntimeError) as exc:
            # A void that is not durable leaves the reservation open, which
            # over-counts. That is the safe direction and it is left alone.
            log.warning(f"[ALPHA_BUDGET] could not void {intent_id}: {exc}")
            return False

    def reconcile(self, intent_id: str, api_cost_usd: float,
                  note: str = "") -> bool:
        """Close out an orphaned intent with its final figure (V4-RA-06).

        The documented exit from UNCERTAIN, and the only row kind that may
        carry a negative amount (a correction or refund). Append-only like
        everything else: the orphaned reservation stays exactly where it is.
        """
        try:
            self.ledger.record({
                "schema": BUDGET_SCHEMA, "kind": KIND_RECONCILED,
                "intent_id": intent_id,
                "api_cost_usd": round(float(api_cost_usd), 8),
                "note": str(note or "")[:200],
                "reconciled_by_instance": INSTANCE_ID})
            return True
        except (OSError, TimeoutError, RuntimeError) as exc:
            log.error(f"[ALPHA_BUDGET] could not reconcile {intent_id}: {exc}")
            return False

    def accounting_state(self) -> dict:
        """What this ledger can and cannot currently prove (V4-RA-06/07)."""
        try:
            resolved = self.ledger.resolve()
        except RuntimeError as exc:
            return {"certain": False, "reason": "ledger_unverifiable",
                    "detail": str(exc)}
        if self.accounting_uncertain:
            return {"certain": False, "reason": "record_not_durable",
                    "detail": self.accounting_uncertain}
        if resolved["orphaned"]:
            return {"certain": False, "reason": "orphaned_intents",
                    "detail": f"{len(resolved['orphaned'])} announced call(s) "
                              f"were never accounted for",
                    "orphaned": [r["intent_id"] for r in resolved["orphaned"]]}
        return {"certain": True, "reason": None, "detail": "",
                "open_intents": len(resolved["open"]),
                "total_usd": resolved["total_usd"]}

    def record_actual(self, cost_row: dict) -> None:
        """Charge what was really spent, after the answer arrived.

        `api_cost_usd` on the ledger row is the BUDGETED figure -- the
        vendor's billed amount when it supplied one, our estimate otherwise
        -- so the cap is enforced against the money that will actually be
        invoiced. The raw token and tool counts travel with it so any row
        can be re-costed under a later rate card.

        V4-RA-06: it SETTLES the reservation `check` announced, by identity.
        The intent is taken from the cost row when the caller carries it
        through, and otherwise looked up as the oldest open reservation this
        instance made for the same provider and model -- from the ledger, not
        from memory, so a restart does not turn a settlement into an
        unattributable extra charge. Settling by identity is also what makes
        RA-05's retry safe: an append that ran twice is one intent, therefore
        one charge.
        """
        intent_id = cost_row.get("intent_id") or self._open_intent_for(
            cost_row.get("provider"), cost_row.get("model"))
        if not intent_id:
            # A spend the gate never announced -- the gate was bypassed, or
            # the caller is `tools/alpha_smoke_test.py` calling
            # `record_actual` directly. It is charged as a STANDALONE actual
            # under its own identity: one row, counted at its own amount,
            # because money that was spent has to appear somewhere. No
            # synthetic reservation is invented for it; a reservation is an
            # announcement, and announcing something after the fact would be
            # a fiction in the one file that has to be believable.
            intent_id = f"{INSTANCE_ID}:unreserved:{uuid.uuid4().hex[:16]}"
            log.warning(f"[ALPHA_BUDGET] recording a cost for "
                        f"{cost_row.get('provider')}/{cost_row.get('model')} "
                        f"that was never announced; it is charged under "
                        f"{intent_id}")

        # V4-RA-07, writer side: never append a row this ledger's own reader
        # would refuse. `budgeted_cost` prefers the VENDOR's billed figure,
        # and a negative or absurd one would brick the ledger permanently --
        # every later total unverifiable, and the only remedy forbidden. The
        # reservation is left OPEN, which blocks conservatively, and the real
        # figure goes through `reconcile()`.
        amount = budgeted_cost(cost_row)
        if not admissible_amount(amount):
            self.accounting_uncertain = (
                f"the cost reported for {cost_row.get('provider')}/"
                f"{cost_row.get('model')} was {amount!r}, which is not an "
                f"admissible charge")
            log.error(f"[ALPHA_BUDGET] refusing to record an inadmissible "
                      f"cost ({amount!r}) for intent {intent_id}; writing it "
                      f"would make every later total unverifiable. The "
                      f"reservation stays OPEN and an operator must "
                      f"reconcile it with the real figure.")
            return
        try:
            self.ledger.record({
                "schema": BUDGET_SCHEMA,
                "kind": KIND_ACTUAL,
                "intent_id": intent_id,
                "provider": cost_row.get("provider"),
                "model": cost_row.get("model"),
                "input_tokens": cost_row.get("input_tokens", 0),
                "cached_input_tokens": cost_row.get("cached_input_tokens", 0),
                "output_tokens": cost_row.get("output_tokens", 0),
                "tool_calls": cost_row.get("tool_calls", 0),
                "search_queries": cost_row.get("search_queries", 0),
                "api_cost_usd": round(amount, 8),
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
        except (OSError, TimeoutError, RuntimeError) as exc:
            # RA-06: already logged, and now REMEMBERED. Swallowing this made
            # money that had genuinely been spent invisible to every later
            # check, which is the under-count the caps cannot survive.
            #
            # V4-RA-06: and the DURABLE half is what actually protects here.
            # The reservation for this intent is already on disk and is now
            # never going to be settled, so the next process to read this
            # ledger sees an orphaned intent and refuses -- whether or not
            # this attribute, or this process, still exists. The latch is
            # kept because it makes the refusal immediate and names the cause
            # precisely; it is no longer the thing standing between a failed
            # write and a restored cap.
            self.accounting_uncertain = f"{type(exc).__name__}: {exc}"
            log.error(f"[ALPHA_BUDGET] a provider call was made and its cost "
                      f"row is NOT durable ({exc}); the reservation "
                      f"{intent_id} stays OPEN on disk, so further calls are "
                      f"refused across restarts until an operator reconciles "
                      f"the budget ledger")

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
