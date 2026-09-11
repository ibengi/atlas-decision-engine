"""Read-only research candidate feed: the producer side of the boundary.

This module is the ONLY thing the trading engine knows about the research
subsystem, and it deliberately knows nothing about it in return. It has no
providers, no models, no ensemble, no ledger and no gateway; it imports
`json`, `os`, `hashlib`, `logging`, `time` and `config`, and nothing else.
`tests/test_research_feed_boundary.py` pins that import list, so the
boundary cannot widen by accident.

WHY THE PRODUCER DOES NOT BUILD THE SNAPSHOT
    The Alpha subsystem mints the immutable `atlas-alpha-v2` snapshot at
    ingest, from the record written here. Doing it the other way round would
    require the engine to import `alpha_snapshot`, and the whole point of
    the isolation is that the money path has no dependency on the research
    path at all -- not a small one, not a pure one. What crosses the
    boundary is plain JSON on a filesystem, and the consumer cannot call
    back.

    Provenance is not lost by this: every record carries `record_sha256`
    over its own content, and the snapshot the consumer mints binds that
    hash, so a spool record edited between write and ingest is detectable.

WHY IT CANNOT HURT THE ENGINE
    A research feed that can break the money path is worse than no research
    feed. So:

      * it is OFF by default (`RESEARCH_FEED_ENABLED`, strict gate);
      * `emit_candidate` never raises -- every failure is logged and
        swallowed, because the caller is inside a decision cycle;
      * it never touches `PersistenceSentinel`: a failed research write is
        not a critical persistence failure and must never block an order
        the risk engine has approved, nor unblock one;
      * it writes ONLY under its own spool directory, never a state file;
      * the spool is BOUNDED. An unbounded research spool on a shared
        volume is a slow way to take the engine down with ENOSPC, so the
        producer prunes its own directory by count and by age on every
        write, and refuses to write when the cap is already met.
"""

import hashlib
import json
import logging
import os
import time

from config import CFG

log = logging.getLogger("RESEARCH_FEED")

FEED_SCHEMA = "atlas-research-candidate-v2"
#: v1 records are refused rather than migrated. A v1 record was allowed to
#: carry a substituted `resolution_source`, a `question` parsed from a ticker,
#: a resolution time copied from the close time, and a volume/open interest of
#: 0.0 that meant "absent". Those are exactly the invented facts this schema
#: exists to prevent, so a v1 record cannot be re-labelled as truthful -- it
#: has to be re-observed.
LEGACY_FEED_SCHEMAS = ("atlas-research-candidate-v1",)
#: Subdirectory under DATA_DIR. Its own directory, so the producer's writes
#: can never collide with a state file and a test can assert exactly which
#: bytes the feed is allowed to touch.
SPOOL_DIRNAME = "research_spool"

#: The fields a candidate must carry for the consumer to be able to mint an
#: `atlas-alpha-v2` snapshot from it. Validated HERE so a malformed record
#: never reaches the spool: the consumer would only be able to drop it, and
#: a drop at ingest is much harder to trace back to the market that caused it.
#: EVERY fact the consumer needs to mint a snapshot without inventing one.
#: It is deliberately the same set the readiness gate requires: a record the
#: consumer would have to refuse must never reach the spool in the first
#: place, because a refusal at ingest is much harder to trace back to the
#: market that caused it -- and, on a bounded spool, it displaces a record
#: that was complete.
REQUIRED_FIELDS = ("contract_id", "question", "resolution_rules",
                   "resolution_source", "yes_bid", "yes_ask",
                   "no_bid", "no_ask", "volume", "open_interest",
                   "market_close_time_utc", "expected_resolution_time_utc")

#: Facts that are genuinely optional for a snapshot. Absent means ABSENT: the
#: record says so in `unavailable_fields` instead of carrying a filler value.
OPTIONAL_FIELDS = ("event_id", "catalyst_name", "catalyst_time_utc")

