"""The ONE strict contract for a research candidate. SHADOW ONLY.

Astra AA-02 found three diverging validators: the producer
(`research_feed`), the consumer (`alpha_consumer`) and the readiness gate
(`alpha_feed_readiness`) each had their own idea of what a well-formed
candidate was, and each was permissive in a different place. A fact that one
of them refused, another accepted. This module is the single definition all
three now import, so a rule can only be weakened in one place and a test that
pins it here pins it everywhere.

WHAT THIS MODULE REFUSES TO DO
    It never repairs, coerces or substitutes. Every function returns either a
    validated value or an error; there is no third branch that "does its best".
    A research candidate is evidence for a calibration ledger, and a repaired
    field is an invented one -- it looks exactly like an observation once it is
    written down, which is the whole failure class Astra rejected the previous
    candidate for.

NO EXECUTION AUTHORITY
    Imports `hashlib`, `json`, `math` and `datetime` only. It cannot reach a
    broker, an order path, a risk gate or CAPITAL, and
    `tests/test_research_feed_boundary.py` pins that import list.
"""

import hashlib
import json
import math
from datetime import datetime, timezone

#: Bumped from v2: a v2 record carried a `field_provenance` that was only
#: required to be a non-empty STRING (AA-05), and a book whose NO side may
#: have been derived by execution normalization (AA-01). Neither is
#: distinguishable from an observation after the fact, so v2 records are
#: refused rather than migrated -- they have to be re-observed.
FEED_SCHEMA = "atlas-research-candidate-v3"

#: Every schema this contract refuses outright. Listed rather than inferred so
#: that "not the current schema" and "a known-unsafe older schema" are
#: different, countable refusals.
LEGACY_FEED_SCHEMAS = ("atlas-research-candidate-v1",
                       "atlas-research-candidate-v2")

REQUIRED_FIELDS = ("contract_id", "question", "resolution_rules",
                   "resolution_source", "yes_bid", "yes_ask",
                   "no_bid", "no_ask", "volume", "open_interest",
                   "market_close_time_utc", "expected_resolution_time_utc",
                   "emitted_at_utc")

#: Genuinely optional. Absent means ABSENT and is declared in
#: `unavailable_fields`; it never means "zero" or "empty string".
OPTIONAL_FIELDS = ("event_id", "catalyst_name", "catalyst_time_utc")

QUOTE_FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask")
SIZE_FIELDS = ("volume", "open_interest")
TIME_FIELDS = ("emitted_at_utc", "market_close_time_utc",
               "expected_resolution_time_utc")
TEXT_FIELDS = ("contract_id", "question", "resolution_rules",
               "resolution_source")

#: AA-02: a float that cannot round-trip through an int is not a market size.
#: 2**53 is where float64 stops representing consecutive integers.
MAX_SAFE_NUMBER = float(2 ** 53)

#: AA-01. Each quote must say whether the exchange published it or whether
#: something downstream computed it. Only OBSERVED is admissible evidence.
QUOTE_OBSERVED = "observed"
QUOTE_DERIVED = "derived"
QUOTE_OBSERVATION_KINDS = (QUOTE_OBSERVED, QUOTE_DERIVED)


class ContractError(ValueError):
    """A candidate violates the contract. Carries a field-tagged reason."""


class AliasContradiction(ContractError):
    """Two alias keys for ONE fact disagree.

    AA-03 (re-audit): this is deliberately its own type, carrying `field` and
    `values`, because the caller used to catch the generic `ContractError` and
    file the fact as absent. "The exchange did not publish this" and "the
    exchange published two incompatible answers" are different facts about
    the source, and a producer that reports the second as the first has
    thrown away the only one an operator could act on.
    """

    def __init__(self, message, *, field, values):
        super().__init__(message)
        self.field = field
        self.values = dict(values)


