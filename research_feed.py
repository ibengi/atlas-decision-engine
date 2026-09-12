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
                                resolve_alias, strict_number, validate_record)
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
    """
    text = value if isinstance(value, str) \
        else f"{type(value).__name__}:{value!r}"
    return text if len(text) <= 200 else text[:197] + "..."


#: The identity keys a settlement-source OBJECT may carry. Both are text and
#: both belong to the authority's identity: `name` says WHO settles the
#: market, `url` says WHERE that authority publishes the number. RA-02: a
#: normalization that keeps the first and drops the second is not a
#: canonical identity, it is a lossy rendering.
SOURCE_IDENTITY_KEYS = ("name", "url")

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
    """
    if isinstance(value, bool):
        raise MalformedSettlementSource("a boolean is not a settlement source")
    if isinstance(value, str):
        text = _identity_text(value, "name")
        return ((("name", text),),) if text else ()
    if isinstance(value, dict):
        fields = []
        for key in SOURCE_IDENTITY_KEYS:
            if key not in value:
                continue
            text = _identity_text(value[key], key)
            if text is not None:
                fields.append((key, text))
        if not fields:
            raise MalformedSettlementSource(
                f"no settlement-source identity among "
                f"{sorted(value)[:8]!r}; a container with no name and no url "
                f"is not an empty source, it is an unrecognised one")
        return (tuple(sorted(fields)),)
    if isinstance(value, (list, tuple)):
        members = []
        for item in value:
            # No early exit on success and none on failure either: a
            # malformed member raises, which taints the whole collection,
            # because a settlement source list that is half readable is not
            # half true.
            members.extend(settlement_source_identity(item))
        return tuple(members)
    raise MalformedSettlementSource(
        f"{type(value).__name__} is not a settlement source")


def _escape_identity(text: str) -> str:
    return "".join(_RENDER_ESCAPES.get(ch, ch) for ch in text)


def render_settlement_source(identity) -> str:
    """Readable AND injective text for a canonical structured identity.

    `name <url>`, members joined by ` | `, with the backslash, pipe and
    angle-bracket characters escaped inside every name and URL. The escaping is what makes the
    rendering injective: without it, one authority literally named `A | B`
    and two authorities `A` and `B` would produce the same record field, and
    RA-02 would be re-opened in the rendering after being closed in the
    comparison.
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


def settlement_source_comparator(value):
    """What `resolve_alias` compares two `resolution_source` aliases BY.

    The STRUCTURED identity, never the rendering: a comparator that flattens
    is a comparator that reports disagreement as agreement.

    Every malformed container compares equal (to `None`), because "we cannot
    read this" is one fact however it is misspelled -- and a field both
    aliases agree is unreadable is ABSENT, which the contract refuses anyway.
    """
    try:
        return settlement_source_identity(value) or None
    except MalformedSettlementSource:
        return None


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


def observed_cents(source: dict, key: str):
    """A quote the exchange actually published, in cents, or None.

    Strict on purpose: a string quote, a boolean, a NaN or an out-of-range
    number is treated as NOT OBSERVED rather than repaired. This is the one
    place where "the exchange published a number we can read" is decided, and
    a lenient parser here is how a malformed quote becomes a market fact.
    """
    if not isinstance(source, dict) or key not in source:
        return None
    try:
        return strict_number(source[key], field=key,
                             minimum=MIN_CENTS, maximum=MAX_CENTS)
    except ContractError:
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
        # RA-02: the comparator compares STRUCTURE. Handing it the text
        # rendering is what made two different authorities look like one.
        comparator = (settlement_source_comparator
                      if field == "resolution_source" else None)
        try:
            value, key = resolve_alias(market, field, keys,
                                       comparator=comparator)
        except AliasContradiction as exc:
            contradictions[field] = {k: _diagnostic(v)
                                     for k, v in exc.values.items()}
            facts[field] = None
            # Deliberately NOT appended to `unavailable_fields`.
            continue
        except ContractError:
            value, key = None, None
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
