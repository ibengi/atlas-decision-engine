# -*- coding: utf-8 -*-
"""A genuinely contract-valid research candidate, built the production way.

Every fixture here goes through `candidate_from_market` and
`ResearchFeed._build`, i.e. the SAME code the engine runs. A hand-written dict
that merely looks like a record would drift from the contract the moment the
contract changed, and a test suite whose fixtures drift is a suite that stops
testing the thing it names.

`raw_market()` deliberately carries the four quotes, because that is where the
exchange publishes them and, since AA-01, the RAW observation is the only book
the research feed may read.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

from research_feed import ResearchFeed, candidate_from_market  # noqa: E402

#: The EXECUTION-normalized book. Present so a fixture can show the two apart.
EXECUTION_BOOK = {"yes_bid": 44, "yes_ask": 46, "no_bid": 54, "no_ask": 56,
                  "yes_mid": 45, "no_mid": 55, "spread": 2}


def raw_market(**over):
    """A raw exchange observation carrying every fact Kalshi really publishes."""
    now = datetime.now(timezone.utc)
    payload = {
        "ticker": "KXBTCD-26SEP1200-T60000",
        "event_ticker": "KXBTCD-26SEP1200",
        "title": "Will BTC be above 60000 at 12:00 ET?",
        "rules_primary": "Settles to the CF Benchmarks RTI at 12:00 ET.",
        "settlement_sources": [{"name": "CF Benchmarks RTI"}],
        "volume": 1200, "open_interest": 3400,
        "close_time": (now + timedelta(hours=3)).isoformat(timespec="seconds"),
        "expiration_time": (now + timedelta(hours=4)).isoformat(timespec="seconds"),
        "yes_bid": 44, "yes_ask": 46, "no_bid": 54, "no_ask": 56,
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v is not DROP}


class _Drop:
    def __repr__(self):
        return "<dropped>"


#: `raw_market(no_bid=DROP)` means "the exchange did not publish it", which is
#: a different fixture from "it published something unusable".
DROP = _Drop()


def valid_candidate(market=None, book=None, **kw):
    """The producer's candidate shape, from a raw observation."""
    payload = raw_market() if market is None else market
    return candidate_from_market(
        payload, EXECUTION_BOOK if book is None else book,
        raw_book=payload, cycle_id="fixture", **kw)


def valid_record(market=None, book=None, **kw):
    """A complete, checksum-correct `atlas-research-candidate-v3` record.

    Built by the real producer, so if the contract tightens and this stops
    being valid, every test using it fails loudly instead of quietly testing a
    record shape that no longer exists.
    """
    feed = ResearchFeed(start_writer=False)
    record = feed._build(valid_candidate(market, book, **kw))
    if record is None:                                  # pragma: no cover
        raise AssertionError(
            f"the fixture is not contract-valid: {feed.last_errors}")
    return record


def record_for_snapshot(snapshot):
    """Produce the exact observation named by an existing synthetic snapshot.

    Persistence fault tests must reach persistence. Pairing an unrelated
    generic record with that snapshot would now correctly fail the earlier
    semantic source gate and make those tests vacuous.
    """
    market = raw_market(
        ticker=snapshot.contract_id, event_ticker=snapshot.event_id,
        title=snapshot.question, rules_primary=snapshot.resolution_rules,
        settlement_sources=[{"name": snapshot.resolution_source}],
        volume=snapshot.volume, open_interest=snapshot.open_interest,
        close_time=snapshot.market_close_time_utc,
        expiration_time=snapshot.expected_resolution_time_utc,
        **{field: round(getattr(snapshot, field) * 100, 8)
           for field in ("yes_bid", "yes_ask", "no_bid", "no_ask")})
    candidate = valid_candidate(market,
                                observed_at_utc=snapshot.snapshot_time_utc)
    if snapshot.next_known_catalyst.time_utc:
        candidate["catalyst_name"] = snapshot.next_known_catalyst.name
        candidate["catalyst_time_utc"] = snapshot.next_known_catalyst.time_utc
    feed = ResearchFeed(start_writer=False)
    record = feed._build(candidate)
    if record is None:
        raise AssertionError(feed.last_errors)
    return record