# ── AA-05: semantic source binding ───────────────────────────────────────
#: field -> (namespace, allowed source keys). Provenance must name one of
#: these EXACT paths. A non-empty string is not provenance: `yes_ask` sourced
#: from `market.title` is precisely the mislabelling AA-05 describes, and it
#: passed the old "is it a non-empty string" test unchanged.
#:
#: The rule for adding a key: two keys may share a row only when the exchange
#: publishes both names for ONE observation. A key naming a DIFFERENT fact --
#: a close time for a resolution time, a ticker for a question, a spread for
#: the far side of the book -- is a derivation, not an alias.
SOURCE_BINDING = {
    "contract_id": ("market", ("ticker",)),
    "event_id": ("market", ("event_ticker", "event_id")),
    "question": ("market", ("title",)),
    "resolution_rules": ("market", ("rules_primary",)),
    "resolution_source": ("market", ("settlement_sources",
                                     "settlement_source")),
    "volume": ("market", ("volume",)),
    "open_interest": ("market", ("open_interest",)),
    "market_close_time_utc": ("market", ("close_time",)),
    "expected_resolution_time_utc": ("market", ("expected_expiration_time",
                                                "expiration_time")),
    "yes_bid": ("raw_book", ("yes_bid",)),
    "yes_ask": ("raw_book", ("yes_ask",)),
    "no_bid": ("raw_book", ("no_bid",)),
    "no_ask": ("raw_book", ("no_ask",)),
    "emitted_at_utc": ("observer", ("emitted_at_utc",)),
    "catalyst_name": ("observer", ("catalyst",)),
    "catalyst_time_utc": ("observer", ("catalyst",)),
}

#: AA-03. Keys that are genuine aliases for ONE fact must AGREE when more than
#: one is supplied. `event_ticker` and `event_id` naming different events is a
#: contradiction in the source, not a choice for us to make silently.
ALIAS_GROUPS = {field: keys for field, (_ns, keys) in SOURCE_BINDING.items()
                if len(keys) > 1}

#: Suffix the producer appends when it converts an observed cents quote into
#: probability units. A unit change on an observed number is not a new fact.
CENTS_SUFFIX = "(cents)"


def provenance_path(field: str, key: str) -> str:
    """The canonical provenance string for a field read from `key`."""
    namespace = SOURCE_BINDING[field][0]
    suffix = CENTS_SUFFIX if field in QUOTE_FIELDS else ""
    return f"{namespace}.{key}{suffix}"


def allowed_provenance(field: str) -> tuple:
    """Every provenance string this contract will accept for `field`."""
    if field not in SOURCE_BINDING:
        return ()
    return tuple(provenance_path(field, key) for key in SOURCE_BINDING[field][1])