#: field -> the source keys that are genuine aliases FOR THE SAME FACT.
#:
#: The rule for adding an entry here is narrow and worth stating: two keys may
#: share a row only when the exchange uses both names for one observation. A
#: key that describes a DIFFERENT fact -- a close time standing in for a
#: resolution time, a ticker standing in for the contract question, a spread
#: standing in for the far side of the book -- is a derivation, and a
#: derivation presented as an observation is the failure mode this whole
#: module exists to prevent.
MARKET_SOURCES = {
    "contract_id": ("ticker",),
    "event_id": ("event_ticker", "event_id"),
    "question": ("title",),
    "resolution_rules": ("rules_primary",),
    "resolution_source": ("settlement_sources", "settlement_source"),
    "volume": ("volume",),
    "open_interest": ("open_interest",),
    "market_close_time_utc": ("close_time",),
    # `expected_expiration_time` and `expiration_time` are two exchange names
    # for when the contract expires. `close_time` is NOT among them.
    "expected_resolution_time_utc": ("expected_expiration_time",
                                     "expiration_time"),
}

#: field -> the book keys, in CENTS. All four sides are required: one quoted
#: side plus a spread does not prove the contemporaneous book.
BOOK_SOURCES = {"yes_bid": ("yes_bid",), "yes_ask": ("yes_ask",),
                "no_bid": ("no_bid",), "no_ask": ("no_ask",)}


