# -*- coding: utf-8 -*-
"""The candidate evidence boundary, attacked rather than described.

Alpha Learning is a calibration system. Its whole output -- "this ensemble
is well calibrated at 0.6" -- is only worth the truthfulness of the market
facts it was scored against. A snapshot that carries an invented settlement
source, a volume of 0.0 that actually meant "unknown", or a resolution time
copied from the close time does not produce a wrong number; it produces a
plausible number about a market that never existed. That is the failure
this file exists to make impossible to reintroduce quietly.

THE ONE RULE UNDER TEST
    Every field of a candidate is either (1) directly observed and
    persisted, (2) explicitly marked unavailable, or (3) omitted because it
    is optional. There is no fourth branch, and in particular there is no
    "derive it, it is mathematically plausible" branch. Each test below
    removes exactly one observation from the source and asserts the pipeline
    reports it missing rather than reconstructing it.

WHY THE SAFETY WITNESS IS ON EVERY FAILURE PATH
    Failure branches are where emergency code gets added. So the isolation
    tests do not merely assert "no exception escaped": they assert, across
    the same failure, that the broker transport was called zero times, that
    no risk threshold moved, that no authority gate flipped, and that the
    engine's own state bytes are untouched. A research subsystem that stays
    silent while it is healthy is not interesting; one that stays harmless
    while it is failing is the entire claim.
"""
import ast
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                   # noqa: E402

import research_feed                                           # noqa: E402
from alpha_consumer import (FeedUnavailable, HttpSpoolSource,   # noqa: E402
                            LocalSpoolSource, ProcessedStore,
                            STATUS_ANALYZED, SpoolConsumer)
from alpha_ledger import AlphaLedger, LedgerError              # noqa: E402
from alpha_resolution_ingest import ingest_settlements         # noqa: E402
from alpha_snapshot import SnapshotError                       # noqa: E402
from config import CFG                                         # noqa: E402
# AA-02: the schema and the required-field list now live in the ONE shared
# contract, not in the producer. Importing them from their real home is part
# of the point: three components can no longer hold three opinions.
from candidate_contract import (FEED_SCHEMA, LEGACY_FEED_SCHEMAS,  # noqa: E402
                                REQUIRED_FIELDS)
from research_feed import (ResearchFeed,                       # noqa: E402
                           candidate_from_market, spool_dir)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Cents, as the exchange quotes them. All four sides, because a quoted side
#: plus a spread is not an observation of the far side.
BOOK = {"yes_bid": 44, "yes_ask": 46, "no_bid": 54, "no_ask": 56}


def market(**over):
    """A market payload with every fact the exchange really does publish."""
    now = datetime.now(timezone.utc)
    payload = {
        "ticker": "KXBTCD-TRUTH", "event_ticker": "EV-TRUTH",
        "title": "Will BTC be above 60000 at 12:00 ET?",
        "rules_primary": "Settles to the CF Benchmarks RTI at 12:00 ET.",
        "settlement_sources": [{"name": "CF Benchmarks RTI"}],
        "volume": 1200, "open_interest": 3400,
        "close_time": (now + timedelta(hours=3)).isoformat(),
        "expiration_time": (now + timedelta(hours=4)).isoformat(),
        # AA-01: quotes are published ON the market object, and that is the
        # RAW observation the research feed is allowed to read. The separate
        # BOOK below is the EXECUTION-normalized structure, in which a missing
        # side may already have been derived.
        **BOOK,
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v is not _DROP}


class _Drop:
    def __repr__(self):
        return "<dropped>"


#: `market(rules_primary=DROP)` means "the exchange did not publish it",
#: which is a different fixture from "it published an empty string".
_DROP = _Drop()
DROP = _DROP


def _read(name: str) -> str:
    with open(os.path.join(REPO, name), encoding="utf-8") as fh:
        return fh.read()


def recompute_record_hash(record: dict) -> str:
    """Re-derive a spool record's checksum the way the producer did.

    Deliberately reimplemented here rather than imported: a checksum test
    that calls the same helper the producer called proves only that the
    helper is deterministic.
    """
    content = {k: v for k, v in record.items() if k != "record_sha256"}
    import hashlib
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _BrokerTripwire:
    """Any call that could mutate broker state raises and is counted."""

    def __init__(self):
        self.mutations = 0

    def __call__(self, *a, **kw):
        self.mutations += 1
        raise AssertionError("the research path reached a broker mutation")


