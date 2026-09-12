"""Read-only research candidate feed: the producer side of the boundary.

This module is the ONLY thing the trading engine knows about the research
subsystem, and it deliberately knows nothing about it in return. It has no
providers, no models, no ensemble, no ledger and no gateway.
`tests/test_research_feed_boundary.py` pins its import list, so the boundary
cannot widen by accident.

RAW OBSERVATION vs EXECUTION NORMALIZATION (Astra AA-01)
    The engine hands this module two different things and they must never be
    confused:

      * the RAW market as the exchange published it, and
      * the EXECUTION-NORMALIZED book that `MarketValidator.normalize_book`
        produced for the order path.

    Normalization legitimately DERIVES a missing side -- `no_bid = 100 -
    yes_ask`, and a bare `50` when even that is impossible -- because the
    order path needs a complete book to price against. That is correct for
    execution and inadmissible as evidence: a derived quote written into a
    calibration ledger is indistinguishable from an observed one, and every
    number computed from it afterwards is unfalsifiable.

    So quotes are read ONLY from the raw observation, every quote carries
    `quote_observation: observed | derived`, and a candidate containing any
    derived quote is REFUSED. The normalized book is still accepted as a
    parameter, but solely so the producer can tell "the exchange did not
    publish this" apart from "something downstream computed it" and say which
    in the refusal.

WHY IT CANNOT HURT THE ENGINE (Astra AA-10)
    A research feed that can break -- or merely delay -- the money path is
    worse than no research feed. So:

      * it is OFF by default (`RESEARCH_FEED_ENABLED`, strict gate);
      * `emit_candidate` never raises, and never performs I/O. It validates
        in memory and hands the record to a bounded queue drained by a
        SEPARATE writer thread. Serialization, `write`, `fsync` and pruning
        all happen there, so a stalled volume stalls research and nothing
        else;
      * it never touches `PersistenceSentinel`: a failed research write is
        not a critical persistence failure and must never block an order the
        risk engine has approved, nor unblock one;
      * it writes ONLY under its own spool directory, never a state file;
      * the spool is bounded by COUNT and by BYTES, and fails closed when its
        capacity cannot be established (AA-11).
"""

import logging
import os
from datetime import datetime, timezone

from candidate_contract import (AliasContradiction, ContractError,
                                FEED_SCHEMA, QUOTE_DERIVED,
                                QUOTE_FIELDS, QUOTE_OBSERVED, SOURCE_BINDING,
                                compute_checksum, iso_second, provenance_path,
                                resolve_alias, safe_render, strict_number,
                                validate_record)
from config import CFG
from research_spool import BoundedSpool, ResearchWriter

log = logging.getLogger("RESEARCH_FEED")

#: Subdirectory under DATA_DIR. Its own directory, so the producer's writes
#: can never collide with a state file and a test can assert exactly which
#: bytes the feed is allowed to touch.
SPOOL_DIRNAME = "research_spool"

#: Exchange quotes arrive in CENTS. A quote outside this range is not a
#: probability price and is never clamped into one.
MIN_CENTS, MAX_CENTS = 0.0, 100.0

#: Facts read from the raw market object, and the alias keys the exchange uses
#: for each. Derived from the shared contract so the producer cannot drift
#: from the consumer (AA-02).
MARKET_FIELDS = tuple(f for f, (ns, _k) in SOURCE_BINDING.items()
                      if ns == "market")


def spool_dir() -> str:
    return os.path.join(CFG.DATA_DIR, SPOOL_DIRNAME)


def _diagnostic(value) -> str:
    """A conflicting alias value, rendered for a human to read.

    This is the ONE place in the producer where a non-string becomes a
    string, and it is safe precisely because the result is never a fact: it
    lands in `contradictory_fields`, which the contract treats as a reason to
    REFUSE the record. Nothing downstream can mistake it for an observation,
    because a record carrying it never becomes a snapshot.

    A non-string keeps its type in the rendering (`int:12345`) so the AA-02
    lesson survives here too: the integer `12345` and the string `"12345"`
    are different claims, and a diagnostic that flattens them would leave an
    operator unable to see which the exchange actually sent.

    V4-RA-03 -- IT RUNS ON THE ENGINE'S THREAD, SO IT IS TOTAL AND BOUNDED
        The previous body was `f"{type(value).__name__}:{value!r}"` followed
        by a slice. Both halves were wrong for the thread it runs on:

          * `value!r` executes `__repr__` -- code the EXCHANGE supplied. One
            that raises propagated out of `candidate_from_market`, into
            `_shadow_observer`'s `except`, and from there into a synchronous
            `log.debug` on the engine's own thread, where a held logging
            handler stalls the decision cycle.
          * the slice bounds the RESULT, not the WORK. A `__repr__` returning
            100MB cost the observer 1.3 seconds and a 100MB allocation to
            retain 200 characters.

        `safe_render` is total and bounded, so neither is reachable. The
        rendering is deliberately in the shared contract rather than here:
        `resolve_alias` builds the same kind of string from the same kind of
        value on the same thread, and one primitive means one guarantee.
    """
    return safe_render(value, limit=200)


#: The identity keys a settlement-source OBJECT may carry. Both are text and
#: both belong to the authority's identity: `name` says WHO settles the
#: market, `url` says WHERE that authority publishes the number. RA-02: a
#: normalization that keeps the first and drops the second is not a
#: canonical identity, it is a lossy rendering.
SOURCE_IDENTITY_KEYS = ("name", "url")