def spool_dir() -> str:
    return os.path.join(CFG.DATA_DIR, SPOOL_DIRNAME)


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _finite_number(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


class ResearchFeed:
    """Append-only, bounded, fail-soft spool of research candidates."""

    def __init__(self, directory: str = None):
        self.directory = directory or spool_dir()
        self.emitted = 0
        self.rejected = 0
        self.pruned = 0
        #: Refusals caused specifically by an incomplete SOURCE, as opposed to
        #: a malformed candidate. Surfaced in `stats()` so "Alpha is getting
        #: nothing" can be told apart from "the market is quiet".
        self.refused_incomplete = 0

    # ── the one method the engine calls ─────────────────────────────────
    def emit_candidate(self, candidate: dict) -> bool:
        """Write one candidate. Returns True when a record was written.

        NEVER raises. The caller is a decision cycle, and a research feed
        that can propagate an exception into it has become part of the money
        path by the back door.
        """
        if not CFG.RESEARCH_FEED_ENABLED:
            return False
        try:
            record = self._build(candidate)
            if record is None:
                return False
            return self._write(record)
        except Exception as e:                                # noqa: BLE001
            log.warning(f"[RESEARCH_FEED] candidate dropped: "
                        f"{type(e).__name__}: {e}")
            return False

    # ── construction ────────────────────────────────────────────────────
    def _build(self, candidate: dict):
        """One spool record, or None. Never substitutes an absent fact.

        The candidate arriving here already carries its own provenance (see
        `candidate_from_market`). This method re-derives nothing: it checks
        that every required fact is present AND that provenance names a real
        source key for it, then hashes the whole thing INCLUDING the
        provenance, so a record whose provenance was edited after the fact
        fails its own checksum.
        """
        if not isinstance(candidate, dict):
            self.rejected += 1
            return None
        provenance = candidate.get("field_provenance")
        unavailable = candidate.get("unavailable_fields")
        if not isinstance(provenance, dict) or not isinstance(unavailable, list):
            self.rejected += 1
            log.debug("[RESEARCH_FEED] candidate carries no field provenance")
            return None
        missing = [f for f in REQUIRED_FIELDS
                   if candidate.get(f) in (None, "") or f in unavailable
                   or not str(provenance.get(f) or "").strip()]
        if missing:
            self.rejected += 1
            self.refused_incomplete += 1
            # Named, at INFO, because this is the visible research signal that
            # the source is not yet carrying the facts Alpha needs. It is a
            # research failure and nothing else: no sentinel, no decision.
            log.info(f"[RESEARCH_FEED] {candidate.get('contract_id')} NOT "
                     f"emitted -- source did not record {sorted(missing)}; "
                     f"these are never reconstructed")
            return None
        prices = {}
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            value = _finite_number(candidate[field])
            if value is None or not 0.0 <= value <= 1.0:
                self.rejected += 1
                log.debug(f"[RESEARCH_FEED] {field}={candidate[field]!r} is "
                          f"not a probability price")
                return None
            prices[field] = value
        sizes = {}
        for field in ("volume", "open_interest"):
            value = _finite_number(candidate[field])
            if value is None or value < 0:
                self.rejected += 1
                log.debug(f"[RESEARCH_FEED] {field}={candidate[field]!r} is "
                          f"not an observed non-negative size")
                return None
            sizes[field] = value
        content = {
            "schema": FEED_SCHEMA,
            "emitted_at_utc": candidate.get("emitted_at_utc")
            or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "contract_id": str(candidate["contract_id"]),
            "event_id": str(candidate.get("event_id") or ""),
            "question": str(candidate["question"]),
            "resolution_rules": str(candidate["resolution_rules"]),
            "resolution_source": str(candidate["resolution_source"]),
            "market_close_time_utc": str(candidate["market_close_time_utc"]),
            "expected_resolution_time_utc":
                str(candidate["expected_resolution_time_utc"]),
            "catalyst_name": str(candidate.get("catalyst_name") or ""),
            "catalyst_time_utc": candidate.get("catalyst_time_utc") or None,
            # Provenance, for auditing which cycle produced the candidate.
            # Deliberately NOT the decision: the research path must not learn
            # what the engine decided, or the two stop being independent.
            "source": str(candidate.get("source") or "scanner"),
            "cycle_id": str(candidate.get("cycle_id") or ""),
            # Where each fact actually came from, and which optional facts the
            # source genuinely did not record. Both are hashed below.
            "field_provenance": {k: str(v) for k, v in sorted(provenance.items())},
            "unavailable_fields": sorted(str(f) for f in unavailable),
            **prices, **sizes,
        }
        content["record_sha256"] = hashlib.sha256(
            _canonical(content).encode("utf-8")).hexdigest()
        return content

    # ── bounded, atomic write ───────────────────────────────────────────
    def _write(self, record: dict) -> bool:
        os.makedirs(self.directory, exist_ok=True)
        existing = self._records_on_disk()
        self._prune(existing)
        if len(self._records_on_disk()) >= int(CFG.RESEARCH_FEED_MAX_SPOOL):
            log.warning(f"[RESEARCH_FEED] spool is full "
                        f"({CFG.RESEARCH_FEED_MAX_SPOOL} records) -- "
                        f"{record['contract_id']} NOT emitted. The consumer "
                        f"is not keeping up, or is not running.")
            self.rejected += 1
            return False
        name = f"{record['emitted_at_utc'].replace(':', '')}-" \
               f"{record['record_sha256'][:16]}.json"
        path = os.path.join(self.directory, name)
        if os.path.exists(path):
            return False                      # identical candidate this second
        tmp = path + ".tmp"
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False,
                             indent=1).encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self.emitted += 1
        return True

    def _records_on_disk(self) -> list:
        try:
            return sorted(n for n in os.listdir(self.directory)
                          if n.endswith(".json"))
        except OSError:
            return []

    def _prune(self, names) -> None:
        """Drop records the consumer will never usefully process.

        By age first -- a research candidate about a market that has since
        closed is worthless -- then by count, oldest first. Both bounds are
        configuration.
        """
        cutoff = time.time() - float(CFG.RESEARCH_FEED_MAX_AGE_S)
        keep = []
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    self.pruned += 1
                else:
                    keep.append(name)
            except OSError:
                continue
        overflow = len(keep) - int(CFG.RESEARCH_FEED_MAX_SPOOL)
        for name in keep[:max(0, overflow)]:
            try:
                os.remove(os.path.join(self.directory, name))
                self.pruned += 1
            except OSError:
                continue

    def stats(self) -> dict:
        return {"emitted": self.emitted, "rejected": self.rejected,
                "refused_incomplete": self.refused_incomplete,
                "pruned": self.pruned, "spooled": len(self._records_on_disk())}