class TruthCase(AlphaCase):
    """Isolated DATA_DIR, feed enabled, and a witness on every safety fact
    that this subsystem is forbidden to move."""

    #: Read before and after each scenario. Every one of them is either an
    #: authority gate or a risk threshold.
    WITNESSED_CFG = ("ALLOW_ORDER_SUBMISSION", "LIVE_BROKER_WRITES_AUTHORIZED",
                     "DAILY_RESEARCH_ORACLE_APPROVED", "ALLOW_FALLBACK_CAPITAL",
                     "KILL_SWITCH", "ALPHA_GATEWAY_ENABLED",
                     "MAX_DAILY_LOSS", "MAX_DAILY_LOSS_PCT", "MAX_POS_PCT",
                     "KELLY_MAX_POS_PCT", "MAX_EQUITY_DRAWDOWN_PCT")

    #: Environment sentinels that must not appear because something in the
    #: research path felt it needed them.
    WITNESSED_ENV = ("ALLOW_ORDER_SUBMISSION", "LIVE_TRADING",
                     "LIVE_TRADING_CONFIRMED", "LIVE_BROKER_WRITES_AUTHORIZED",
                     "MODEL_APPROVED", "DAILY_RESEARCH_ORACLE_APPROVED")

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()
        self.feed = ResearchFeed()
        # AA-10 gave every feed its own writer thread. Stop it, or the
        # suite accumulates one polling daemon per test.
        self.addCleanup(self.feed.writer.stop)

    # ── acting ──────────────────────────────────────────────────────────
    def emit(self, market_payload=None, book=None, **kw):
        """Emit one candidate and WAIT for the writer to finish.

        AA-10 moved the spool write onto a separate thread, so `emit_candidate`
        returning no longer means the bytes are on disk -- that is the whole
        point of the change. Tests that then read the spool have to wait for
        the writer explicitly. The ENGINE never does this: `drain()` is
        documented as test-and-shutdown only, and a production caller that
        waited for the writer would reintroduce exactly the coupling AA-10 is
        about.
        """
        payload = (market_payload if market_payload is not None else market())
        accepted = self.feed.emit_candidate(candidate_from_market(
            payload, BOOK if book is None else book,
            raw_book=payload, cycle_id="c1", **kw))
        self.feed.writer.drain(timeout=5.0)
        return accepted

    def spool_bytes(self) -> dict:
        """Every spooled RECORD, by name.

        Only `.json` files. The producer also keeps its capacity-reservation
        lock inside this directory -- deliberately, so that "it writes only
        under its own spool directory" stays true -- and that lock is not
        evidence: it is never read by the consumer, never counted against the
        bound and never pruned. `all_spool_bytes()` covers it where a test
        needs the whole directory.
        """
        return {n: b for n, b in self.all_spool_bytes().items()
                if n.endswith(".json")}

    def all_spool_bytes(self) -> dict:
        """Every byte in the spool directory, records and lock alike."""
        directory = spool_dir()
        if not os.path.isdir(directory):
            return {}
        out = {}
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                out[name] = fh.read()
        return out

    def records(self) -> list:
        return [json.loads(b) for b in self.spool_bytes().values()]

    def only_record(self) -> dict:
        found = self.records()
        self.assertEqual(len(found), 1, f"expected one record, got {found}")
        return found[0]

    # ── witnessing ──────────────────────────────────────────────────────
    def witness(self) -> dict:
        return {"cfg": {k: getattr(CFG, k) for k in self.WITNESSED_CFG},
                "env": {k: os.environ.get(k) for k in self.WITNESSED_ENV},
                "data": self._data_bytes()}

    def _data_bytes(self) -> dict:
        """Every byte under DATA_DIR, so a scenario that writes outside its
        own spool is caught rather than assumed impossible."""
        out = {}
        for root, _dirs, names in os.walk(self._tmp):
            for name in names:
                path = os.path.join(root, name)
                try:
                    with open(path, "rb") as fh:
                        out[os.path.relpath(path, self._tmp)] = fh.read()
                except OSError:
                    continue
        return out

    def assertNothingUnsafeMoved(self, before: dict, *, data=True):
        after = self.witness()
        self.assertEqual(after["cfg"], before["cfg"],
                         "a risk threshold or authority gate moved")
        self.assertEqual(after["env"], before["env"],
                         "an authority sentinel appeared in the environment")
        if data:
            self.assertEqual(after["data"], before["data"],
                             "engine state bytes changed")

    def under_tripwire(self, fn, *a, **kw):
        """Run `fn` with every broker mutation wired to explode, and return
        (result, mutation_count)."""
        import kalshi_client
        import order_manager
        wire = _BrokerTripwire()
        targets = [(kalshi_client.KalshiClient, "create_order"),
                   (kalshi_client.KalshiClient, "cancel_order"),
                   (order_manager.OrderManager, "place_and_track")]
        started = [patch.object(owner, name, wire) for owner, name in targets]
        for p in started:
            p.start()
        try:
            return fn(*a, **kw), wire.mutations
        finally:
            for p in started:
                p.stop()