#: V4-RA-01. The COMPLETE schema of a settlement-source object: the only keys
#: a member may carry at all, not merely the ones this producer reads.
#:
#: It is the same tuple as `SOURCE_IDENTITY_KEYS` and that is the point. The
#: previous version looped over the keys it UNDERSTOOD and ignored everything
#: else, so `{"name": "CF Benchmarks RTI", "source_id": 7}` normalized to the
#: authority "CF Benchmarks RTI" with `source_id` silently discarded -- and
#: `source_id: 7` and `source_id: 9` then produced BYTE-IDENTICAL records and
#: the same `record_sha256`.
#:
#: WHY REJECTION RATHER THAN RETENTION
#:   The finding permits either, provided an extension that is supported is
#:   also validated and RETAINED. Retention is the wider change: the value
#:   that survives into the record is a TEXT rendering, the comparison that
#:   detects contradictory aliases is performed on that same text (V4-RA-02),
#:   and an arbitrary extension can only be retained injectively by inventing
#:   a new rendering grammar for values whose types the exchange has never
#:   documented. So no extension is supported, and an unsupported key makes
#:   the member MALFORMED -- a member this producer cannot claim to have
#:   understood, which is the honest verdict rather than a convenient one.
SOURCE_OBJECT_SCHEMA = SOURCE_IDENTITY_KEYS

#: The longest a single identity value may be, in characters. Part of the
#: schema rather than a performance tweak, for two reasons:
#:
#:   V4-RA-01  a schema that says which KEYS are allowed and nothing about
#:             how large their values may be is not a complete schema.
#:   V4-RA-03  `render_settlement_source` escapes character by character, on
#:             the ENGINE's observer thread. A 40,000,000-character name cost
#:             the decision cycle 3.74 SECONDS, measured -- to produce a
#:             record the spool's `max_record_bytes` then refuses anyway, on
#:             a different thread. Capping the input bounds the cycle's work;
#:             capping the OUTPUT would not, and truncating the value would
#:             be exactly the information loss V4-RA-02 is about.
#:
#: Generous against reality: the longest settlement source Kalshi publishes
#: is a short authority name and an ordinary URL. Anything past this is
#: REFUSED, never shortened.
MAX_IDENTITY_CHARS = 2048

#: The most members a settlement-source collection may carry. Same argument:
#: a market settled by more than this many authorities is not a shape this
#: producer has a definition for, and reading it costs the decision cycle
#: time proportional to a number the exchange chooses.
MAX_SOURCE_MEMBERS = 64

#: Characters the rendering below escapes so that the text form is INJECTIVE:
#: distinct structured identities must never render to the same string, or the
#: comparison that detects contradictory aliases is comparing renderings
#: rather than facts (RA-02).
_RENDER_ESCAPES = {"\\": "\\\\", "|": "\\|", "<": "\\<", ">": "\\>"}


class MalformedSettlementSource(Exception):
    """A settlement-source container this producer cannot claim to understand.

    Raised rather than returned so that "the exchange published nothing" and
    "the exchange published something we cannot read" stay different facts
    inside this module, even though both end as an ABSENT `resolution_source`
    in the record. The distinction is what stops a partly-read member from
    being reported as a fully-read one (RA-01).
    """


