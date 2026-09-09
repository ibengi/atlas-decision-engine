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

FEED_SCHEMA = "atlas-research-candidate-v1"
#: Subdirectory under DATA_DIR. Its own directory, so the producer's writes
#: can never collide with a state file and a test can assert exactly which
#: bytes the feed is allowed to touch.
SPOOL_DIRNAME = "research_spool"

#: The fields a candidate must carry for the consumer to be able to mint an
#: `atlas-alpha-v2` snapshot from it. Validated HERE so a malformed record
#: never reaches the spool: the consumer would only be able to drop it, and
#: a drop at ingest is much harder to trace back to the market that caused it.
REQUIRED_FIELDS = ("contract_id", "question", "yes_bid", "yes_ask",
                   "no_bid", "no_ask", "market_close_time_utc",
                   "expected_resolution_time_utc")


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
        if not isinstance(candidate, dict):
            self.rejected += 1
            return None
        missing = [f for f in REQUIRED_FIELDS if candidate.get(f) in (None, "")]
        if missing:
            self.rejected += 1
            log.debug(f"[RESEARCH_FEED] candidate missing {missing}")
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
        content = {
            "schema": FEED_SCHEMA,
            "emitted_at_utc": candidate.get("emitted_at_utc")
            or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "contract_id": str(candidate["contract_id"]),
            "event_id": str(candidate.get("event_id") or ""),
            "question": str(candidate["question"]),
            "resolution_rules": str(candidate.get("resolution_rules") or ""),
            "resolution_source": str(candidate.get("resolution_source") or ""),
            "market_close_time_utc": str(candidate["market_close_time_utc"]),
            "expected_resolution_time_utc":
                str(candidate["expected_resolution_time_utc"]),
            "volume": _finite_number(candidate.get("volume")) or 0.0,
            "open_interest": _finite_number(candidate.get("open_interest")) or 0.0,
            "catalyst_name": str(candidate.get("catalyst_name") or ""),
            "catalyst_time_utc": candidate.get("catalyst_time_utc") or None,
            # Provenance, for auditing which cycle produced the candidate.
            # Deliberately NOT the decision: the research path must not learn
            # what the engine decided, or the two stop being independent.
            "source": str(candidate.get("source") or "scanner"),
            "cycle_id": str(candidate.get("cycle_id") or ""),
            **prices,
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
                "pruned": self.pruned, "spooled": len(self._records_on_disk())}


def candidate_from_market(market: dict, book: dict, *, cycle_id: str = "",
                         catalyst_name: str = "",
                         catalyst_time_utc: str = None) -> dict:
    """Shape a scanner market plus its order book into a feed candidate.

    Prices arrive from the book in CENTS and leave in probability units,
    which is what `atlas-alpha-v2` speaks. Converting here rather than at
    ingest keeps the unit change next to the code that knows the source
    unit.
    """
    book = book or {}

    def _prob(value):
        v = _finite_number(value)
        return None if v is None else round(v / 100.0, 6)

    return {
        "contract_id": market.get("ticker"),
        "event_id": market.get("event_ticker") or market.get("event_id") or "",
        "question": market.get("title") or market.get("subtitle")
        or market.get("ticker") or "",
        "resolution_rules": market.get("rules_primary")
        or market.get("settlement_sources") or "",
        "resolution_source": market.get("settlement_source") or "kalshi",
        "yes_bid": _prob(book.get("yes_bid")),
        "yes_ask": _prob(book.get("yes_ask")),
        "no_bid": _prob(book.get("no_bid")),
        "no_ask": _prob(book.get("no_ask")),
        "volume": _finite_number(market.get("volume")) or 0.0,
        "open_interest": _finite_number(market.get("open_interest")) or 0.0,
        "market_close_time_utc": market.get("close_time")
        or market.get("expiration_time"),
        "expected_resolution_time_utc": market.get("expected_expiration_time")
        or market.get("expiration_time") or market.get("close_time"),
        "catalyst_name": catalyst_name,
        "catalyst_time_utc": catalyst_time_utc,
        "source": "scanner",
        "cycle_id": cycle_id,
    }