# ════════════════════════════════════════════════════════════════════════
# 1. CANDIDATE TRUTH
# ════════════════════════════════════════════════════════════════════════
class AFullyObservedCandidateIsAccepted(TruthCase):

    def test_a_complete_market_becomes_one_attributed_record(self):
        self.assertTrue(self.emit())
        record = self.only_record()
        self.assertEqual(record["schema"], FEED_SCHEMA)
        for field in REQUIRED_FIELDS:
            self.assertNotIn(record[field], (None, ""), field)
            self.assertTrue(record["field_provenance"].get(field),
                            f"{field} reached the spool unattributed")

    def test_provenance_names_the_key_the_fact_was_read_from(self):
        """Not a label -- the actual source key, so an auditor can go back
        to the payload and find the same value."""
        self.emit()
        provenance = self.only_record()["field_provenance"]
        self.assertEqual(provenance["question"], "market.title")
        self.assertEqual(provenance["resolution_rules"], "market.rules_primary")
        self.assertEqual(provenance["resolution_source"],
                         "market.settlement_sources")
        self.assertEqual(provenance["expected_resolution_time_utc"],
                         "market.expiration_time")
        # AA-01: the namespace is `raw_book`, not `book`. The distinction is
        # the finding: `book` was the EXECUTION-normalized structure, in which
        # this quote may have been computed rather than published.
        self.assertEqual(provenance["yes_ask"], "raw_book.yes_ask(cents)")

    def test_observed_facts_survive_serialization_unchanged(self):
        source = market()
        self.emit(source)
        record = self.only_record()
        self.assertEqual(record["question"], source["title"])
        self.assertEqual(record["resolution_rules"], source["rules_primary"])
        self.assertEqual(record["volume"], float(source["volume"]))
        self.assertEqual(record["open_interest"],
                         float(source["open_interest"]))
        self.assertEqual(record["market_close_time_utc"], source["close_time"])
        self.assertEqual(record["expected_resolution_time_utc"],
                         source["expiration_time"])

    def test_the_only_transformation_is_a_unit_change_on_an_observed_price(self):
        self.emit()
        record = self.only_record()
        for field, cents in BOOK.items():
            self.assertAlmostEqual(record[field], cents / 100.0, places=6)


class AnAbsentFactIsNeverReconstructed(TruthCase):
    """One test per fact the earlier revision used to invent."""

    def assertRefused(self, source, *, field, book=None):
        before = self.witness()
        self.assertFalse(self.emit(source, book))
        self.assertEqual(self.spool_bytes(), {},
                         f"{field} was absent and a record was written anyway")
        self.assertEqual(self.feed.stats()["refused_incomplete"], 1)
        self.assertNothingUnsafeMoved(before)

    def test_an_unpublished_resolution_source_is_not_filled_with_the_venue(self):
        """`resolution_source` used to default to the literal "kalshi". The
        exchange being Kalshi is not evidence of who settles the contract."""
        self.assertRefused(market(settlement_sources=DROP),
                           field="resolution_source")

    def test_an_unpublished_question_is_not_parsed_from_the_ticker(self):
        self.assertRefused(market(title=DROP), field="question")

    def test_unpublished_rules_are_not_replaced_by_an_empty_string(self):
        self.assertRefused(market(rules_primary=DROP),
                           field="resolution_rules")

    def test_an_absent_expiration_is_not_taken_from_the_close_time(self):
        """The one derivation the brief names explicitly: a close time is a
        different instant from a resolution time, and a market that closes
        at noon can settle hours later."""
        source = market(expiration_time=DROP, expected_expiration_time=DROP)
        self.assertIn("close_time", source)
        self.assertRefused(source, field="expected_resolution_time_utc")

    def test_an_absent_volume_does_not_become_zero(self):
        """`0.0` and "the exchange did not report it" are different facts,
        and a model calibrated on the first while seeing the second is
        calibrated on nothing."""
        self.assertRefused(market(volume=DROP), field="volume")

    def test_an_absent_open_interest_does_not_become_zero(self):
        self.assertRefused(market(open_interest=DROP), field="open_interest")

    def test_a_missing_book_side_is_not_inferred_from_the_spread(self):
        """AA-01: the side must be dropped from the RAW observation.

        Dropping it from the execution book alone proves nothing now -- that
        book is no longer the source the research feed reads. The raw market is
        where the exchange publishes its quotes, so that is where the gap has
        to be made for this to be a real test.
        """
        for side in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            with self.subTest(side=side):
                self.setUp()
                raw = market(**{side: DROP})
                book = {k: v for k, v in BOOK.items() if k != side}
                book["spread"] = 2           # present, and deliberately unused
                self.assertRefused(raw, field=side, book=book)

    def test_the_refusal_names_the_field_and_stays_a_research_event(self):
        with self.assertLogs("RESEARCH_FEED", level="INFO") as logs:
            self.emit(market(volume=DROP))
        text = "\n".join(logs.output)
        self.assertIn("volume", text)
        self.assertIn("never reconstructed", text)

    def test_an_optional_fact_is_reported_absent_rather_than_filled(self):
        """`event_id` and the catalyst are genuinely optional: absent means
        the record SAYS absent, which is branch (2), not branch (4)."""
        self.assertTrue(self.emit(market(event_ticker=DROP, event_id=DROP)))
        record = self.only_record()
        # AA-02: absent is now None, never the empty string. `""` was itself
        # a filler -- it reads as "the exchange published an empty event id"
        # rather than "the exchange published none".
        self.assertIsNone(record["event_id"])
        self.assertIn("event_id", record["unavailable_fields"])
        self.assertIn("catalyst_time_utc", record["unavailable_fields"])
        self.assertNotIn("event_id", record["field_provenance"])

    def test_an_observed_catalyst_is_attributed_like_any_other_fact(self):
        when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertTrue(self.emit(catalyst_name="CPI", catalyst_time_utc=when))
        record = self.only_record()
        self.assertEqual(record["catalyst_name"], "CPI")
        self.assertEqual(record["field_provenance"]["catalyst_time_utc"],
                         "observer.catalyst")