def _identity_text(value, key: str):
    """Non-blank text for one identity key, `None` when JSON-absent, or raise.

    A JSON `null` is how a feed says "not published", so it reads as ABSENT.
    Anything else that is not non-blank text is MALFORMED: an integer URL is
    a plausible internal field and a wholly implausible publication location,
    and a blank one is "published an empty URL", which is not "published no
    URL".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise MalformedSettlementSource(
            f"{key} is {type(value).__name__}, not text")
    if len(value) > MAX_IDENTITY_CHARS:
        # Checked on the RAW value, before `strip` copies it, and refused
        # rather than truncated: a shortened authority name is a different
        # authority (V4-RA-02), and the cost of rendering this one is paid by
        # the decision cycle (V4-RA-03).
        raise MalformedSettlementSource(
            f"{key} is {len(value)} characters, past the "
            f"{MAX_IDENTITY_CHARS}-character bound on one identity value; it "
            f"is refused rather than shortened")
    text = value.strip()
    if not text:
        raise MalformedSettlementSource(f"{key} is blank")
    return text


def settlement_source_identity(value):
    """The CANONICAL STRUCTURED identity of a settlement source, or None.

    Returns a tuple of MEMBERS, each member a tuple of sorted `(key, text)`
    pairs -- so collection boundaries and per-member URLs both survive into
    the value that gets compared and rendered.

    RA-01: EVERY MEMBER, AND THE WHOLE CONTAINER, BEFORE NORMALIZATION.
        The previous version looped `for key in ("name", "url")` and RETURNED
        on the first key that was present and readable. With `name` present,
        `url` was never examined at all, so

            {"name": "CF Benchmarks RTI", "url": 8080}

        was accepted as the authority "CF Benchmarks RTI" while the malformed
        half of the same object went unread. AA-02's own rule -- one malformed
        member taints the collection -- was correct and simply never reached.

        Here there is no early return. Every identity key present on a member
        is validated, every member of a list is validated, and a container
        whose shape this producer does not recognise -- a mapping with no
        identity key, a bare number, a boolean -- is MALFORMED rather than
        quietly empty.

    RA-02: STRUCTURE IS THE IDENTITY.
        `", ".join(names)` destroyed exactly the two things that distinguish
        two authorities. `[{"name": "A"}, {"name": "B"}]` (two authorities)
        and `[{"name": "A, B"}]` (one authority whose name contains a comma)
        rendered identically, and the URL was dropped altogether -- so two
        alias keys naming one authority at two DIFFERENT locations compared
        EQUAL, and `resolve_alias` resolved a real contradiction silently.

    V4-RA-03: EVERY FAILURE TO READ IS `MalformedSettlementSource`.
        This runs on the engine's observer thread, so it must have exactly
        two outcomes: a canonical identity, or the one exception its callers
        classify. A container can misbehave in ways that are neither -- a
        mapping whose `__iter__` raises, a sequence whose `__len__` raises, a
        `__getitem__` that fails -- and each of those used to escape
        `_settlement_source_name` and `settlement_source_comparator`, both of
        which catch only `MalformedSettlementSource`, and propagate into the
        engine. Semantically they are the SAME fact: a container this
        producer cannot even traverse is one it cannot claim to have
        understood.
    """
    try:
        return _source_collection(value)
    except MalformedSettlementSource:
        raise
    except Exception as exc:                                  # noqa: BLE001
        raise MalformedSettlementSource(
            f"the settlement-source container could not be read "
            f"({type(exc).__name__}); a container this producer cannot "
            f"traverse is one it cannot claim to have understood") from exc


def _source_member(value):
    """The canonical identity of ONE settlement-source member. Never empty.

    A member is a non-blank string, or an object carrying ONLY the keys in
    `SOURCE_OBJECT_SCHEMA` and at least one of them non-null. Everything else
    raises: a member this producer can only partly account for is not a
    member it understood.
    """
    if isinstance(value, bool):
        raise MalformedSettlementSource("a boolean is not a settlement source")
    if isinstance(value, str):
        # `_identity_text` refuses a blank string, so this is non-blank text
        # or an exception; it is never the empty identity.
        return (("name", _identity_text(value, "name")),)
    if isinstance(value, dict):
        # V4-RA-01: THE WHOLE OBJECT, INCLUDING WHAT WE DO NOT UNDERSTAND.
        # Checked BEFORE any key is read, so no part of an object carrying an
        # unsupported key is ever canonicalized -- "canonicalize only fully
        # validated structures" is an ordering property, not a wish.
        # Short-circuits, and that is a bound rather than an optimisation:
        # `sorted(str(k) for k in value)` walks a mapping whose size the
        # EXCHANGE chooses, on the engine's observer thread (V4-RA-03). Only
        # two keys can ever be supported, so this stops after at most ten
        # iterations whatever arrives. `safe_render` because a key can be any
        # hashable, including one whose `__repr__` misbehaves.
        unsupported = []
        for key in value:
            if key in SOURCE_OBJECT_SCHEMA:
                continue
            unsupported.append(safe_render(key, limit=64))
            if len(unsupported) >= 8:
                break
        if unsupported:
            raise MalformedSettlementSource(
                f"settlement-source object carries unsupported key(s) "
                f"{sorted(unsupported)!r}; this producer has no definition "
                f"for them, so it cannot claim to have read this member, and "
                f"dropping them would make two different objects one record")
        fields = []
        for key in SOURCE_OBJECT_SCHEMA:
            if key not in value:
                continue
            text = _identity_text(value[key], key)
            if text is not None:
                fields.append((key, text))
        if not fields:
            raise MalformedSettlementSource(
                f"no settlement-source identity among "
                f"{sorted(str(k) for k in value)[:8]!r}; a container with no "
                f"name and no url is not an empty source, it is an "
                f"unrecognised one")
        return tuple(sorted(fields))
    if isinstance(value, (list, tuple)):
        # V4-RA-01/V4-RA-02: A NESTED CONTAINER IS NOT A MEMBER.
        # `members.extend(...)` used to FLATTEN it, so `[[{"name": "A"}],
        # {"name": "B"}]` and `[{"name": "A"}, {"name": "B"}]` canonicalized
        # to the same value and hashed to the same record. Kalshi publishes a
        # FLAT list of source objects; a list inside that list is a shape this
        # producer has no definition for, and inventing one -- flattening --
        # is how the grouping the exchange published stopped existing.
        raise MalformedSettlementSource(
            f"a settlement-source collection member is itself a "
            f"{type(value).__name__} of {len(value)}; nesting is not a shape "
            f"this producer understands, and flattening it would erase the "
            f"grouping the source published")
    raise MalformedSettlementSource(
        f"{type(value).__name__} is not a settlement source")


def _source_collection(value):
    """The canonical identity of a settlement-source CONTAINER, or `()`.

    `()` -- the empty identity -- is reachable for exactly ONE input: an empty
    list or tuple at the TOP LEVEL, which is how a feed says "this market has
    no settlement sources". It reads as ABSENT, and `resolution_source` is a
    REQUIRED field, so a record built from it is refused rather than spooled.

    An empty container anywhere BELOW the top level is a different claim and
    is refused by `_source_member`, which admits no nested container at all:
    `[{"name": "A"}, []]` used to canonicalize exactly like `[{"name": "A"}]`,
    so a member the exchange published as empty VANISHED from a record that
    no longer mentioned it (V4-RA-01) -- and the two raw structures produced
    one identical `record_sha256` (V4-RA-02).
    """
    if isinstance(value, (list, tuple)):
        if not value:
            return ()
        if len(value) > MAX_SOURCE_MEMBERS:
            raise MalformedSettlementSource(
                f"a settlement-source collection of {len(value)} members is "
                f"past the {MAX_SOURCE_MEMBERS}-member bound; reading it "
                f"costs the decision cycle time proportional to a number the "
                f"exchange chooses")
        members = []
        for item in value:
            # No early exit on success and none on failure either: a
            # malformed member raises, which taints the whole collection,
            # because a settlement source list that is half readable is not
            # half true.
            members.append(_source_member(item))
        return tuple(members)
    # A bare object or string: the single-source shape. One member, and
    # deliberately indistinguishable from the one-element list carrying it --
    # they are the same claim in two spellings, and refusing that equivalence
    # would make the producer useless against a feed that uses both.
    return (_source_member(value),)


def _escape_identity(text: str) -> str:
    return "".join(_RENDER_ESCAPES.get(ch, ch) for ch in text)


def _unescape_identity(text: str) -> str:
    """The inverse of `_escape_identity`. See `parse_settlement_source`."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(text[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_unescaped(text: str, separator: str):
    """`text` split on `separator`, ignoring separators inside an escape.

    A single left-to-right scan that steps OVER a backslash and the character
    it protects. Necessary rather than tidy: `str.split` and `str.find` cannot
    see escaping, so an authority named `A | B` -- rendered `A \\| B` -- looked
    like two members to a naive split, which is precisely the collapse
    `_escape_identity` exists to prevent.
    """
    parts, buffer, index = [], [], 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            buffer.append(text[index:index + 2])
            index += 2
            continue
        if text.startswith(separator, index):
            parts.append("".join(buffer))
            buffer = []
            index += len(separator)
            continue
        buffer.append(text[index])
        index += 1
    parts.append("".join(buffer))
    return parts


def _find_unescaped(text: str, char: str) -> int:
    """Index of the first UNESCAPED `char`, or -1."""
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            index += 2
            continue
        if text[index] == char:
            return index
        index += 1
    return -1


