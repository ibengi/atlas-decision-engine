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


def _settlement_source_name(value):
    """The settlement authority the exchange PUBLISHED, as text, or None.

    Kalshi records settlement sources as a list of objects. This reads the
    name it published; it never invents one, and it never falls back to the
    exchange's own name just because the market is listed there.

    AA-02 (re-audit): TYPE FIRST, NEVER COERCE FIRST.
        The previous version reached `str(name).strip()` after excluding only
        dicts, lists and booleans, so `{"name": 12345}` became the settlement
        source `"12345"`. An integer is a plausible internal identifier and a
        wholly implausible settlement authority; once it is a string nothing
        downstream can tell it apart from a name somebody published. The same
        held for `{"url": 8080}`.

        A member whose name is not TEXT is malformed, and one malformed member
        taints the whole collection: a settlement source list that is half
        readable is not half true.

    The `name or url` fallback is also gone. `or` fires on the empty string,
    so a member carrying `{"name": "", "url": ...}` silently reported the URL
    as the authority's name -- "published an empty name" and "published no
    name at all" are different facts and neither of them is the URL.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "url"):
            if key not in value:
                continue
            candidate = value[key]
            # The type check happens HERE, before any string conversion.
            if not isinstance(candidate, str):
                return None
            text = candidate.strip()
            return text or None
        return None
    if isinstance(value, (list, tuple)):
        names = []
        for item in value:
            name = _settlement_source_name(item)
            if name is None:
                return None       # one malformed entry taints the collection
            names.append(name)
        return ", ".join(names) or None
    return None


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

    # ── the one method the engine calls ─────────────────────────────────
    def emit_candidate(self, candidate: dict) -> bool:
        """Offer one candidate to the writer. True when it was ACCEPTED.

        NEVER raises and NEVER performs I/O. The caller is a decision cycle;
        a research feed that can propagate an exception -- or an fsync -- into
        it has become part of the money path by the back door.

        True means "queued", not "durable". The producer deliberately has no
        way to learn whether the bytes reached the disk, because there is no
        action the engine could take on that answer.
        """
        if not CFG.RESEARCH_FEED_ENABLED:
            return False
        try:
            record = self._build(candidate)
            if record is None:
                return False
            return self.writer.offer(record, approx_bytes=self._size(record))
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

        Deliberately an estimate: serializing twice to be exact would put JSON
        encoding of the full record back inside the decision cycle.
        """
        return 512 + 2 * sum(len(str(v)) for v in record.values())

    # ── construction ────────────────────────────────────────────────────
    def _build(self, candidate: dict):
        """One spool record, or None. Never substitutes an absent fact.

        The candidate arriving here already carries its own provenance and its
        per-quote observed/derived verdict (see `candidate_from_market`). This
        method re-derives nothing: it assembles the record, hashes it
        INCLUDING the provenance and the quote verdicts, and then validates
        the whole thing against the SHARED contract -- the same function the
        consumer and the readiness gate call, so a record that reaches the
        spool is one the consumer can mint from.
        """
        if not isinstance(candidate, dict):
            self.rejected += 1
            return None
        provenance = candidate.get("field_provenance")
        unavailable = candidate.get("unavailable_fields")
        observation = candidate.get("quote_observation")
        contradictions = candidate.get("contradictory_fields") or {}
        if not isinstance(provenance, dict) or not isinstance(unavailable, list) \
                or not isinstance(observation, dict) \
                or not isinstance(contradictions, dict):
            self.rejected += 1
            self._note(logging.DEBUG,
                       "[RESEARCH_FEED] candidate carries no provenance "
                       "container")
            return None

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
        comparator = (_settlement_source_name
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