class MalformedObservationsFailClosed(TruthCase):

    def candidate(self, **over):
        """A structurally complete candidate whose ONE field is poisoned,
        built directly so the poison reaches `_build` rather than being
        filtered out while shaping."""
        base = candidate_from_market(market(), BOOK, cycle_id="c1")
        base.update(over)
        return base

    def test_a_price_above_one_is_refused(self):
        self.assertFalse(self.feed.emit_candidate(self.candidate(yes_ask=1.5)))
        self.assertEqual(self.spool_bytes(), {})

    def test_a_negative_price_is_refused(self):
        self.assertFalse(self.feed.emit_candidate(self.candidate(no_bid=-0.1)))
        self.assertEqual(self.spool_bytes(), {})

    def test_nan_and_infinity_are_refused_on_every_numeric_field(self):
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask", "volume",
                      "open_interest"):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=bad):
                    self.assertFalse(self.feed.emit_candidate(
                        self.candidate(**{field: bad})))
        self.assertEqual(self.spool_bytes(), {})

    def test_a_nan_price_in_the_book_never_reaches_the_candidate(self):
        """Fails closed one stage earlier too: the shaping step reports the
        side unavailable rather than passing a NaN along."""
        # AA-01: quotes are read from the RAW observation, so that is where a
        # malformed one has to be injected for this to test anything.
        raw = market(yes_ask=float("nan"))
        candidate = candidate_from_market(raw, BOOK, raw_book=raw)
        self.assertIsNone(candidate["yes_ask"])
        self.assertIn("yes_ask", candidate["unavailable_fields"])
        self.assertNotIn("yes_ask", candidate["field_provenance"])

    def test_a_negative_size_is_refused(self):
        self.assertFalse(self.feed.emit_candidate(self.candidate(volume=-1)))

    def test_a_candidate_with_no_provenance_at_all_is_refused(self):
        candidate = self.candidate()
        candidate.pop("field_provenance")
        self.assertFalse(self.feed.emit_candidate(candidate))
        self.assertEqual(self.spool_bytes(), {})

    def test_a_candidate_whose_provenance_omits_one_field_is_refused(self):
        candidate = self.candidate()
        candidate["field_provenance"].pop("resolution_source")
        self.assertFalse(self.feed.emit_candidate(candidate))
        self.assertEqual(self.feed.stats()["refused_incomplete"], 1)

    def test_a_field_both_present_and_listed_unavailable_is_refused(self):
        """A record that contradicts itself is not resolved in favour of the
        value: the contradiction IS the incompleteness."""
        candidate = self.candidate()
        candidate["unavailable_fields"] = ["volume"]
        self.assertFalse(self.feed.emit_candidate(candidate))

    def test_a_non_dict_candidate_is_refused_without_raising(self):
        for junk in (None, [], "KXBTC", 7):
            self.assertFalse(self.feed.emit_candidate(junk))