def _last_unescaped(text: str) -> int:
    """Index of the last character the escape-aware scan lands on, or -1."""
    index, last = 0, -1
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            index += 2
            continue
        last = index
        index += 1
    return last


def _ends_unescaped(text: str, char: str) -> bool:
    """Does `text` end with an UNESCAPED `char`?

    `A\\>` ends with the character `>`, but that `>` is escaped data rather
    than the closing delimiter. The test is whether the escape-aware scan
    actually lands on the final position.
    """
    return bool(text) and text.endswith(char) \
        and _last_unescaped(text) == len(text) - 1


def parse_settlement_source(rendered: str):
    """The canonical identity a rendering came from. THE INJECTIVITY PROOF.

    V4-RA-02 requires that the representation which is COMPARED be the same
    complete representation that is later retained and hashed. It is: the
    comparator compares this rendering, and the record carries this rendering.
    That only preserves every distinction if the rendering is INJECTIVE, and
    "it is injective" was previously an argument in a docstring.

    This function makes it a decidable property instead. Escaping guarantees
    that no raw `\\`, `|`, `<` or `>` survives inside a name or a URL, so
    ` | ` splits members unambiguously and the first raw `<` separates a name
    from its URL. `round_trips(identity)` below asserts the consequence, and
    `tests/test_astra_v4_ra01_ra04.py` runs it over a hostile corpus rather
    than trusting the reasoning.
    """
    if not rendered:
        return ()
    members = []
    for part in _split_unescaped(rendered, " | "):
        opened = _find_unescaped(part, "<")
        if opened == -1 or not _ends_unescaped(part, ">"):
            # A name-only member. `render_settlement_source` never leaves an
            # unescaped `<` in one, so reaching here with one would mean the
            # text did not come from that function.
            if opened != -1:                    # pragma: no cover
                raise MalformedSettlementSource(
                    f"{part!r} opens a URL it never closes")
            members.append((("name", _unescape_identity(part)),))
            continue
        fields = [("url", _unescape_identity(part[opened + 1:-1]))]
        if opened:
            # `render_settlement_source` writes exactly one space between the
            # name and `<`, so that space is the delimiter and not part of a
            # name -- a name cannot end in whitespace, because
            # `_identity_text` strips before it accepts.
            fields.append(("name",
                           _unescape_identity(part[:opened - 1])))
        members.append(tuple(sorted(fields)))
    return tuple(members)


def round_trips(identity) -> bool:
    """Does `identity` survive rendering and parsing unchanged?

    The executable form of "the retained text loses nothing". Used by the
    tests, and deliberately defined HERE beside the two functions it relates,
    so a change to either one is a change to the thing that checks them.
    """
    try:
        return parse_settlement_source(
            render_settlement_source(identity)) == tuple(identity)
    except MalformedSettlementSource:           # pragma: no cover
        return False


def render_settlement_source(identity) -> str:
    """Readable AND injective text for a canonical structured identity.

    `name <url>`, members joined by ` | `, with the backslash, pipe and
    angle-bracket characters escaped inside every name and URL. The escaping is what makes the
    rendering injective: without it, one authority literally named `A | B`
    and two authorities `A` and `B` would produce the same record field, and
    RA-02 would be re-opened in the rendering after being closed in the
    comparison.

    V4-RA-02: `parse_settlement_source` is the inverse, and `round_trips`
    states the property this docstring used to merely assert. Injectivity
    matters more since V4-RA-02 than it did before, because this text is now
    what the alias comparator compares -- the SAME representation the record
    retains and the digest covers, rather than a second one that could agree
    where the record disagrees.
    """
    parts = []
    for member in identity:
        fields = dict(member)
        name = fields.get("name")
        url = fields.get("url")
        if name and url:
            parts.append(f"{_escape_identity(name)} "
                         f"<{_escape_identity(url)}>")
        elif url:
            parts.append(f"<{_escape_identity(url)}>")
        elif name:
            parts.append(_escape_identity(name))
    return " | ".join(parts)


class _SourceVerdict:
    """A comparator answer that is NOT a rendered identity.

    Deliberately an object rather than a marker string. A string sentinel
    spelled `"<malformed settlement source>"` is FORGEABLE: a member published
    as `{"url": "malformed settlement source"}` renders to exactly those
    characters, so a real identity would have compared equal to an unreadable
    one and V4-RA-02 would have been re-opened by the fix for it. An object
    compares equal only to itself, whatever the exchange publishes.
    """

    __slots__ = ("label",)

    def __init__(self, label):
        self.label = label

    def __repr__(self):                                 # pragma: no cover
        return f"<{self.label}>"


#: What the comparator returns for a container it could not read at all, and
#: for one that validly published nothing. They are DIFFERENT sentinels on
#: purpose (V4-RA-02): "we cannot read this" and "the exchange says there are
#: none" are different facts, and a comparator that returns the same value for
#: both reports a real disagreement between two aliases as agreement.
_SOURCE_MALFORMED = _SourceVerdict("malformed settlement source")
_SOURCE_NONE_PUBLISHED = _SourceVerdict("no settlement source published")


def settlement_source_comparator(value):
    """What `resolve_alias` compares two `resolution_source` aliases BY.

    V4-RA-02 -- THE COMPARED REPRESENTATION IS THE RETAINED ONE.
        This used to return the structured identity while the record retained
        `render_settlement_source(identity)`. Two representations of one fact
        is two chances to disagree: a distinction the comparison kept could
        still be lost by the rendering, and a distinction the rendering kept
        could be invisible to the comparison. The invariant the finding states
        is that the representation compared is the COMPLETE representation
        retained and hashed, so this returns exactly the text that ends up in
        `resolution_source` -- and `parse_settlement_source` is the proof that
        that text loses nothing.

        The structure is still what decides the answer; it is simply carried
        through a rendering that is invertible instead of a second encoding
        that is merely similar.

    Malformed containers still all compare equal to each other -- "we cannot
    read this" is one fact however it is misspelled -- and a field both
    aliases agree is unreadable is ABSENT, which the contract refuses anyway.
    """
    try:
        identity = settlement_source_identity(value)
        if not identity:
            return _SOURCE_NONE_PUBLISHED
        return render_settlement_source(identity)
    except MalformedSettlementSource:
        return _SOURCE_MALFORMED
    except Exception:                                         # noqa: BLE001
        # V4-RA-03: `resolve_alias` calls this ON THE ENGINE'S THREAD, and a
        # comparator that raises makes the whole normalization raise. There
        # is no failure here that is not "we cannot read this".
        return _SOURCE_MALFORMED


