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
                f"{field}: {value!r} is a string; numeric strings are not "
                f"accepted as market facts")
        text = value.strip()
        if not text:
            raise ContractError(f"{field}: blank string is not a number")
        try:
            value = float(text)
        except ValueError:
            raise ContractError(f"{field}: {value!r} is not numeric")
    elif isinstance(value, int):
        # An int beyond 2**53 silently loses precision as a float. Detect it
        # on the INTEGER, before the lossy conversion hides the problem.
        if abs(value) > MAX_SAFE_NUMBER:
            raise ContractError(
                f"{field}: {value} exceeds the exactly representable range")
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
                raise ContractError(
                    f"{field}: contradictory aliases -- {first_key}="
                    f"{first_value!r} and {key}={value!r} claim to be the "
                    f"same fact; neither is chosen")
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
    except (TypeError, ValueError) as exc:
        raise ContractError(f"record cannot be canonicalized: {exc}")
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
    try:
        quotes = {f: float(record[f]) for f in QUOTE_FIELDS}
    except (KeyError, TypeError, ValueError):
        return
    if quotes["yes_ask"] < quotes["yes_bid"]:
        errors.append(
            f"book: crossed YES side (bid {quotes['yes_bid']} > ask "
            f"{quotes['yes_ask']})")
    if quotes["no_ask"] < quotes["no_bid"]:
        errors.append(
            f"book: crossed NO side (bid {quotes['no_bid']} > ask "
            f"{quotes['no_ask']})")


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
    """Every contract violation in `record`. Empty list means valid.

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