class TheConsumerRefusesWhatItCannotAttribute(TruthCase):

    def consumer(self):
        return SpoolConsumer(source=LocalSpoolSource(spool_dir()),
                             store=ProcessedStore())

    def write_raw(self, record: dict, name="raw.json"):
        os.makedirs(spool_dir(), exist_ok=True)
        with open(os.path.join(spool_dir(), name), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def good_record(self) -> dict:
        self.emit()
        return self.only_record()

    def test_a_complete_record_mints_a_snapshot(self):
        self.good_record()
        pending = self.consumer().pending()
        self.assertEqual(len(pending), 1)
        snapshot, _record = pending[0]
        snapshot.verify()

    def test_a_legacy_v1_record_is_refused_not_migrated(self):
        """A v1 record was ALLOWED to carry substituted facts. Re-labelling
        it would launder exactly the values this schema exists to stop."""
        record = dict(self.good_record(), schema=LEGACY_FEED_SCHEMAS[0])
        self.write_raw(record, "legacy.json")
        consumer = self.consumer()
        consumer.pending()
        self.assertEqual(consumer.stats["legacy_schema"], 1)
        self.assertEqual(consumer.stats["minted"], 1)   # only the v2 one

    def test_a_record_missing_provenance_is_refused_at_ingest(self):
        record = self.good_record()
        record.pop("field_provenance")
        self.write_raw(record, "noprov.json")
        os.remove(os.path.join(spool_dir(), sorted(
            n for n in self.spool_bytes() if n != "noprov.json")[0]))
        consumer = self.consumer()
        self.assertEqual(consumer.pending(), [])
        # A record whose provenance container is gone also fails its checksum,
        # because AA-05 requires provenance to be INSIDE the digest. Either
        # counter proves the refusal; both must be non-zero in total.
        self.assertEqual(consumer.stats["unattributed"]
                         + consumer.stats["checksum_failures"], 1)

    def test_the_consumer_supplies_no_default_for_any_market_fact(self):
        """The old `.get(x, "kalshi")` / `.get(x, 0.0)` pair, pinned shut."""
        source = ast.parse(_read("alpha_consumer.py"))
        mint = next(n for n in ast.walk(source)
                    if isinstance(n, ast.FunctionDef) and n.name == "mint")
        defaulted = []
        for node in ast.walk(mint):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and len(node.args) == 2:
                key = getattr(node.args[0], "value", "?")
                if key in REQUIRED_FIELDS:
                    defaulted.append(key)
        self.assertEqual(defaulted, [],
                         f"mint() substitutes a default for {defaulted}")


# ════════════════════════════════════════════════════════════════════════
# 2. IMMUTABILITY
# ════════════════════════════════════════════════════════════════════════
class EvidenceIdentityIsDerivedFromContent(TruthCase):

    def snapshot_for(self, record):
        return SpoolConsumer(source=LocalSpoolSource(spool_dir()),
                             store=ProcessedStore()).mint(record)

    def test_the_same_observation_mints_the_same_identity(self):
        self.emit()
        record = self.only_record()
        self.assertEqual(self.snapshot_for(record).market_snapshot_id,
                         self.snapshot_for(dict(record)).market_snapshot_id)

    def test_a_moved_price_is_a_different_snapshot(self):
        self.emit()
        record = self.only_record()
        moved = dict(record, yes_ask=record["yes_ask"] + 0.01)
        self.assertNotEqual(self.snapshot_for(record).market_snapshot_id,
                            self.snapshot_for(moved).market_snapshot_id)

    def test_a_moved_time_is_a_different_snapshot(self):
        self.emit()
        record = self.only_record()
        later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(
            timespec="seconds")
        moved = dict(record, emitted_at_utc=later)
        self.assertNotEqual(self.snapshot_for(record).market_snapshot_id,
                            self.snapshot_for(moved).market_snapshot_id)

    def test_a_snapshot_refuses_mutation(self):
        self.emit()
        snapshot = self.snapshot_for(self.only_record())
        with self.assertRaises(Exception):
            snapshot.yes_ask = 0.99
        copy = snapshot.as_dict()
        copy["question"] = "a different question"
        self.assertNotEqual(snapshot.question, copy["question"])

    def test_a_tampered_snapshot_fails_its_own_verification(self):
        import dataclasses
        self.emit()
        snapshot = self.snapshot_for(self.only_record())
        forged = dataclasses.replace(snapshot, resolution_source="kalshi")
        with self.assertRaises(SnapshotError):
            forged.verify()

    def test_provenance_is_inside_the_record_checksum(self):
        """Editing where a fact came from changes the record's identity, so
        a re-attributed record cannot masquerade as the original."""
        self.emit()
        record = self.only_record()
        self.assertEqual(recompute_record_hash(record),
                         record["record_sha256"])
        forged = json.loads(json.dumps(record))
        forged["field_provenance"]["resolution_source"] = "market.ticker"
        self.assertNotEqual(recompute_record_hash(forged),
                            forged["record_sha256"])

    def test_an_edited_unavailable_list_also_breaks_the_checksum(self):
        self.emit(market(event_ticker=DROP, event_id=DROP))
        record = self.only_record()
        forged = json.loads(json.dumps(record))
        forged["unavailable_fields"] = []
        self.assertNotEqual(recompute_record_hash(forged),
                            forged["record_sha256"])

    def test_a_full_consume_changes_not_one_byte_of_the_spool(self):
        """The authority rule, measured: the producer owns those bytes."""
        self.emit()
        before = self.spool_bytes()
        consumer = SpoolConsumer(source=LocalSpoolSource(spool_dir()),
                                 store=ProcessedStore())
        pending = consumer.pending()
        self.assertEqual(len(pending), 1)
        consumer.store.mark(pending[0][0].market_snapshot_id, STATUS_ANALYZED)
        self.assertEqual(self.spool_bytes(), before)


# ════════════════════════════════════════════════════════════════════════
# 3. PERSISTENCE
# ════════════════════════════════════════════════════════════════════════
class ThePredictionLedgerIsAppendOnly(TruthCase):

    def ledger(self):
        return AlphaLedger(path=os.path.join(self._tmp, "alpha_ledger.jsonl"),
                           cost_path=os.path.join(self._tmp, "alpha_cost.jsonl"))

    def prediction(self, pid="p-1", **over):
        """One prediction row.

        AA-13 gives each snapshot a STABLE analysis identity and allows it at
        most one committed prediction, so the snapshot id is derived from the
        prediction id here. Two predictions sharing one snapshot id is now the
        thing the ledger refuses, and a fixture that did it by accident would
        make every case below fail for that reason instead of its own.
        """
        row = {"prediction_id": pid, "contract_id": "KXBTCD-TRUTH",
               "market_snapshot_id": f"snap-{pid}", "p_yes": 0.61,
               "state": "SHADOW_POSITIVE_EDGE", "executed": False}
        row.update(over)
        return row

    def test_a_prediction_is_written_once(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        with self.assertRaises(LedgerError):
            ledger.record_prediction(self.prediction())

    def test_resolution_appends_and_leaves_the_prediction_row_byte_identical(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        with open(ledger.log.path, "rb") as fh:
            first_line = fh.read()
        ledger.resolve("p-1", 1, source="settlement feed")
        with open(ledger.log.path, "rb") as fh:
            lines = fh.read()
        self.assertTrue(lines.startswith(first_line),
                        "the prediction row was rewritten by the resolution")
        # PREDICTION, its COMMIT receipt (AA-13 re-audit), then RESOLUTION.
        # The count moved because a receipt is now written; the property
        # under test -- the prediction row is not touched -- did not.
        self.assertEqual([json.loads(x)["kind"] for x in lines.splitlines()],
                         ["PREDICTION", "COMMIT", "RESOLUTION"])

    def test_an_outcome_is_written_once(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        ledger.resolve("p-1", 1, source="feed")
        with self.assertRaises(LedgerError):
            ledger.resolve("p-1", 0, source="feed")

    def test_resolving_an_unknown_prediction_writes_nothing(self):
        ledger = self.ledger()
        with self.assertRaises(LedgerError):
            ledger.resolve("never-predicted", 1, source="feed")
        self.assertEqual(ledger.rows(), [])

    def test_a_torn_last_row_preserves_every_earlier_row(self):
        """A crash mid-append must cost the interrupted row and nothing
        else: history before it is still history."""
        ledger = self.ledger()
        ledger.record_prediction(self.prediction("p-1"))
        ledger.record_prediction(self.prediction("p-2"))
        with open(ledger.log.path, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "prediction", "predi')
        torn_bytes = open(ledger.log.path, "rb").read()
        rows = [r for r in ledger.rows() if r["kind"] == "PREDICTION"]
        self.assertEqual([r.get("prediction_id") for r in rows],
                         ["p-1", "p-2"])
        # AA-12: a later append must not splice itself onto the damaged
        # fragment, and must not truncate it away either. The old bytes stay
        # byte-for-byte where they were; the new row simply starts on its own
        # line, and the fragment remains visible as an unparsable row.
        ledger.record_prediction(self.prediction("p-3"))
        after = open(ledger.log.path, "rb").read()
        self.assertTrue(after.startswith(torn_bytes),
                        "the torn tail was rewritten or truncated")
        self.assertEqual([r.get("prediction_id") for r in ledger.rows()
                          if r["kind"] == "PREDICTION"],
                         ["p-1", "p-2", "p-3"])

    def test_a_corrupt_middle_row_is_not_read_as_end_of_file(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction("p-1"))
        with open(ledger.log.path, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        ledger.record_prediction(self.prediction("p-2"))
        self.assertEqual([r.get("prediction_id") for r in ledger.rows()
                          if r["kind"] == "PREDICTION"],
                         ["p-1", "p-2"])

    def test_the_ledger_opens_its_file_only_to_append_or_to_read(self):
        """Static, because this is a property of the code rather than of one
        run: no truncation, no seek, no rewrite anywhere in the module."""
        tree = ast.parse(_read("alpha_ledger.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "open" and getattr(node.func, "attr", "") == "open" \
                    and getattr(getattr(node.func, "value", None), "id", "") == "os":
                flags = ast.dump(node.args[1]) if len(node.args) > 1 else ""
                self.assertIn("O_APPEND", flags)
                self.assertNotIn("O_TRUNC", flags)
            elif name == "open":
                mode = node.args[1].value if len(node.args) > 1 else \
                    next((k.value.value for k in node.keywords
                          if k.arg == "mode"), "r")
                self.assertTrue(str(mode).startswith("r"),
                                f"alpha_ledger opens a file with mode {mode!r}")
            self.assertNotIn(name, ("truncate", "seek"),
                             "alpha_ledger can move or shorten its own log")


class ResolutionIngestionIsIdempotentAndNonDestructive(TruthCase):

    #: Since the AA-15 re-audit a settlement must carry the complete
    #: required binding and name a source an operator has qualified. The
    #: fixture carries both because a real one does.
    BINDING = {"contract_id": "KXBTCD-TRUTH",
               "market_snapshot_id": "snap-truth",
               "record_sha256": "e" * 64}
    TRUSTED = ["kalshi feed"]

    def ledger_with_prediction(self, pid="p-1"):
        ledger = AlphaLedger(
            path=os.path.join(self._tmp, "alpha_ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "alpha_cost.jsonl"))
        ledger.record_prediction({"prediction_id": pid,
                                  "contract_id": "KXBTCD-TRUTH",
                                  "market_snapshot_id": "snap-truth",
                                  "source_binding": dict(self.BINDING),
                                  "p_yes": 0.61, "executed": False})
        return ledger

    def settlement(self, **over):
        row = {"prediction_id": "p-1", "outcome": 1, "source": "kalshi feed",
               "contract_id": self.BINDING["contract_id"],
               "market_snapshot_id": self.BINDING["market_snapshot_id"],
               "source_record_sha256": self.BINDING["record_sha256"]}
        row.update(over)
        return row

    def ingest(self, ledger, rows, **kw):
        kw.setdefault("trusted_sources", self.TRUSTED)
        return ingest_settlements(ledger, rows, **kw)

    def test_the_same_settlement_twice_appends_once(self):
        ledger = self.ledger_with_prediction()
        first = self.ingest(ledger, [self.settlement()])
        self.assertEqual(first["appended"], 1)
        rows_after_first = len(ledger.rows())
        second = self.ingest(ledger, [self.settlement()])
        self.assertEqual(second["appended"], 0)
        self.assertEqual(second["idempotent"], 1)
        self.assertEqual(len(ledger.rows()), rows_after_first)

    def test_a_contradictory_settlement_is_reported_and_not_written(self):
        ledger = self.ledger_with_prediction()
        self.ingest(ledger, [self.settlement(outcome=1)])
        before = len(ledger.rows())
        result = self.ingest(ledger, [self.settlement(outcome=0)])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(len(ledger.rows()), before)
        self.assertEqual(ledger.find_resolution("p-1")["actual_outcome"], 1)

    def test_an_unknown_prediction_is_rejected_rather_than_invented(self):
        ledger = self.ledger_with_prediction()
        result = self.ingest(ledger, [self.settlement(
            prediction_id="p-does-not-exist")])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(result["rejected"][0]["reason"],
                         "unknown prediction_id")

    def test_a_settlement_without_a_source_is_rejected(self):
        """An outcome with no stated source is not evidence, however true."""
        ledger = self.ledger_with_prediction()
        result = self.ingest(ledger, [self.settlement(source="")])
        self.assertEqual(result["appended"], 0)
        # The message now comes from the shared contract's `strict_text`,
        # which refuses a whitespace-only source as well as an empty one.
        self.assertIn("source", result["rejected"][0]["reason"])
        self.assertIn("blank", result["rejected"][0]["reason"])

    def test_ingestion_declares_itself_shadow_only_with_no_broker_authority(self):
        ledger = self.ledger_with_prediction()
        result, mutations = self.under_tripwire(
            ingest_settlements, ledger, [self.settlement()])
        self.assertEqual(result["mode"], "SHADOW_ONLY")
        self.assertIs(result["broker_authority"], False)
        self.assertEqual(mutations, 0)


class ARestartDoesNotRepeatFinishedWork(TruthCase):

    def consumer(self, store=None):
        return SpoolConsumer(source=LocalSpoolSource(spool_dir()),
                             store=store or ProcessedStore())

    def test_a_terminal_snapshot_is_not_reprocessed_after_restart(self):
        self.emit()
        first = self.consumer()
        pending = first.pending()
        self.assertEqual(len(pending), 1)
        first.store.mark(pending[0][0].market_snapshot_id, STATUS_ANALYZED)
        # A fresh process: new consumer, new store object, same file.
        self.assertEqual(self.consumer().pending(), [])

    def test_the_same_market_twice_in_one_batch_is_minted_once(self):
        record = None
        self.emit()
        record = self.only_record()
        with open(os.path.join(spool_dir(), "copy.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh)
        consumer = self.consumer()
        self.assertEqual(len(consumer.pending()), 1)
        self.assertEqual(consumer.stats["duplicates"], 1)

    def test_a_torn_state_tail_still_remembers_earlier_work(self):
        store = ProcessedStore(path=os.path.join(self._tmp, "processed.jsonl"))
        store.mark("snap-one", STATUS_ANALYZED)
        store.mark("snap-two", STATUS_ANALYZED)
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write('{"market_snapshot_')
        reopened = ProcessedStore(path=store.path)
        self.assertTrue(reopened.seen("snap-one"))
        self.assertTrue(reopened.seen("snap-two"))


# ════════════════════════════════════════════════════════════════════════
# 4. FAILURE ISOLATION
# ════════════════════════════════════════════════════════════════════════
class EveryFailurePathLeavesTheEngineUntouched(TruthCase):
    """Each scenario asserts the same four things: nothing raised into the
    caller, zero broker mutations, no threshold or gate moved, and no bytes
    written outside the research spool."""

    def test_an_unwritable_evidence_directory_is_survived(self):
        """AA-10 changed what a `True` return MEANS, and this case says so.

        The engine now hands the record to a bounded queue and returns; the
        write happens on the writer thread. So `emit_candidate` reporting
        `True` means QUEUED, not DURABLE -- the engine deliberately cannot
        learn whether the disk accepted the bytes, because there is no action
        it could take on that answer without coupling the two paths again.

        What must still hold, and is asserted here: nothing raised into the
        caller, nothing was spooled, and no gate or threshold moved.
        """
        blocked = os.path.join(self._tmp, "not-a-directory")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("this path is a file")
        feed = ResearchFeed(directory=blocked)
        self.addCleanup(feed.writer.stop)
        before = self.witness()
        payload = market()
        _result, mutations = self.under_tripwire(
            feed.emit_candidate,
            candidate_from_market(payload, BOOK, raw_book=payload))
        feed.writer.drain(timeout=5.0)
        self.assertEqual(mutations, 0)
        self.assertEqual(feed.writer.spool.stats["written"], 0,
                         "a write succeeded into an unusable directory")
        self.assertNothingUnsafeMoved(before)

    def test_a_full_disk_is_survived(self):
        before = self.witness()
        payload = market()

        def _emit():
            queued = self.feed.emit_candidate(
                candidate_from_market(payload, BOOK, raw_book=payload))
            with patch("os.open",
                       side_effect=OSError(28, "No space left on device")):
                self.feed.writer.drain(timeout=5.0)
            return queued

        _result, mutations = self.under_tripwire(_emit)
        self.assertEqual(mutations, 0)
        self.assertEqual(self.spool_bytes(), {},
                         "a record reached the spool despite ENOSPC")
        self.assertNothingUnsafeMoved(before)

    def test_a_malformed_source_payload_is_survived(self):
        before = self.witness()
        junk = [None, [], "", {"ticker": None}, {"volume": object()}]
        for payload in junk:
            with self.subTest(payload=repr(payload)[:40]):
                result, mutations = self.under_tripwire(
                    self.feed.emit_candidate,
                    candidate_from_market(payload, BOOK, raw_book=payload))
                self.assertFalse(result)
                self.assertEqual(mutations, 0)
        self.assertNothingUnsafeMoved(before)

    def test_an_unreadable_spool_record_does_not_stop_the_batch(self):
        self.emit()
        os.makedirs(spool_dir(), exist_ok=True)
        with open(os.path.join(spool_dir(), "aaa-broken.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        consumer = SpoolConsumer(source=LocalSpoolSource(spool_dir()),
                                 store=ProcessedStore())
        pending, mutations = self.under_tripwire(consumer.pending)
        self.assertEqual(len(pending), 1)
        self.assertEqual(consumer.stats["malformed"], 1)
        self.assertEqual(mutations, 0)

    def test_an_unreachable_feed_is_reported_not_reported_as_empty(self):
        """An outage and a quiet market must never look the same: the first
        would otherwise be indistinguishable from "no candidates today"."""
        class _Boom:
            def get(self, *a, **kw):
                raise ConnectionError("engine unreachable")

        source = HttpSpoolSource(base_url="https://engine.invalid",
                                 token="t" * 12, session=_Boom())
        with self.assertRaises(FeedUnavailable):
            source.records()
        consumer = SpoolConsumer(source=source, store=ProcessedStore())
        self.assertEqual(consumer.pending(), [])
        self.assertEqual(consumer.stats["feed_errors"], 1)
        self.assertIn("ConnectionError", consumer.feed_error)

    def test_a_transport_error_never_quotes_the_token(self):
        token = "alpha-secret-token-value"

        class _Leaky:
            def get(self, *a, **kw):
                raise ConnectionError(f"failed with Bearer {token}")

        source = HttpSpoolSource(base_url="https://engine.invalid",
                                 token=token, session=_Leaky())
        try:
            source.records()
        except FeedUnavailable as e:
            self.assertNotIn(token, str(e))
            self.assertIn("<redacted:", str(e))
        else:
            self.fail("an unreachable feed must raise FeedUnavailable")

    def test_a_full_spool_refuses_the_write_rather_than_growing(self):
        before = self.witness()
        with patch.object(CFG, "RESEARCH_FEED_MAX_SPOOL", 2):
            # The bound is read when the spool is CONSTRUCTED, so the feed has
            # to be built inside the patch. A bound re-read on every write
            # would be one more filesystem-adjacent decision taken on the
            # engine's thread.
            self.feed = ResearchFeed()
            self.addCleanup(self.feed.writer.stop)
            for i in range(5):
                self.emit(market(ticker=f"KX-{i}"))
            self.assertLessEqual(len(self.spool_bytes()), 2)
        self.assertEqual(before["cfg"], self.witness()["cfg"])

    def test_aged_out_evidence_is_pruned_by_the_owner_of_the_bytes(self):
        self.emit(market(ticker="KX-OLD"))
        old = os.path.join(spool_dir(), sorted(self.spool_bytes())[0])
        stale = time.time() - float(CFG.RESEARCH_FEED_MAX_AGE_S) - 60
        os.utime(old, (stale, stale))
        self.emit(market(ticker="KX-NEW"))
        self.assertNotIn(os.path.basename(old), self.spool_bytes())
        self.assertEqual(self.feed.stats()["pruned"], 1)

    def test_the_producer_is_off_by_default_and_writes_nothing(self):
        with patch.object(CFG, "RESEARCH_FEED_ENABLED", False):
            self.assertFalse(self.emit())
        self.assertEqual(self.spool_bytes(), {})

    def test_the_tripwire_can_actually_fire(self):
        """Otherwise every zero above proves only that nothing was checked."""
        import kalshi_client
        with self.assertRaises(AssertionError):
            self.under_tripwire(
                lambda: kalshi_client.KalshiClient.create_order(
                    None, "t", "yes", 1, 40))


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