def _settlement_source_name(value):
    """The settlement authority the exchange PUBLISHED, as text, or None.

    Kalshi records settlement sources as a list of objects. This reads what it
    published -- every member, and the URL as well as the name -- and never
    invents one, never falls back to the exchange's own name because the
    market is listed there, and never treats an empty name as a reason to
    report the URL as the name.
    """
    try:
        identity = settlement_source_identity(value)
    except MalformedSettlementSource:
        return None
    if not identity:
        return None
    return render_settlement_source(identity) or None


def _present_aliases(market, keys):
    """`(key, value)` for every alias key the source actually supplied.

    Used only to build the evidence for a field whose normalization failed
    outright (V4-RA-03). Bounded and total by construction: it reads keys and
    never renders a value -- the caller does that through `safe_render`.
    """
    try:
        return [(key, market[key]) for key in keys if key in market]
    except Exception:                                         # noqa: BLE001
        return []


def observed_cents(source: dict, key: str):
    """A quote the exchange actually published, in cents, or None.

    Strict on purpose: a string quote, a boolean, a NaN or an out-of-range
    number is treated as NOT OBSERVED rather than repaired. This is the one
    place where "the exchange published a number we can read" is decided, and
    a lenient parser here is how a malformed quote becomes a market fact.

    V4-RA-03: TOTAL, NOT MERELY STRICT.
        `except ContractError` was the whole guard, and `strict_number` does
        not only raise `ContractError`: a quote of `10 ** 5000` made it raise
        `ValueError` from inside the construction of its own refusal message,
        and a mapping with a hostile `__getitem__` makes `source[key]` raise
        anything at all. Either escaped this function, escaped
        `candidate_from_market`, and reached the engine's observer, whose
        `except` then logged synchronously.

        Widening the catch does not weaken the strictness: every branch here
        still ends in "NOT OBSERVED", which is refusal, not repair. What
        changes is that a value we cannot even read reaches that verdict
        instead of reaching the decision cycle.
    """
    try:
        if not isinstance(source, dict) or key not in source:
            return None
        return strict_number(source[key], field=key,
                             minimum=MIN_CENTS, maximum=MAX_CENTS)
    except ContractError:
        return None
    except Exception:                                         # noqa: BLE001
        return None