def _first_observed(source: dict, keys):
    """(value, key) for the first key the source actually recorded, else
    (None, None). No key is consulted that is not an alias for this fact."""
    if not isinstance(source, dict):
        return None, None
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value, key
    return None, None


def _settlement_source_name(value):
    """Kalshi records settlement sources as a list of objects. Read the name
    the exchange published; never invent one, and never fall back to the
    exchange's own name just because the market is listed there."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("url")
        return str(name).strip() or None if name else None
    if isinstance(value, (list, tuple)):
        names = [n for n in (_settlement_source_name(v) for v in value) if n]
        return ", ".join(names) or None
    return None


def candidate_from_market(market: dict, book: dict, *, cycle_id: str = "",
                         catalyst_name: str = "",
                         catalyst_time_utc: str = None) -> dict:
    """Shape an observed market plus its observed book into a feed candidate.

    THE ONE RULE: a field is either something the source recorded, or it is
    reported absent. There is no third branch. Earlier revisions of this
    function had one -- `question` fell back to the ticker, the resolution
    time fell back to the close time, an absent volume became `0.0`, and an
    absent settlement source became the literal string `"kalshi"` -- and each
    of those turned "the exchange did not tell us" into a fact that Alpha
    would later calibrate against. A model scored on invented premises looks
    better than it is, which is the one error this subsystem cannot afford.

    Prices arrive from the book in CENTS and leave in probability units. That
    is a unit change on an observed number, not a new fact, and it happens
    here because this is the code that knows the source unit.
    """
    market = market if isinstance(market, dict) else {}
    book = book if isinstance(book, dict) else {}
    facts, provenance, unavailable = {}, {}, []

    for field, keys in MARKET_SOURCES.items():
        value, key = _first_observed(market, keys)
        if field == "resolution_source" and value is not None:
            value = _settlement_source_name(value)
            if value is None:
                key = None
        if value is None or key is None:
            facts[field] = None
            unavailable.append(field)
        else:
            facts[field] = value
            provenance[field] = f"market.{key}"

    for field, keys in BOOK_SOURCES.items():
        value, key = _first_observed(book, keys)
        cents = _finite_number(value)
        if cents is None:
            facts[field] = None
            unavailable.append(field)
        else:
            facts[field] = round(cents / 100.0, 6)
            provenance[field] = f"book.{key}(cents)"

    for field in ("volume", "open_interest"):
        if facts.get(field) is not None:
            number = _finite_number(facts[field])
            if number is None:
                facts[field] = None
                provenance.pop(field, None)
                unavailable.append(field)
            else:
                facts[field] = number

    # A catalyst is evidence only when the caller genuinely observed one.
    if catalyst_name or catalyst_time_utc:
        facts["catalyst_name"] = catalyst_name or ""
        facts["catalyst_time_utc"] = catalyst_time_utc
        provenance["catalyst_name"] = "observer.catalyst"
        if catalyst_time_utc:
            provenance["catalyst_time_utc"] = "observer.catalyst"
        else:
            unavailable.append("catalyst_time_utc")
    else:
        facts["catalyst_name"] = ""
        facts["catalyst_time_utc"] = None
        unavailable.extend(("catalyst_name", "catalyst_time_utc"))

    return {**facts, "source": "scanner", "cycle_id": cycle_id,
            "field_provenance": provenance,
            "unavailable_fields": sorted(set(unavailable))}