# ── strict scalar validation (AA-02) ─────────────────────────────────────
def strict_number(value, *, field, minimum=None, maximum=None,
                  allow_numeric_string=False):
    """A finite real number, or raise.

    Refuses, by explicit case rather than by whatever `float()` happens to
    tolerate: booleans (AA-02 names them -- `True` is an `int` in Python and
    `float(True) == 1.0`), NaN, +/-Infinity, values outside the documented
    bounds, and integers too large to survive the float conversion the rest
    of the pipeline performs.

    `allow_numeric_string` is OFF by default and exists only so a caller can
    opt in per field with the decision written down. AA-02 asks for ambiguous
    numeric strings to be refused "unless explicitly allowed by documented
    schema"; no field in this contract turns it on.
    """
    if isinstance(value, bool):
        raise ContractError(f"{field}: booleans are not market numbers")
    if isinstance(value, str):
        if not allow_numeric_string:
            raise ContractError(
                f"{field}: {safe_render(value)} is a string; numeric strings "
                f"are not accepted as market facts")
        text = value.strip()
        if not text:
            raise ContractError(f"{field}: blank string is not a number")
        try:
            value = float(text)
        except ValueError:
            raise ContractError(
                f"{field}: {safe_render(value)} is not numeric")
    elif isinstance(value, int):
        # An int beyond 2**53 silently loses precision as a float. Detect it
        # on the INTEGER, before the lossy conversion hides the problem.
        if abs(value) > MAX_SAFE_NUMBER:
            # V4-RA-03: `f"{value}"` on a large integer is not a diagnostic,
            # it is a decimal expansion. On CPython 3.11+ it RAISES
            # `ValueError` past 4300 digits -- so the one function whose
            # contract is "or raise ContractError" raised something else
            # entirely, from inside the construction of its own refusal, and
            # `observed_cents`'s `except ContractError` did not catch it. A
            # plain JSON integer quote of `10 ** 5000` therefore escaped the
            # producer and reached the observer's synchronous log call. It is
            # also unbounded work on the engine's thread below that limit.
            raise ContractError(
                f"{field}: {safe_render(value)} exceeds the exactly "
                f"representable range")
    elif not isinstance(value, float):
        raise ContractError(
            f"{field}: {type(value).__name__} is not a number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ContractError(f"{field}: {value!r} does not convert to a float")
    if math.isnan(number):
        raise ContractError(f"{field}: NaN is not an observation")
    if math.isinf(number):
        raise ContractError(f"{field}: {number} is not finite")
    if minimum is not None and number < minimum:
        raise ContractError(f"{field}: {number} is below the minimum {minimum}")
    if maximum is not None and number > maximum:
        raise ContractError(f"{field}: {number} is above the maximum {maximum}")
    return number


def strict_text(value, *, field, max_length=20000):
    """A non-blank string, or raise.

    Lists and dicts are refused rather than stringified: `str(["a"])` yields
    `"['a']"`, which is a plausible-looking question that the exchange never
    published (AA-02, "arrays/objects as text").
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractError(
            f"{field}: {type(value).__name__} is not text; it is never "
            f"stringified into one")
    text = value.strip()
    if not text:
        raise ContractError(f"{field}: blank or whitespace-only")
    if len(text) > max_length:
        raise ContractError(f"{field}: {len(text)} characters exceeds "
                            f"{max_length}")
    return text


def strict_timestamp(value, *, field):
    """A timezone-aware UTC `datetime`, or raise.

    A naive timestamp is refused rather than assumed to be UTC. Every deadline
    downstream is a comparison between two instants, and a silent timezone
    assumption turns a stale observation into a fresh one.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractError(
            f"{field}: {type(value).__name__} is not an ISO 8601 timestamp")
    text = value.strip()
    if not text:
        raise ContractError(f"{field}: blank timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError(f"{field}: {value!r} is not ISO 8601")
    if parsed.tzinfo is None:
        raise ContractError(
            f"{field}: {value!r} has no timezone; UTC is never assumed")
    return parsed.astimezone(timezone.utc)


def iso_second(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── V4-RA-03: rendering a raw source value is ENGINE-THREAD work ─────────
#: Hard ceiling on any rendering of a raw source value, in characters.
MAX_RENDER_CHARS = 200

#: How far `safe_render` descends into a container before naming its type
#: instead of describing its members.
MAX_RENDER_DEPTH = 2

#: How many members of a container `safe_render` describes.
MAX_RENDER_MEMBERS = 6


def _render(value, budget: int, depth: int) -> str:
    """One rendering step. Raises nothing it can help; `safe_render` is total.

    Every branch here is bounded by construction, and the reason is that this
    runs on the ENGINE's observer thread (V4-RA-03). "The diagnostic is
    truncated afterwards" is not a bound: the truncated string still has to be
    BUILT first, and building it is the cost the decision cycle pays.
    """
    if isinstance(value, str):
        # SLICE FIRST, and never through `repr`. `repr` of a 100MB string
        # materialises a 100MB string before anything truncates it; `value`
        # is a raw exchange field and its length is the exchange's choice,
        # not ours. Bare rather than quoted, because AA-03 pins the rendered
        # form of a contradictory text alias as the text itself.
        head = value[:budget]
        return head if len(head) == len(value) else head + "..."
    if value is None or isinstance(value, (bool, float, complex)):
        # Provably short: none of these has a rendering longer than a line.
        return f"{type(value).__name__}:{value!r}"
    if isinstance(value, int):
        # An integer's decimal expansion is UNBOUNDED -- `10 ** 10 ** 9` has
        # a billion digits and `repr` builds every one of them. `bit_length`
        # is cheap and decides whether the digits are affordable.
        bits = value.bit_length()
        if bits <= 4 * max(budget, 1) + 64:
            return f"int:{value!r}"
        return f"int:<{bits} bits>"
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:budget])
        tail = "" if len(head) == len(value) else "..."
        return f"{type(value).__name__}:{head!r}{tail}"
    if isinstance(value, dict):
        if depth <= 0 or budget <= 8:
            return f"dict:<{len(value)} key(s)>"
        parts, shown = [], 0
        for key, item in value.items():
            if shown >= MAX_RENDER_MEMBERS:
                break
            parts.append(f"{_render(key, 24, 0)}: "
                         f"{_render(item, max(8, budget // 4), depth - 1)}")
            shown += 1
        if shown < len(value):
            parts.append("...")
        return "dict:{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        if depth <= 0 or budget <= 8:
            return f"{type(value).__name__}:<{len(value)} item(s)>"
        parts, shown = [], 0
        for item in value:
            if shown >= MAX_RENDER_MEMBERS:
                break
            parts.append(_render(item, max(8, budget // 4), depth - 1))
            shown += 1
        if shown < len(value):
            parts.append("...")
        return f"{type(value).__name__}:[" + ", ".join(parts) + "]"
    if isinstance(value, (set, frozenset)):
        # Members deliberately NOT described: a set's iteration order is not
        # stable across processes, and this rendering goes INSIDE the record
        # digest (AA-03 puts `contradictory_fields` under the checksum). A
        # digest that depends on hash seeding is a digest of nothing.
        return f"{type(value).__name__}:<{len(value)} item(s)>"
    # An object whose `__repr__` is the exchange's code, not ours. Its class
    # name reads a slot on the type and is the one description of a hostile
    # value that cannot itself misbehave.
    return f"<{type(value).__name__}>"


def safe_render(value, *, limit: int = MAX_RENDER_CHARS) -> str:
    """A raw source value rendered for a human. TOTAL and BOUNDED.

    V4-RA-03. Two properties, and both of them are properties of the MONEY
    PATH rather than of research, because `candidate_from_market` runs on the
    engine's observer thread:

      TOTAL     it never raises. A value whose `__repr__` raises used to
                propagate that exception out of the producer, into the
                observer's `except`, and from there into a SYNCHRONOUS log
                call on the engine's own thread.

      BOUNDED   it never builds a large intermediate. `f"{value!r}"[:200]`
                is not bounded; it is a 100MB allocation followed by a
                truncation, paid for by the decision cycle.

    `KeyboardInterrupt` and `SystemExit` are deliberately NOT caught: an
    operator interrupting the process must not be swallowed by a diagnostic.
    Everything else -- including `MemoryError` and `RecursionError`, which a
    hostile `__repr__` is exactly how you provoke -- becomes text.
    """
    try:
        text = _render(value, max(int(limit), 8), MAX_RENDER_DEPTH)
    except Exception:                                         # noqa: BLE001
        try:
            return f"<unrenderable {type(value).__name__}>"
        except Exception:                                     # noqa: BLE001
            return "<unrenderable>"
    if not isinstance(text, str):                       # pragma: no cover
        return "<unrenderable>"
    return text if len(text) <= limit else text[:max(limit - 3, 1)] + "..."


# ── AA-03: alias contradiction ───────────────────────────────────────────
def resolve_alias(source: dict, field: str, keys, *, comparator=None):
    """`(value, key)` for a fact the source recorded, or `(None, None)`.

    When MORE THAN ONE alias is present they must agree. The previous
    first-present-wins rule silently picked one of two contradictory values --
    an `event_ticker` and an `event_id` naming different events would resolve
    to whichever happened to be listed first in a tuple in this file.

    `comparator` normalises before comparison for facts whose alias values are
    equal in meaning but not byte-identical (a settlement source list versus
    its single-object form). It never makes two different facts compare equal.
    """
    if not isinstance(source, dict):
        return None, None
    present = [(key, source[key]) for key in keys
               if key in source and source[key] not in (None, "")]
    if not present:
        return None, None
    first_key, first_value = present[0]
    if len(present) > 1:
        normalise = comparator or (lambda v: v)
        reference = normalise(first_value)
        for key, value in present[1:]:
            if normalise(value) != reference:
                # V4-RA-03: `{first_value!r}` was the raising site. This
                # message is built on the engine's observer thread, and
                # interpolating a raw exchange value through `repr` hands
                # that thread code the exchange wrote: a `__repr__` that
                # raises propagated out of the producer entirely, and one
                # that returns 100MB made the cycle pay for 100MB.
                raise AliasContradiction(
                    f"{field}: contradictory aliases -- {first_key}="
                    f"{safe_render(first_value)} and "
                    f"{key}={safe_render(value)} claim to be the "
                    f"same fact; neither is chosen",
                    field=field,
                    # EVERY present alias, not just the two that differ: an
                    # operator resolving this needs to see the whole
                    # disagreement, and a third agreeing key is evidence too.
                    values={k: v for k, v in present})
    return first_value, first_key


# ── AA-04: canonicalization and checksum ─────────────────────────────────
def canonical_json(obj) -> str:
    """Deterministic bytes for hashing.

    `default=str` is deliberately ABSENT: a value that cannot be serialized is
    an error, not something to stringify into the hash. Silently hashing
    `"<object at 0x7f..>"` would make the digest depend on a memory address.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_content(record: dict) -> dict:
    """The record minus its own digest -- exactly what the digest covers.

    AA-05 also asks that provenance be inside the checksum, so that editing
    where a fact "came from" invalidates the record. It is: this returns every
    key except `record_sha256`, `field_provenance` and `quote_observation`
    included.
    """
    return {k: v for k, v in record.items() if k != "record_sha256"}


def compute_checksum(record: dict) -> str:
    return hashlib.sha256(
        canonical_json(canonical_content(record)).encode("utf-8")).hexdigest()


def verify_checksum(record: dict) -> str:
    """Recompute and compare. Returns the verified digest, or raises.

    IMPORTANT, and stated here because the report must not overclaim: this
    proves only that the record's bytes match the digest travelling with them.
    It does NOT authenticate the exchange, the producer, or the transport --
    anyone able to rewrite the record can recompute the digest. It is an
    integrity check against corruption and accidental edits, not a signature.
    """
    if not isinstance(record, dict):
        raise ContractError("record is not an object")
    claimed = record.get("record_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        raise ContractError("record_sha256 is missing or not a sha256 digest")
    if not all(c in "0123456789abcdef" for c in claimed.lower()):
        raise ContractError("record_sha256 is not hexadecimal")
    try:
        actual = compute_checksum(record)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        # NEW-01: a value that cannot be canonicalized is a REFUSAL, not an
        # exception for the caller to trip over. `OverflowError` in
        # particular is what an oversized number raises, and it is neither a
        # TypeError nor a ValueError.
        raise ContractError(f"record cannot be canonicalized: "
                            f"{type(exc).__name__}: {exc}")
    if actual != claimed.lower():
        raise ContractError(
            f"record_sha256 mismatch: content hashes to {actual}, record "
            f"claims {claimed.lower()}")
    return actual


# ── the whole-record contract (AA-02, AA-05, AA-06, AA-07, AA-08) ────────
def _check_provenance_container(record, errors):
    provenance = record.get("field_provenance")
    unavailable = record.get("unavailable_fields")
    if not isinstance(provenance, dict):
        errors.append("field_provenance: missing or not an object")
        provenance = None
    if not isinstance(unavailable, list) or not all(
            isinstance(f, str) for f in unavailable):
        errors.append("unavailable_fields: missing or not a list of strings")
        unavailable = None
    return provenance, unavailable


def _check_field_provenance(field, record, provenance, unavailable, errors):
    """AA-05: provenance must name an ALLOWED EXACT path for this field."""
    claimed = provenance.get(field)
    if not isinstance(claimed, str) or not claimed.strip():
        errors.append(f"{field}: provenance missing or not a string")
        return False
    claimed = claimed.strip()
    permitted = allowed_provenance(field)
    if not permitted:
        errors.append(f"{field}: has no declared source binding")
        return False
    if claimed not in permitted:
        errors.append(
            f"{field}: provenance {claimed!r} is not an allowed source "
            f"(allowed: {list(permitted)})")
        return False
    if unavailable is not None and field in unavailable:
        # AA-05: "unavailable_fields does not contradict a populated field".
        errors.append(
            f"{field}: declared unavailable yet carries a value and "
            f"provenance")
        return False
    return True


def _check_book(record, errors):
    """AA-02: reject crossed books and quotes that cannot coexist.

    Checked only once all four quotes have validated as numbers; otherwise the
    comparison would raise on the malformed value instead of reporting it.
    """
    # NEW-01: `float(10**500)` raises OverflowError, which the previous
    # `except (KeyError, TypeError, ValueError)` did not catch -- so the one
    # function whose whole contract is "return a structured refusal" raised
    # instead, and the exception travelled out through `validate_record` and
    # `SpoolConsumer.pending()`, stopping the batch. Reuse `strict_number`,
    # which decides by explicit case and never converts a value it has not
    # already classified; a quote it refuses is reported by its own field
    # check above, so there is nothing to compare here.
    quotes = {}
    for field in QUOTE_FIELDS:
        try:
            quotes[field] = strict_number(record.get(field), field=field,
                                          minimum=0.0, maximum=1.0)
        except ContractError:
            return
    if quotes["yes_ask"] < quotes["yes_bid"]:
        errors.append(
            f"book: crossed YES side (bid {quotes['yes_bid']} > ask "
            f"{quotes['yes_ask']})")
    if quotes["no_ask"] < quotes["no_bid"]:
        errors.append(
            f"book: crossed NO side (bid {quotes['no_bid']} > ask "
            f"{quotes['no_ask']})")


def _check_contradictions(record, errors):
    """AA-03: a source that contradicts itself is not evidence.

    `contradictory_fields` maps a field to every alias value the source
    supplied for it. Its ABSENCE means "no contradiction was observed"; its
    presence with any entry means the producer saw one and preserved it. The
    map is inside the digest, so neither adding nor stripping it can be done
    without invalidating the record.

    Refused whether the field is required or optional. An optional field is
    one the source may stay SILENT about -- it is not one the source may
    answer twice, differently.
    """
    clashes = record.get("contradictory_fields")
    if clashes is None:
        return
    if not isinstance(clashes, dict):
        errors.append("contradictory_fields: must be an object")
        return
    for field, values in sorted(clashes.items()):
        if not isinstance(values, dict) or not values:
            errors.append(f"contradictory_fields.{field}: must name the "
                          f"conflicting source keys and their values")
            continue
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(values.items()))
        errors.append(
            f"{field}: the source contradicts itself ({detail}); neither "
            f"value is chosen and the record is refused")


def _check_quote_observation(record, errors):
    """AA-01: every quote must be declared, and declared OBSERVED.

    An execution-normalized book may legitimately derive a missing NO side for
    its own purposes -- that is what it is for. It is simply not evidence, and
    this is where the distinction is enforced rather than assumed.
    """
    observation = record.get("quote_observation")
    if not isinstance(observation, dict):
        errors.append("quote_observation: missing or not an object")
        return
    for field in QUOTE_FIELDS:
        kind = observation.get(field)
        if kind not in QUOTE_OBSERVATION_KINDS:
            errors.append(
                f"{field}: quote_observation must be one of "
                f"{list(QUOTE_OBSERVATION_KINDS)}, got {kind!r}")
        elif kind != QUOTE_OBSERVED:
            errors.append(
                f"{field}: quote was {kind}, not directly observed; a "
                f"complement or spread-derived quote is not evidence")


def validate_record(record, *, require_checksum=True) -> list:
    """Every contract violation in `record`. Empty list means valid. NEVER RAISES.

    NEW-01 is the reason for the wrapper. Astra sent `yes_ask = 10**500`; the
    crossed-book check called `float()` on it, `OverflowError` escaped every
    `except` clause in the chain, and the one function whose entire contract
    is "return a structured refusal" raised instead. Downstream that is not a
    cosmetic difference: `SpoolConsumer.pending()` calls this per record, so
    a single hostile row stopped the whole batch -- the AA-09 failure coming
    back through a different door.

    The invariant is therefore stated as code, not as care: an unexpected
    exception anywhere inside the contract becomes a REFUSAL naming the
    exception. Failing closed on a value we do not understand is the only
    answer a validator is allowed to give.
    """
    try:
        return _validate_record(record, require_checksum=require_checksum)
    except ContractError as exc:                              # pragma: no cover
        return [str(exc)]
    except Exception as exc:                                  # noqa: BLE001
        return [f"record could not be validated ({type(exc).__name__}: "
                f"{exc}); a value the contract cannot classify is refused"]


def _validate_record(record, *, require_checksum=True) -> list:
    """The contract proper. `validate_record` is the total wrapper.

    Returns ALL errors rather than the first, because the operator question
    "what is this source still missing" is unanswerable one field per run.

    Used by the producer before a record reaches the spool, by the consumer
    before a snapshot is minted, and by the readiness gate. One definition,
    three callers (AA-02, AA-07).
    """
    errors = []
    if not isinstance(record, dict):
        return ["record is not an object"]

    schema = record.get("schema")
    if schema in LEGACY_FEED_SCHEMAS:
        return [f"schema: {schema!r} is a refused legacy schema; its facts "
                f"may be substituted and must be re-observed"]
    if schema != FEED_SCHEMA:
        return [f"schema: expected {FEED_SCHEMA!r}, got {schema!r}"]

    if require_checksum:
        try:
            verify_checksum(record)
        except ContractError as exc:
            errors.append(str(exc))

    provenance, unavailable = _check_provenance_container(record, errors)

    for field in TEXT_FIELDS:
        try:
            strict_text(record.get(field), field=field)
        except ContractError as exc:
            errors.append(str(exc))
    for field in QUOTE_FIELDS:
        try:
            strict_number(record.get(field), field=field,
                          minimum=0.0, maximum=1.0)
        except ContractError as exc:
            errors.append(str(exc))
    for field in SIZE_FIELDS:
        try:
            strict_number(record.get(field), field=field, minimum=0.0)
        except ContractError as exc:
            errors.append(str(exc))
    for field in TIME_FIELDS:
        try:
            strict_timestamp(record.get(field), field=field)
        except ContractError as exc:
            errors.append(str(exc))

    _check_book(record, errors)
    _check_quote_observation(record, errors)
    _check_contradictions(record, errors)

    if provenance is not None:
        for field in REQUIRED_FIELDS:
            _check_field_provenance(field, record, provenance, unavailable,
                                    errors)
        # AA-05 / AA-08: an OPTIONAL field that carries a value must also
        # carry valid provenance and must not also be declared unavailable.
        # `event_id` is optional in all three components, and this is the one
        # place that decides what "supplied" obliges.
        for field in OPTIONAL_FIELDS:
            value = record.get(field)
            if value in (None, ""):
                continue
            if field in ("catalyst_name", "event_id"):
                try:
                    strict_text(value, field=field)
                except ContractError as exc:
                    errors.append(str(exc))
            if field == "catalyst_time_utc":
                try:
                    strict_timestamp(value, field=field)
                except ContractError as exc:
                    errors.append(str(exc))
            _check_field_provenance(field, record, provenance, unavailable,
                                    errors)

    if unavailable is not None:
        for field in unavailable:
            if field in REQUIRED_FIELDS:
                errors.append(
                    f"{field}: required facts cannot be declared unavailable")

    return errors


def assert_valid(record, *, require_checksum=True) -> dict:
    """`record` if it satisfies the contract, else raise with every reason."""
    errors = validate_record(record, require_checksum=require_checksum)
    if errors:
        raise ContractError("; ".join(errors))
    return record