class ResearchFeed:
    """Non-blocking producer in front of a bounded, durable spool."""

    def __init__(self, directory: str = None, writer: ResearchWriter = None,
                 *, start_writer: bool = True):
        self.directory = directory or spool_dir()
        self.rejected = 0
        #: Refusals caused specifically by an incomplete or DERIVED source, as
        #: opposed to a malformed candidate. Surfaced in `stats()` so "Alpha
        #: is getting nothing" can be told apart from "the market is quiet".
        self.refused_incomplete = 0
        self.refused_derived = 0
        self.last_errors = []
        if writer is not None:
            self.writer = writer
        else:
            spool = BoundedSpool(
                self.directory,
                max_records=int(CFG.RESEARCH_FEED_MAX_SPOOL),
                max_bytes=int(CFG.RESEARCH_FEED_MAX_BYTES),
                max_record_bytes=int(CFG.RESEARCH_FEED_MAX_RECORD_BYTES),
                max_age_s=float(CFG.RESEARCH_FEED_MAX_AGE_S))
            self.writer = ResearchWriter(
                spool,
                max_queue=int(CFG.RESEARCH_FEED_QUEUE_MAX),
                max_queue_bytes=int(CFG.RESEARCH_FEED_QUEUE_MAX_BYTES),
                start=start_writer)
        # RA-03: the writer thread is what assembles, hashes and validates.
        # Wired after BOTH branches, on purpose: an INJECTED writer -- which
        # is how most tests and the readiness gate drive this -- must get the
        # same finalizer, and therefore the same verdicts, as production.
        #
        # Bound LATE, through the instance, and not as `self._finalize`. A
        # bound method captured here would freeze the original function, so a
        # test -- or a mutation -- that replaces `_finalize` on the class
        # would leave the writer calling the unpatched one and pass while
        # testing nothing. That is the false-green class AA-17 exists for, so
        # the indirection is deliberate rather than incidental.
        self.writer.finalizer = lambda candidate: self._finalize(candidate)

    # ── the one method the engine calls ─────────────────────────────────
    def observe_market(self, market, book, *, raw_book=None,
                       cycle_id: str = "") -> bool:
        """Normalize one observed market and offer it. TOTAL and NON-BLOCKING.

        V4-RA-03. This exists so that the ENGINE's observer has exactly one
        call to make and no error path of its own to write. Before it,
        `ExecutionEngine._shadow_observer` did this:

            try:
                self.research_feed.emit_candidate(candidate_from_market(...))
            except Exception as e:
                log.debug(f"research feed: {e}")

        and both halves of that were on the decision cycle's thread.
        `candidate_from_market` is research NORMALIZATION -- it could raise,
        from inside the construction of a refusal message over a value the
        exchange supplied -- and the `except` then performed a SYNCHRONOUS
        `log.debug`. A logging handler held by another thread stalls that
        call, so the money path waited on research after all, through the
        error path instead of through the data path.

        Everything that could go wrong is therefore absorbed HERE, on this
        side of the boundary, and reported through `_note` -- `put_nowait` on
        a bounded queue drained by the writer thread. The engine cannot learn
        that anything failed, because there is no action it could take on the
        answer that would not couple the two paths again.

        Returns True only when a candidate was ADMITTED, for telemetry and
        tests. The engine ignores it.
        """
        if not CFG.RESEARCH_FEED_ENABLED:
            # The strict gate, read BEFORE normalizing rather than inside
            # `emit_candidate` afterwards. With research OFF, the engine was
            # still paying `candidate_from_market` for every candidate of
            # every cycle and then throwing the result away at the gate. An
            # OFF subsystem should cost the decision cycle nothing at all.
            return False
        try:
            candidate = candidate_from_market(
                market, book, raw_book=raw_book, cycle_id=cycle_id)
            return self.emit_candidate(candidate)
        except Exception as exc:                              # noqa: BLE001
            # `type(exc).__name__` reads a slot on the class: bounded, and it
            # cannot execute anything the exchange wrote. `str(exc)` can do
            # both -- the message may embed a raw source value -- so the
            # exception's own argument goes through the bounded renderer
            # rather than being interpolated.
            self.rejected += 1
            detail = safe_render(exc.args[0] if exc.args else None, limit=120)
            self._note(logging.WARNING,
                       f"[RESEARCH_FEED] a market could not be normalized "
                       f"({type(exc).__name__}: {detail}); it is REFUSED, "
                       f"and counted, rather than allowed to reach the cycle")
            return False

    def note_observer_failure(self, detail: str) -> None:
        """Report an observer-side failure WITHOUT touching a device.

        The engine's last-resort path (V4-RA-03). `observe_market` is total,
        so this should be unreachable; it exists because "should be" is not a
        guarantee, and the guarantee the engine needs is that its fallback
        cannot block either. Public because the caller is `execution_engine`,
        and a private name would have invited it to log instead.
        """
        self._note(logging.WARNING,
                   f"[RESEARCH_FEED] observer-side failure: "
                   f"{safe_render(detail, limit=200)}")

    def emit_candidate(self, candidate: dict) -> bool:
        """Offer one candidate to the writer. True when it was ACCEPTED.

        NEVER raises and NEVER performs I/O. The caller is a decision cycle;
        a research feed that can propagate an exception -- or an fsync -- into
        it has become part of the money path by the back door.

        RA-03 -- AND IT NEVER HASHES, SERIALIZES OR VALIDATES EITHER
            AA-10 moved `write`, `fsync` and `prune` off this thread, and its
            re-audit moved `log` off too. The CPU work stayed: every candidate
            of every cycle was serialized with `json.dumps`, hashed with
            sha256 and walked field-by-field against the full contract on the
            engine's own thread, and a refusal then interpolated the whole
            error list into a diagnostic string before handing it over.

            None of that is free and none of it is the observer's business.
            What happens here is ADMISSION: type checks, and a shallow copy of
            the containers the writer will read. Assembly, hashing and
            validation happen on the writer's thread, where a slow record
            costs research latency and nothing else.

        True means "ADMITTED", not "valid" and not "durable". The producer
        deliberately has no way to learn whether the bytes reached the disk,
        because there is no action the engine could take on that answer -- and
        since RA-03 it has no way to learn the contract verdict either, for
        exactly the same reason. `rejected`, `refused_incomplete` and
        `refused_derived` in `stats()` are how the verdicts are reported.
        """
        if not CFG.RESEARCH_FEED_ENABLED:
            return False
        try:
            admitted = self._admit(candidate)
            if admitted is None:
                return False
            return self.writer.offer_candidate(
                admitted, approx_bytes=self._size(admitted))
        except Exception as e:                                # noqa: BLE001
            self.rejected += 1
            # AA-10 (re-audit): DEFERRED, not logged. `log.warning` here runs
            # the handler on the engine's thread, and the handler writes to
            # the same volume the fsync was moved off.
            self._note(logging.WARNING,
                       f"[RESEARCH_FEED] candidate dropped: "
                       f"{type(e).__name__}: {e}")
            return False

    def _note(self, level: int, message: str) -> None:
        """Say something WITHOUT touching a device (AA-10).

        Everything the producer has to report is handed to the writer and
        emitted on its thread. `note()` is `put_nowait` on a bounded queue:
        it cannot block, and under pressure the diagnostic is dropped and
        counted rather than allowed to slow the cycle down.
        """
        try:
            self.writer.note(level, message)
        except Exception:                                     # noqa: BLE001
            # A failure to say something is never allowed to become a failure
            # of the thing that was trying to speak.
            pass

    @staticmethod
    def _size(record: dict) -> int:
        """Cheap in-memory size estimate for the queue's byte budget.

        Deliberately an estimate: serializing to be exact would put JSON
        encoding of the full record back inside the decision cycle, which is
        the whole of RA-03.
        """
        return 512 + 2 * sum(len(str(v)) for v in record.values())

    # ── observer side: admission, and nothing else (RA-03) ───────────
    def _admit(self, candidate):
        """The ONLY work the observer's thread does: type checks and a copy.

        No hashing, no serialization, no contract walk, no diagnostic
        formatting and no device. `tests/test_astra_v4_remediation.py` pins
        that statically as well as by timing, because a timing test can only
        prove the calls that happened to run.

        The copy is shallow and it is not an optimisation: the record is
        assembled on ANOTHER thread now, so the caller must be free to reuse
        or mutate its candidate the moment this returns. A record whose
        fields could change underneath the digest that covers them would make
        the digest a claim about nothing.
        """
        if not isinstance(candidate, dict):
            self.rejected += 1
            return None
        provenance = candidate.get("field_provenance")
        unavailable = candidate.get("unavailable_fields")
        observation = candidate.get("quote_observation")
        contradictions = candidate.get("contradictory_fields") or {}
        if not isinstance(provenance, dict) \
                or not isinstance(unavailable, list) \
                or not isinstance(observation, dict) \
                or not isinstance(contradictions, dict):
            self.rejected += 1
            self._note(logging.DEBUG,
                       "[RESEARCH_FEED] candidate carries no provenance "
                       "container")
            return None
        admitted = dict(candidate)
        admitted["field_provenance"] = dict(provenance)
        admitted["unavailable_fields"] = list(unavailable)
        admitted["quote_observation"] = dict(observation)
        admitted["contradictory_fields"] = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in contradictions.items()}
        return admitted

    # ── construction: WRITER THREAD ONLY (RA-03) ───────────────────
    def _build(self, candidate: dict):
        """Admit and finalize in one call. The whole producer path.

        Kept as one function because that is what a reader wants when
        asking "what record does this candidate produce"; the engine
        never calls it, because half of it belongs on the writer's
        thread. `emit_candidate` calls `_admit`, and `ResearchWriter`
        calls `_finalize` on its own thread (RA-03).
        """
        admitted = self._admit(candidate)
        if admitted is None:
            return None
        return self._finalize(admitted)

    def _finalize(self, candidate: dict):
        """Assemble, hash and validate. TOTAL: it returns None, never raises.

        RA-03 moved this onto the writer's thread, and that move changed who
        catches its failures. `emit_candidate` used to wrap it, so a record
        the contract could not even be HASHED -- a NaN quote, which
        `canonical_json` refuses outright with `allow_nan=False` -- was
        counted as a rejection and reported in `stats()`. On the writer's
        thread the same exception would land in `ResearchWriter._handle`'s
        general catch, which logs and moves on: the record would still be
        refused, and the producer's own refusal counters would say nothing
        happened.

        This is NEW-01's lesson arriving by a different route -- a function
        that raises instead of refusing pushes the decision to a caller that
        cannot classify it -- so the totality lives HERE, where the counters
        are. `tests/test_astra_v4_remediation.py` patches internals to raise
        and asserts the counters still move.
        """
        try:
            return self._finalize_record(candidate)
        except Exception as exc:                              # noqa: BLE001
            self.rejected += 1
            self._note(logging.WARNING,
                       f"[RESEARCH_FEED] candidate could not be finalized "
                       f"({type(exc).__name__}: {exc}); it is REFUSED, and "
                       f"counted, rather than dropped silently")
            return None

    def _finalize_record(self, candidate: dict):
        """One spool record, or None. Never substitutes an absent fact.

        The candidate arriving here already carries its own provenance and its
        per-quote observed/derived verdict (see `candidate_from_market`). This
        method re-derives nothing: it assembles the record, hashes it
        INCLUDING the provenance and the quote verdicts, and then validates
        the whole thing against the SHARED contract -- the same function the
        consumer and the readiness gate call, so a record that reaches the
        spool is one the consumer can mint from.
        """
        candidate = self._admit(candidate)
        if candidate is None:
            return None
        provenance = candidate["field_provenance"]
        unavailable = candidate["unavailable_fields"]
        observation = candidate["quote_observation"]
        contradictions = candidate["contradictory_fields"]

        content = {
            "schema": FEED_SCHEMA,
            # AA-06: the OBSERVATION time, stamped by the observer that saw
            # the market. Never defaulted here and never re-stamped later: a
            # record replayed an hour after it was written must mint the same
            # snapshot identity it would have minted at the time.
            "emitted_at_utc": candidate.get("emitted_at_utc"),
            "contract_id": candidate.get("contract_id"),
            "event_id": candidate.get("event_id"),
            "question": candidate.get("question"),
            "resolution_rules": candidate.get("resolution_rules"),
            "resolution_source": candidate.get("resolution_source"),
            "market_close_time_utc": candidate.get("market_close_time_utc"),
            "expected_resolution_time_utc":
                candidate.get("expected_resolution_time_utc"),
            "catalyst_name": candidate.get("catalyst_name") or None,
            "catalyst_time_utc": candidate.get("catalyst_time_utc") or None,
            "volume": candidate.get("volume"),
            "open_interest": candidate.get("open_interest"),
            # Audit trail for which cycle produced the candidate. Deliberately
            # NOT the decision: the research path must not learn what the
            # engine decided, or the two stop being independent.
            "source": str(candidate.get("source") or "scanner"),
            "cycle_id": str(candidate.get("cycle_id") or ""),
            "field_provenance": {str(k): str(v)
                                 for k, v in sorted(provenance.items())},
            "unavailable_fields": sorted({str(f) for f in unavailable}),
            "quote_observation": {str(k): str(v)
                                  for k, v in sorted(observation.items())},
            # AA-03: inside the digest, so a contradiction cannot be edited
            # out of a record without invalidating it.
            "contradictory_fields": {
                str(f): {str(k): _diagnostic(v) for k, v in sorted(vals.items())}
                for f, vals in sorted(contradictions.items())},
            **{f: candidate.get(f) for f in QUOTE_FIELDS},
        }
        content["record_sha256"] = compute_checksum(content)

        errors = validate_record(content)
        if errors:
            self.rejected += 1
            self.last_errors = errors
            derived = [e for e in errors if "not directly observed" in e]
            if derived:
                self.refused_derived += 1
                self._note(logging.INFO,
                           f"[RESEARCH_FEED] {content.get('contract_id')} NOT "
                           f"emitted -- {len(derived)} quote(s) were DERIVED "
                           f"by execution normalization, not observed: "
                           f"{derived}")
            else:
                self.refused_incomplete += 1
                # Named, at INFO, because this is the visible research signal
                # that the source is not yet carrying the facts Alpha needs.
                # It is a research failure and nothing else: no sentinel, no
                # decision -- and, since AA-10, not a log call on the caller's
                # thread either.
                self._note(logging.INFO,
                           f"[RESEARCH_FEED] {content.get('contract_id')} NOT "
                           f"emitted -- {errors}; these are never "
                           f"reconstructed")
            return None
        return content

    def stats(self) -> dict:
        return {"rejected": self.rejected,
                "refused_incomplete": self.refused_incomplete,
                "refused_derived": self.refused_derived,
                **self.writer.telemetry()}


def candidate_from_market(market: dict, book: dict, *, raw_book: dict = None,
                          cycle_id: str = "", catalyst_name: str = "",
                          catalyst_time_utc: str = None,
                          observed_at_utc: str = None) -> dict:
    """Shape a RAW observed market plus its RAW book into a feed candidate.

    THE ONE RULE: a field is either something the source recorded, or it is
    reported absent. There is no third branch. Earlier revisions had one --
    `question` fell back to the ticker, the resolution time fell back to the
    close time, an absent volume became `0.0`, an absent settlement source
    became the literal string `"kalshi"`, and an absent NO quote became the
    complement of the YES quote. Each of those turned "the exchange did not
    tell us" into a fact Alpha would later calibrate against.

    `book` is the EXECUTION-normalized book and is used ONLY to classify a
    quote the raw source lacks as `derived`. `raw_book` defaults to `market`,
    because the exchange publishes the quotes on the market object itself.

    Prices arrive in CENTS and leave in probability units. That is a unit
    change on an observed number, not a new fact, and it happens here because
    this is the code that knows the source unit.
    """
    market = market if isinstance(market, dict) else {}
    book = book if isinstance(book, dict) else {}
    raw = raw_book if isinstance(raw_book, dict) else market
    facts, provenance, unavailable, observation = {}, {}, [], {}
    #: AA-03 (re-audit). field -> every alias value the source supplied.
    #: A contradiction is PRESERVED here and refused by the contract; it is
    #: never written into `unavailable_fields`, because filing "the source
    #: answered twice, differently" as "the source said nothing" is how a
    #: self-contradicting feed came to look like a quiet market.
    contradictions = {}

    for field in MARKET_FIELDS:
        keys = SOURCE_BINDING[field][1]
        # RA-02: the comparator compares the COMPLETE canonical
        # representation -- the same text the record retains and the digest
        # covers. Handing it a flattened rendering is what made two different
        # authorities look like one.
        comparator = (settlement_source_comparator
                      if field == "resolution_source" else None)
        try:
            value, key = resolve_alias(market, field, keys,
                                       comparator=comparator)
        except AliasContradiction as exc:
            contradictions[field] = {str(k): _diagnostic(v)
                                     for k, v in exc.values.items()}
            facts[field] = None
            # Deliberately NOT appended to `unavailable_fields`.
            continue
        except ContractError:
            value, key = None, None
        except Exception as exc:                          # noqa: BLE001
            # V4-RA-03 -- THIS LOOP IS TOTAL, BECAUSE IT RUNS ON THE ENGINE.
            #
            # `candidate_from_market` is called from
            # `ExecutionEngine._shadow_observer`, i.e. on the decision
            # cycle's own thread. It used to let an exception through: a
            # source value whose `__repr__` raises made `resolve_alias`
            # raise while BUILDING its refusal message, the exception left
            # the producer entirely, and the observer's `except` then
            # performed a synchronous `log.debug` -- so a held logging
            # handler stalled the cycle. `safe_render` closed that at the
            # root; this closes it at the boundary, because "no known input
            # raises" and "this function does not raise" are different
            # claims and only the second one is a property of the engine.
            #
            # The field is recorded as CONTRADICTED rather than absent. That
            # is the stretch the existing contract allows and it is the
            # honest one available: the map is inside the digest, it forces
            # the record to be REFUSED by name, and filing "we could not
            # read what the source sent" as "the source sent nothing" is the
            # exact downgrade RA-01 and AA-03 both exist to prevent.
            contradictions[field] = {
                str(k): safe_render(v, limit=120)
                for k, v in _present_aliases(market, keys)} or {
                "unreadable": f"{type(exc).__name__}"}
            facts[field] = None
            continue
        if field == "resolution_source" and value is not None:
            value = _settlement_source_name(value)
            if value is None:
                key = None
        if value is None or key is None:
            facts[field] = None
            unavailable.append(field)
        else:
            facts[field] = value
            provenance[field] = provenance_path(field, key)

    # AA-01. Quotes come from the RAW observation only. The normalized book is
    # consulted solely to say WHY a quote is missing.
    for field in QUOTE_FIELDS:
        key = SOURCE_BINDING[field][1][0]
        cents = observed_cents(raw, key)
        if cents is not None:
            facts[field] = round(cents / 100.0, 6)
            provenance[field] = provenance_path(field, key)
            observation[field] = QUOTE_OBSERVED
            continue
        facts[field] = None
        unavailable.append(field)
        # Present in the execution book but absent from the raw source means
        # normalization computed it. Recorded as DERIVED so the refusal names
        # the real reason rather than reporting a bare absence.
        observation[field] = (QUOTE_DERIVED
                              if observed_cents(book, key) is not None
                              else QUOTE_OBSERVED)

    for field in ("volume", "open_interest"):
        if facts.get(field) is None:
            continue
        try:
            facts[field] = strict_number(facts[field], field=field, minimum=0.0)
        except ContractError:
            facts[field] = None
            provenance.pop(field, None)
            unavailable.append(field)
        except Exception:                                     # noqa: BLE001
            # V4-RA-03, same reason as `observed_cents`: a `volume` of
            # `10 ** 5000` made `strict_number` raise `ValueError` from
            # inside its own refusal message, on the engine's thread. The
            # verdict is unchanged -- the size is ABSENT -- and it is now
            # reached instead of escaping.
            facts[field] = None
            provenance.pop(field, None)
            unavailable.append(field)

    # A catalyst is evidence only when the caller genuinely observed one.
    if catalyst_name:
        facts["catalyst_name"] = catalyst_name
        provenance["catalyst_name"] = provenance_path("catalyst_name",
                                                      "catalyst")
    else:
        facts["catalyst_name"] = None
        unavailable.append("catalyst_name")
    if catalyst_time_utc:
        facts["catalyst_time_utc"] = catalyst_time_utc
        provenance["catalyst_time_utc"] = provenance_path("catalyst_time_utc",
                                                          "catalyst")
    else:
        facts["catalyst_time_utc"] = None
        unavailable.append("catalyst_time_utc")

    # AA-06: the observer stamps the instant it SAW this market. The clock
    # is read once, here, at observation; nothing downstream may substitute a
    # later one.
    facts["emitted_at_utc"] = observed_at_utc or iso_second(
        datetime.now(timezone.utc))
    provenance["emitted_at_utc"] = provenance_path("emitted_at_utc",
                                                   "emitted_at_utc")

    return {**facts, "source": "scanner", "cycle_id": cycle_id,
            "field_provenance": provenance,
            "quote_observation": observation,
            "contradictory_fields": contradictions,
            "unavailable_fields": sorted(set(unavailable))}
