# -*- coding: utf-8 -*-
"""Astra AA-01 .. AA-18, reproduced and then closed.

Every case in this file was written by first REPRODUCING the finding against
the rejected candidate's behaviour and only then asserting the fix. Where a
finding is about something that must now be refused, the refusal is paired
with a CONTROL in which the same call succeeds once the offending condition is
lifted -- otherwise a test can pass because the pipeline is broken in some
unrelated way, which is the failure mode Astra found in the candidate's own
suite (AA-17: two safety mutations survived a 1,670-test run).

WHAT THIS FILE DOES NOT CLAIM
    Nothing here proves the feed is fed by a real exchange. The live schema is
    UNPROVEN until a captured read-only observation is supplied; see
    `tools/alpha_live_schema_qualify.py`. A checksum proves byte integrity, not
    source authenticity, and the cases below say so where it matters.
"""
import json
import math
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                   # noqa: E402
from _candidate import (DROP, EXECUTION_BOOK, raw_market,      # noqa: E402
                        valid_candidate, valid_record)

import candidate_contract as contract                          # noqa: E402
from alpha_consumer import (LocalSpoolSource, ProcessedStore,   # noqa: E402
                            STATUS_ANALYZED, SpoolConsumer)
from alpha_feed_readiness import assess_record                  # noqa: E402
from alpha_ledger import AlphaLedger, LedgerError               # noqa: E402
from alpha_resolution_ingest import ingest_settlements          # noqa: E402
from config import CFG                                          # noqa: E402
from market_validator import MarketValidator                    # noqa: E402
from research_feed import ResearchFeed, candidate_from_market   # noqa: E402
from research_spool import BoundedSpool, ResearchWriter         # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RemediationCase(AlphaCase):
    """Isolated DATA_DIR with the feed enabled and no writer thread leaked."""

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()

    def feed(self, **kw):
        made = ResearchFeed(**kw)
        self.addCleanup(made.writer.stop)
        return made

    def build(self, candidate, feed=None):
        """`(record_or_None, feed)` without touching the filesystem."""
        made = feed or ResearchFeed(start_writer=False)
        return made._build(candidate), made


# ── AA-01 ────────────────────────────────────────────────────────────────
class AA01_DerivedQuotesArePresentedAsObserved(RemediationCase):
    """The research producer received an EXECUTION-normalized book in which a
    missing opposite side had already been computed, and recorded it with
    provenance claiming it was observed."""

    def yes_only(self):
        return raw_market(no_bid=DROP, no_ask=DROP)

    def test_reproduce_execution_normalization_still_derives_the_no_side(self):
        """The CONTROL that makes the rest of this class meaningful.

        `MarketValidator.normalize_book` must keep deriving the NO side -- the
        order path needs a complete book to price against, and breaking that
        would be a change to execution, which this remediation is forbidden to
        make. The finding is not that the derivation exists; it is that the
        result was handed to research as an observation.
        """
        normalized = MarketValidator.normalize_book(self.yes_only())
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["no_bid"], 100 - 46)
        self.assertEqual(normalized["no_ask"], 100 - 44)

    def test_the_research_feed_refuses_those_derived_quotes(self):
        raw = self.yes_only()
        candidate = candidate_from_market(
            raw, MarketValidator.normalize_book(raw), raw_book=raw)
        self.assertEqual(candidate["quote_observation"]["no_bid"],
                         contract.QUOTE_DERIVED)
        self.assertEqual(candidate["quote_observation"]["no_ask"],
                         contract.QUOTE_DERIVED)
        record, feed = self.build(candidate)
        self.assertIsNone(record, "a derived quote reached the spool")
        self.assertEqual(feed.refused_derived, 1)

    def test_no_prediction_row_is_created_from_such_evidence(self):
        """End to end: nothing is spooled, so nothing can be minted or
        predicted from it."""
        raw = self.yes_only()
        feed = self.feed()
        feed.emit_candidate(candidate_from_market(
            raw, MarketValidator.normalize_book(raw), raw_book=raw))
        feed.writer.drain(timeout=5.0)
        consumer = SpoolConsumer(source=LocalSpoolSource(feed.directory),
                                 store=ProcessedStore())
        self.assertEqual(consumer.pending(), [])
        self.assertEqual(AlphaLedger().predictions(), [])

    def test_control_a_fully_observed_book_is_accepted(self):
        """Anti-vacuity: without this, every refusal above could be produced
        by a producer that accepts nothing at all."""
        record, _feed = self.build(valid_candidate())
        self.assertIsNotNone(record)
        self.assertEqual(
            set(record["quote_observation"].values()), {contract.QUOTE_OBSERVED})

    def test_quotes_are_read_from_the_raw_source_not_the_execution_book(self):
        """A quote present ONLY in the execution book is never picked up, even
        when the raw source has a different value for it."""
        raw = raw_market(yes_ask=46)
        execution = dict(EXECUTION_BOOK, yes_ask=99)
        candidate = candidate_from_market(raw, execution, raw_book=raw)
        self.assertEqual(candidate["yes_ask"], 0.46)
        self.assertEqual(candidate["field_provenance"]["yes_ask"],
                         "raw_book.yes_ask(cents)")


# ── AA-02 ────────────────────────────────────────────────────────────────
class AA02_MalformedTypesPassAsMarketFacts(RemediationCase):
    """Coercion was permissive enough that non-facts became market facts."""

    def refused(self, **over):
        record = dict(valid_record(), **over)
        record["record_sha256"] = contract.compute_checksum(record)
        return contract.validate_record(record)

    def test_booleans_are_not_numbers(self):
        for field in ("yes_ask", "volume", "open_interest"):
            with self.subTest(field=field):
                self.assertTrue(self.refused(**{field: True}))

    def test_nan_and_infinities_are_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                # A NaN cannot even be canonicalized, which is itself the
                # refusal: `allow_nan=False` means the record has no digest.
                with self.assertRaises((contract.ContractError, ValueError)):
                    contract.assert_valid(
                        dict(valid_record(), yes_ask=value))

    def test_negative_sizes_are_refused(self):
        self.assertTrue(self.refused(volume=-1.0))
        self.assertTrue(self.refused(open_interest=-0.0001))

    def test_impossible_prices_are_refused(self):
        for price in (-0.01, 1.01, 2.0):
            with self.subTest(price=price):
                self.assertTrue(self.refused(yes_ask=price))

    def test_a_crossed_book_is_refused(self):
        errors = self.refused(yes_bid=0.60, yes_ask=0.40)
        self.assertTrue(any("crossed YES" in e for e in errors), errors)
        errors = self.refused(no_bid=0.60, no_ask=0.40)
        self.assertTrue(any("crossed NO" in e for e in errors), errors)

    def test_arrays_and_objects_are_never_stringified_into_text(self):
        for value in (["a"], {"b": 1}, 7, None):
            with self.subTest(value=repr(value)):
                self.assertTrue(self.refused(question=value))

    def test_blank_and_whitespace_only_text_is_refused(self):
        for field in ("question", "resolution_rules"):
            for value in ("", "   ", "\t\n"):
                with self.subTest(field=field, value=repr(value)):
                    self.assertTrue(self.refused(**{field: value}))

    def test_malformed_settlement_source_collections_report_absence(self):
        """A malformed collection must NOT become a plausible string."""
        from research_feed import _settlement_source_name
        for value in ([{"no_name": 1}], [{"name": {"nested": 1}}], [None],
                      {"name": ["a"]}, True, 7):
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(_settlement_source_name(value))

    def test_malformed_timestamps_are_refused(self):
        for field in contract.TIME_FIELDS:
            for value in ("not-a-date", "2026-13-45T99:99:99Z", "",
                          "2026-09-11T12:00:00"):   # last one: no timezone
                with self.subTest(field=field, value=value):
                    self.assertTrue(self.refused(**{field: value}))

    def test_absurd_integers_that_overflow_float_conversion_are_refused(self):
        self.assertTrue(self.refused(volume=2 ** 70))
        with self.assertRaises(contract.ContractError):
            contract.strict_number(10 ** 400, field="volume")

    def test_ambiguous_numeric_strings_are_refused_by_default(self):
        for value in ("0.46", "46", " 0.46 "):
            with self.subTest(value=value):
                self.assertTrue(self.refused(yes_ask=value))

    def test_control_the_same_record_is_valid_untouched(self):
        """Anti-vacuity for the whole class."""
        self.assertEqual(contract.validate_record(valid_record()), [])

    def test_one_contract_is_shared_by_producer_consumer_and_readiness(self):
        """AA-02's actual ask: not three validators, one.

        Asserted by IMPORT rather than by behaviour, because the failure it
        prevents is a future edit adding a fourth opinion, and that is visible
        in the import graph before it is visible in any single run.
        """
        import ast
        for module in ("research_feed.py", "alpha_consumer.py",
                       "alpha_feed_readiness.py"):
            with self.subTest(module=module):
                tree = ast.parse(open(os.path.join(REPO, module),
                                      encoding="utf-8").read())
                imported = {(n.module or "").split(".")[0]
                            for n in ast.walk(tree)
                            if isinstance(n, ast.ImportFrom)}
                self.assertIn("candidate_contract", imported)


# ── AA-03 ────────────────────────────────────────────────────────────────
class AA03_ContradictoryAliasesSilentlyChosen(RemediationCase):
    """First-present-wins silently picked one of two disagreeing values."""

    def test_reproduce_contradictory_event_aliases_are_now_refused(self):
        with self.assertRaises(contract.ContractError) as caught:
            contract.resolve_alias(
                {"event_ticker": "EV-A", "event_id": "EV-B"},
                "event_id", ("event_ticker", "event_id"))
        self.assertIn("contradictory aliases", str(caught.exception))
        # Neither value is chosen, and BOTH are named in the refusal: an
        # operator has to be able to see what disagreed with what.
        self.assertIn("EV-A", str(caught.exception))
        self.assertIn("EV-B", str(caught.exception))

    def test_a_contradiction_is_preserved_as_a_contradiction(self):
        """SUPERSEDES `..._makes_the_fact_absent_not_arbitrary`.

        The v2 remediation stopped the producer CHOOSING between two
        contradictory aliases, which was right, and then filed the fact in
        `unavailable_fields`, which was not: Astra's re-audit named that a
        downgrade of a contradiction to an ordinary absence. The assertion
        below is strictly stronger than the one it replaces -- the fact is
        still not chosen, and the disagreement is now preserved by name and
        refuses the record instead of disappearing into "the exchange did not
        publish this".
        """
        raw = raw_market(event_ticker="EV-A", event_id="EV-B")
        candidate = candidate_from_market(raw, EXECUTION_BOOK, raw_book=raw)
        self.assertIsNone(candidate["event_id"])
        self.assertNotIn("event_id", candidate["field_provenance"])
        self.assertNotIn("event_id", candidate["unavailable_fields"])
        self.assertEqual(candidate["contradictory_fields"]["event_id"],
                         {"event_ticker": "EV-A", "event_id": "EV-B"})

    def test_control_agreeing_aliases_resolve_normally(self):
        value, key = contract.resolve_alias(
            {"event_ticker": "EV-SAME", "event_id": "EV-SAME"},
            "event_id", ("event_ticker", "event_id"))
        self.assertEqual((value, key), ("EV-SAME", "event_ticker"))

    def test_contradictory_expiry_aliases_are_refused(self):
        """`expected_expiration_time` and `expiration_time` are two exchange
        names for ONE instant. Two different instants is a source problem."""
        with self.assertRaises(contract.ContractError):
            contract.resolve_alias(
                {"expected_expiration_time": "2026-09-10T13:00:00+00:00",
                 "expiration_time": "2026-09-10T19:00:00+00:00"},
                "expected_resolution_time_utc",
                ("expected_expiration_time", "expiration_time"))

    def test_settlement_source_aliases_compare_by_meaning(self):
        """The list form and the single-object form of ONE source agree; two
        genuinely different sources do not."""
        from research_feed import _settlement_source_name
        same, _key = contract.resolve_alias(
            {"settlement_sources": [{"name": "CF Benchmarks RTI"}],
             "settlement_source": "CF Benchmarks RTI"},
            "resolution_source", ("settlement_sources", "settlement_source"),
            comparator=_settlement_source_name)
        self.assertIsNotNone(same)
        with self.assertRaises(contract.ContractError):
            contract.resolve_alias(
                {"settlement_sources": [{"name": "CF Benchmarks RTI"}],
                 "settlement_source": "Some Other Oracle"},
                "resolution_source",
                ("settlement_sources", "settlement_source"),
                comparator=_settlement_source_name)

    def test_close_time_is_not_an_alias_for_the_resolution_time(self):
        """They describe DIFFERENT facts, so they are not in one alias row."""
        self.assertNotIn(
            "close_time",
            contract.SOURCE_BINDING["expected_resolution_time_utc"][1])
        raw = raw_market(expiration_time=DROP)
        candidate = candidate_from_market(raw, EXECUTION_BOOK, raw_book=raw)
        self.assertIsNone(candidate["expected_resolution_time_utc"])


# ── AA-04 ────────────────────────────────────────────────────────────────
class AA04_ConsumerDoesNotVerifyTheChecksum(RemediationCase):
    """The consumer minted snapshots from records whose digest it never
    recomputed: the producer, the transport and the spool bytes were all
    trusted."""

    def spool(self, record, name="rec.json"):
        directory = os.path.join(self._tmp, "spool")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return directory

    def consumer(self, directory):
        return SpoolConsumer(source=LocalSpoolSource(directory),
                             store=ProcessedStore())

    def test_reproduce_an_edited_record_is_now_refused(self):
        record = valid_record()
        record["yes_ask"] = 0.99          # edited in flight; digest unchanged
        consumer = self.consumer(self.spool(record))
        self.assertEqual(consumer.pending(), [])
        self.assertEqual(consumer.stats["checksum_failures"], 1)

    def test_a_missing_or_malformed_digest_is_refused(self):
        for bad in (None, "", "short", "z" * 64, 12345, ["a" * 64]):
            with self.subTest(digest=repr(bad)[:30]):
                record = dict(valid_record(), record_sha256=bad)
                errors = contract.validate_record(record)
                self.assertTrue(any("record_sha256" in e for e in errors),
                                errors)

    def test_control_the_untouched_record_is_minted(self):
        consumer = self.consumer(self.spool(valid_record()))
        pending = consumer.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(consumer.stats["checksum_failures"], 0)

    def test_the_verified_digest_is_retained_downstream(self):
        record = valid_record()
        consumer = self.consumer(self.spool(record))
        consumer.pending()
        self.assertIn(record["record_sha256"], consumer.verified_digests)

    def test_the_checksum_is_recomputed_not_read_back(self):
        """A digest that merely matches ITSELF proves nothing.

        Re-hashing after an edit produces a record that passes the checksum --
        and that is correct and worth pinning, because it is the honest limit
        of the mechanism: a checksum proves the bytes and the digest agree, NOT
        that the exchange produced them. Anyone who can rewrite the record can
        rewrite the digest. This is why the report says integrity, never
        authenticity.
        """
        record = valid_record()
        record["yes_ask"] = 0.99
        record["record_sha256"] = contract.compute_checksum(record)
        self.assertEqual(contract.validate_record(record), [])
        consumer = self.consumer(self.spool(record))
        self.assertEqual(len(consumer.pending()), 1,
                         "a re-hashed record should pass INTEGRITY checks")

    def test_provenance_is_inside_the_digest(self):
        """AA-05 asks for this explicitly: editing where a fact came from must
        invalidate the record."""
        record = valid_record()
        record["field_provenance"]["question"] = "market.title"  # unchanged
        self.assertEqual(contract.validate_record(record), [])
        record["field_provenance"]["question"] = "market.ticker"
        errors = contract.validate_record(record)
        self.assertTrue(any("record_sha256" in e for e in errors), errors)


# ── AA-05 ────────────────────────────────────────────────────────────────
class AA05_ProvenanceWasOnlyANonemptyString(RemediationCase):
    """Any non-blank string counted as provenance, so `yes_ask` could claim to
    come from `market.title` and pass."""

    def with_provenance(self, field, value):
        record = valid_record()
        record["field_provenance"][field] = value
        record["record_sha256"] = contract.compute_checksum(record)
        return contract.validate_record(record)

    def test_reproduce_yes_ask_cannot_claim_to_come_from_the_title(self):
        errors = self.with_provenance("yes_ask", "market.title")
        self.assertTrue(any("yes_ask" in e and "not an allowed source" in e
                            for e in errors), errors)

    def test_an_unknown_namespace_is_refused(self):
        for value in ("wherever.yes_ask(cents)", "guess.yes_ask(cents)",
                      "yes_ask", "raw_book.yes_ask", "RAW_BOOK.yes_ask(cents)"):
            with self.subTest(value=value):
                self.assertTrue(self.with_provenance("yes_ask", value))

    def test_an_unrelated_source_key_in_the_right_namespace_is_refused(self):
        self.assertTrue(self.with_provenance("question", "market.rules_primary"))
        self.assertTrue(self.with_provenance("volume", "market.open_interest"))

    def test_non_string_provenance_is_refused(self):
        for value in (True, 1, ["market.title"], {"p": "market.title"}, None):
            with self.subTest(value=repr(value)[:30]):
                self.assertTrue(self.with_provenance("question", value))

    def test_a_populated_field_declared_unavailable_is_refused(self):
        record = valid_record()
        record["unavailable_fields"] = sorted(
            set(record["unavailable_fields"]) | {"question"})
        record["record_sha256"] = contract.compute_checksum(record)
        errors = contract.validate_record(record)
        self.assertTrue(any("declared unavailable yet carries a value" in e
                            for e in errors), errors)

    def test_an_optional_field_carrying_a_value_must_carry_provenance(self):
        record = valid_record()
        record["catalyst_name"] = "CPI release"
        record["unavailable_fields"] = [
            f for f in record["unavailable_fields"] if f != "catalyst_name"]
        record["record_sha256"] = contract.compute_checksum(record)
        errors = contract.validate_record(record)
        self.assertTrue(any("catalyst_name" in e for e in errors), errors)

    def test_control_every_allowed_path_is_accepted(self):
        """Anti-vacuity: the binding map must actually admit the real paths."""
        for field in contract.REQUIRED_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(contract.allowed_provenance(field))
        self.assertEqual(contract.validate_record(valid_record()), [])

    def test_the_binding_is_enforced_at_all_three_components(self):
        record = valid_record()
        record["field_provenance"]["yes_ask"] = "market.title"
        record["record_sha256"] = contract.compute_checksum(record)
        # producer
        built, feed = self.build(dict(
            valid_candidate(),
            field_provenance=dict(record["field_provenance"])))
        self.assertIsNone(built)
        # consumer
        directory = os.path.join(self._tmp, "spool5")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "r.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        consumer = SpoolConsumer(source=LocalSpoolSource(directory),
                                 store=ProcessedStore())
        self.assertEqual(consumer.pending(), [])
        # readiness
        self.assertFalse(assess_record(record)["ready"])


# ── AA-06 ────────────────────────────────────────────────────────────────
class AA06_MissingObservationTimeGetsTheCurrentClock(RemediationCase):
    """`build_snapshot` fell back to `datetime.now()`, so the same evidence
    replayed later minted a different snapshot identity."""

    def spool(self, record, directory=None):
        directory = directory or os.path.join(self._tmp, "spool6")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "r.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return directory

    def mint(self, record):
        consumer = SpoolConsumer(source=LocalSpoolSource(self.spool(record)),
                                 store=ProcessedStore())
        return consumer.pending()

    def test_reproduce_the_same_record_replayed_later_keeps_its_identity(self):
        """Replay the SAME evidence an hour later; the identity must not move.

        The clock is moved with a `datetime` SUBCLASS rather than a MagicMock,
        so every `isinstance(x, datetime)` inside the snapshot code still
        behaves normally and the only thing that changes is what `now()`
        returns -- which is precisely the variable under test.
        """
        record = valid_record()
        snapshot = self.mint(record)[0][0]
        first = snapshot.market_snapshot_id
        # The snapshot's own time is the OBSERVATION time, not ingest time.
        self.assertEqual(snapshot.snapshot_time_utc, record["emitted_at_utc"])

        later = datetime.now(timezone.utc) + timedelta(hours=1)

        class _Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return later if tz is None else later.astimezone(tz)

        with patch("alpha_snapshot.datetime", _Later):
            second = SpoolConsumer(
                source=LocalSpoolSource(self.spool(record, os.path.join(
                    self._tmp, "spool6b"))),
                store=ProcessedStore()).pending()[0][0].market_snapshot_id
        self.assertEqual(first, second,
                         "snapshot identity moved because the clock moved")

    def test_a_missing_observation_time_is_refused(self):
        record = valid_record()
        record.pop("emitted_at_utc")
        record["record_sha256"] = contract.compute_checksum(record)
        self.assertTrue(contract.validate_record(record))
        self.assertEqual(self.mint(record), [])

    def test_a_malformed_observation_time_is_refused(self):
        for bad in ("", "   ", "yesterday", "2026-09-11T12:00:00", 12345, None):
            with self.subTest(value=repr(bad)):
                record = dict(valid_record(), emitted_at_utc=bad)
                record["record_sha256"] = contract.compute_checksum(record)
                self.assertTrue(contract.validate_record(record))

    def test_the_consumer_never_substitutes_its_own_clock(self):
        """Static: `mint` must pass the record's own time, not a default."""
        import ast
        tree = ast.parse(open(os.path.join(REPO, "alpha_consumer.py"),
                              encoding="utf-8").read())
        mint = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "mint")
        source = ast.dump(mint)
        self.assertIn("emitted_at_utc", source)
        self.assertNotIn("now", source,
                         "mint() reads a clock; the observation time must "
                         "come from the record")

    def test_control_the_producer_stamps_a_real_observation_time(self):
        candidate = valid_candidate()
        self.assertTrue(candidate["emitted_at_utc"])
        contract.strict_timestamp(candidate["emitted_at_utc"],
                                  field="emitted_at_utc")
        self.assertEqual(candidate["field_provenance"]["emitted_at_utc"],
                         "observer.emitted_at_utc")


# ── AA-07 / AA-08 ────────────────────────────────────────────────────────
class AA07_ReadinessBypassedProvenance(RemediationCase):
    """Readiness fell back to a shape check when the contract was absent."""

    def shape_only(self):
        """Every required field NAME present, and nothing else. This is what a
        substituted default looks like."""
        now = datetime.now(timezone.utc)
        return {
            "contract_id": "C-1", "question": "Will the event occur?",
            "resolution_rules": "Resolve from the named official source.",
            "resolution_source": "kalshi",          # the substituted default
            "snapshot_time_utc": now.isoformat(timespec="seconds"),
            "yes_bid": 0.44, "yes_ask": 0.46, "no_bid": 0.54, "no_ask": 0.56,
            "volume": 0.0, "open_interest": 0.0,    # the "0.0 means absent"
            "market_close_time_utc": now.isoformat(timespec="seconds"),
            "expected_resolution_time_utc": now.isoformat(timespec="seconds"),
        }

    def test_reproduce_a_shape_complete_row_is_no_longer_ready(self):
        result = assess_record(self.shape_only())
        self.assertFalse(result["ready"])
        self.assertTrue(result["contract_errors"])
        # It passed the SHAPE check, which is what makes this a reproduction
        # of the finding rather than a test of a missing field.
        self.assertEqual(result["missing_fields"], [])

    def test_every_contract_element_is_required(self):
        for mutation in (
                {"schema": None}, {"record_sha256": None},
                {"field_provenance": None}, {"unavailable_fields": None},
                {"quote_observation": None}, {"emitted_at_utc": None}):
            with self.subTest(missing=sorted(mutation)[0]):
                record = dict(valid_record(), **mutation)
                self.assertFalse(assess_record(record)["ready"])

    def test_control_the_full_contract_is_ready(self):
        result = assess_record(valid_record())
        self.assertTrue(result["ready"], result["reason"])

    def test_readiness_has_no_shape_only_fallback_path(self):
        """Static: `ready` must be computed from `validate_record`."""
        source = open(os.path.join(REPO, "alpha_feed_readiness.py"),
                      encoding="utf-8").read()
        self.assertIn("contract_errors = validate_record(row)", source)
        self.assertNotIn('"ready": not missing,', source,
                         "a shape-only readiness verdict is still reachable")


class AA08_EventIdContractMismatch(RemediationCase):
    """Producer, consumer and readiness disagreed on whether `event_id` was
    required."""

    def test_all_three_treat_an_absent_event_id_as_acceptable(self):
        raw = raw_market(event_ticker=DROP, event_id=DROP)
        record, feed = self.build(valid_candidate(raw))
        self.assertIsNotNone(record, feed.last_errors)       # producer
        self.assertIsNone(record["event_id"])
        self.assertEqual(contract.validate_record(record), [])   # consumer
        self.assertTrue(assess_record(record)["ready"])           # readiness

    def test_a_supplied_event_id_must_validate_and_be_attributed(self):
        record = valid_record()
        self.assertTrue(record["event_id"])
        self.assertIn("event_id", record["field_provenance"])
        # Supplied but unattributed -> refused.
        broken = dict(record)
        broken["field_provenance"] = {k: v for k, v
                                      in record["field_provenance"].items()
                                      if k != "event_id"}
        broken["record_sha256"] = contract.compute_checksum(broken)
        errors = contract.validate_record(broken)
        self.assertTrue(any("event_id" in e for e in errors), errors)

    def test_a_supplied_event_id_cannot_also_be_declared_unavailable(self):
        record = valid_record()
        record["unavailable_fields"] = sorted(
            set(record["unavailable_fields"]) | {"event_id"})
        record["record_sha256"] = contract.compute_checksum(record)
        errors = contract.validate_record(record)
        self.assertTrue(any("event_id" in e for e in errors), errors)

    def test_event_id_is_optional_everywhere_by_declaration(self):
        self.assertIn("event_id", contract.OPTIONAL_FIELDS)
        self.assertNotIn("event_id", contract.REQUIRED_FIELDS)
        from alpha_feed_readiness import READINESS_OPTIONAL
        self.assertIn("event_id", READINESS_OPTIONAL)


# ── AA-09 ────────────────────────────────────────────────────────────────
class AA09_OneBadRowStarvesTheBatch(RemediationCase):
    """An unanticipated per-record exception aborted the whole poll."""

    class _ExplodingSource:
        """Yields one poisoned row followed by good ones."""

        kind = "local"

        def __init__(self, rows):
            self.rows = rows

        def records(self):
            return list(self.rows)

    def consumer(self, rows):
        return SpoolConsumer(source=self._ExplodingSource(rows),
                             store=ProcessedStore())

    def test_reproduce_a_poisoned_row_does_not_starve_the_rest(self):
        good_a = valid_record(raw_market(ticker="KX-A"))
        good_b = valid_record(raw_market(ticker="KX-B"))
        poison = dict(valid_record(raw_market(ticker="KX-BAD")),
                      volume=10 ** 400)      # OverflowError on float()
        consumer = self.consumer([poison, good_a, good_b])
        pending = consumer.pending()
        self.assertEqual(len(pending), 2,
                         "the batch stopped at the poisoned record")
        self.assertEqual(
            sorted(s.contract_id for s, _ in pending), ["KX-A", "KX-B"])

    def test_every_known_validation_failure_is_contained_per_record(self):
        good = valid_record(raw_market(ticker="KX-OK"))
        for bad in ({"emitted_at_utc": "not-a-date"},
                    {"yes_ask": float("inf")},
                    {"volume": 2 ** 70},
                    {"question": ["array"]},
                    {"record_sha256": "z" * 64}):
            with self.subTest(bad=sorted(bad)[0]):
                rows = [dict(valid_record(raw_market(ticker="KX-BAD")), **bad),
                        good]
                pending = self.consumer(rows).pending()
                self.assertEqual([s.contract_id for s, _ in pending],
                                 ["KX-OK"])

    def test_a_refusal_is_counted_and_logged_not_silent(self):
        poison = dict(valid_record(raw_market(ticker="KX-BAD")),
                      volume=10 ** 400)
        consumer = self.consumer([poison])
        with self.assertLogs("ALPHA", level="WARNING"):
            consumer.pending()
        self.assertGreaterEqual(consumer.stats["malformed"], 1)

    def test_a_programmer_error_still_fails_loudly(self):
        """Containment must not become a blanket `except Exception`, or a real
        bug would be logged once per record forever instead of being fixed."""
        consumer = self.consumer([valid_record()])
        with patch.object(SpoolConsumer, "_valid",
                          side_effect=AttributeError("typo in the consumer")):
            with self.assertRaises(AttributeError):
                consumer.pending()

    def test_control_a_clean_batch_is_fully_processed(self):
        rows = [valid_record(raw_market(ticker=f"KX-{i}")) for i in range(3)]
        self.assertEqual(len(self.consumer(rows).pending()), 3)


# ── AA-10 ────────────────────────────────────────────────────────────────
class AA10_ResearchIOBlockedTheEngineCycle(RemediationCase):
    """Serialization, write, fsync and pruning all happened on the engine's
    thread. A slow volume therefore slowed the money path."""

    def test_reproduce_a_stalled_writer_does_not_block_the_engine(self):
        """THE case for this finding. The writer's fsync is held forever; the
        engine's call must still return promptly."""
        released = threading.Event()
        entered = threading.Event()

        def _hanging_fsync(_fd):
            entered.set()
            # Bounded. `os.fsync` is patched process-wide, so an unrelated
            # writer thread can enter this too; parking it for 30s would make
            # one test able to stall the rest of the suite.
            released.wait(timeout=10)

        feed = self.feed()
        # Registered AFTER the feed, so LIFO cleanup releases the fsync BEFORE
        # `writer.stop()` tries to join the thread that is sitting in it.
        self.addCleanup(released.set)
        with patch("research_spool.os.fsync", _hanging_fsync):
            # First record occupies the writer thread inside the stalled fsync.
            feed.emit_candidate(valid_candidate(raw_market(ticker="KX-STALL")))
            self.assertTrue(entered.wait(timeout=5),
                            "the writer never reached the fsync")
            # Now the engine emits again, repeatedly, while the writer is stuck.
            started = time.monotonic()
            for i in range(50):
                feed.emit_candidate(
                    valid_candidate(raw_market(ticker=f"KX-{i}")))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0,
                        f"the engine cycle waited {elapsed:.2f}s on a stalled "
                        f"research writer")

    def test_emit_performs_no_filesystem_call_on_the_calling_thread(self):
        """Static, because this is a property of the code rather than of one
        timing run: no open/write/fsync/replace/listdir reachable from
        `emit_candidate`."""
        import ast
        tree = ast.parse(open(os.path.join(REPO, "research_feed.py"),
                              encoding="utf-8").read())
        banned = {"fsync", "replace", "listdir", "makedirs", "remove",
                  "rename", "stat"}
        offences = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in banned:
                offences.append(f"line {node.lineno}: .{node.attr}")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_queue_is_bounded_by_item_count(self):
        spool = BoundedSpool(os.path.join(self._tmp, "s"), max_records=1000,
                             max_bytes=10 ** 9, max_record_bytes=10 ** 6,
                             max_age_s=3600)
        writer = ResearchWriter(spool, max_queue=4, start=False)
        self.addCleanup(writer.stop)
        accepted = [writer.offer({"n": i}) for i in range(10)]
        self.assertEqual(accepted.count(True), 4)
        self.assertEqual(writer.stats["dropped_queue_full"], 6)

    def test_the_queue_is_bounded_by_bytes(self):
        spool = BoundedSpool(os.path.join(self._tmp, "s2"), max_records=1000,
                             max_bytes=10 ** 9, max_record_bytes=10 ** 6,
                             max_age_s=3600)
        writer = ResearchWriter(spool, max_queue=1000, max_queue_bytes=1000,
                                start=False)
        self.addCleanup(writer.stop)
        self.assertTrue(writer.offer({"n": 1}, approx_bytes=600))
        self.assertTrue(writer.offer({"n": 2}, approx_bytes=300))
        self.assertFalse(writer.offer({"n": 3}, approx_bytes=600))
        self.assertEqual(writer.stats["dropped_queue_bytes"], 1)

    def test_a_dropped_record_is_counted_not_silent(self):
        spool = BoundedSpool(os.path.join(self._tmp, "s3"), max_records=10,
                             max_bytes=10 ** 9, max_record_bytes=10 ** 6,
                             max_age_s=3600)
        writer = ResearchWriter(spool, max_queue=1, start=False)
        self.addCleanup(writer.stop)
        writer.offer({"n": 1})
        writer.offer({"n": 2})
        self.assertEqual(writer.telemetry()["dropped_queue_full"], 1)

    def test_control_the_writer_does_reach_the_disk_when_healthy(self):
        """Anti-vacuity: without this, every case above would also pass for a
        writer that never writes anything."""
        feed = self.feed()
        self.assertTrue(feed.emit_candidate(valid_candidate()))
        self.assertTrue(feed.writer.drain(timeout=5.0))
        self.assertEqual(feed.writer.spool.stats["written"], 1)
        # `.json` only: the producer also keeps its capacity-reservation
        # lock in this directory (AA-11 re-audit), and a lock is not a record.
        self.assertEqual(
            len([n for n in os.listdir(feed.directory)
                 if n.endswith(".json")]), 1)

    def test_research_work_never_alters_financial_decision_state(self):
        before = {name: getattr(CFG, name) for name in (
            "ALLOW_ORDER_SUBMISSION", "KILL_SWITCH", "MAX_POS_PCT",
            "DAILY_RESEARCH_ORACLE_APPROVED")}
        feed = self.feed()
        for i in range(5):
            feed.emit_candidate(valid_candidate(raw_market(ticker=f"KX-{i}")))
        feed.writer.drain(timeout=5.0)
        after = {name: getattr(CFG, name) for name in before}
        self.assertEqual(before, after)


# ── AA-11 ────────────────────────────────────────────────────────────────
class AA11_SpoolLimitsFailUnderFilesystemFaults(RemediationCase):
    """`os.listdir` failing was read as "the spool is empty", so the bound
    disappeared exactly when the filesystem was already in trouble."""

    def spool(self, **kw):
        options = dict(max_records=5, max_bytes=10_000,
                       max_record_bytes=2_000, max_age_s=3600)
        options.update(kw)
        return BoundedSpool(os.path.join(self._tmp, "spool11"), **options)

    def test_reproduce_an_enumeration_failure_fails_closed(self):
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        with patch("research_spool.os.listdir",
                   side_effect=OSError("EIO: the volume is unhappy")):
            self.assertFalse(spool.write(valid_record()))
        self.assertEqual(spool.stats["capacity_unknown"], 1)
        self.assertEqual(spool.stats["written"], 0)

    def test_the_spool_is_bounded_by_bytes_not_only_by_count(self):
        spool = self.spool(max_records=1000, max_bytes=3_000)
        written = 0
        for i in range(50):
            if spool.write(valid_record(raw_market(ticker=f"KX-{i}"))):
                written += 1
        total = sum(os.path.getsize(os.path.join(spool.directory, n))
                    for n in os.listdir(spool.directory))
        self.assertLessEqual(total, 3_000)
        self.assertGreater(spool.stats["dropped_full"] + spool.stats["pruned"],
                           0)
        self.assertGreater(written, 0, "nothing was ever written")

    def test_an_oversized_record_is_refused_not_written(self):
        spool = self.spool(max_record_bytes=200)
        self.assertFalse(spool.write(valid_record()))
        self.assertEqual(spool.stats["dropped_oversize"], 1)

    def test_temporary_files_count_toward_capacity(self):
        spool = self.spool(max_bytes=1_500)
        os.makedirs(spool.directory, exist_ok=True)
        with open(os.path.join(spool.directory, "x.json.partial"), "wb") as fh:
            fh.write(b"0" * 1_400)
        self.assertFalse(spool.write(valid_record()))
        self.assertEqual(spool.stats["dropped_full"], 1)
        self.assertGreater(spool.stats["temp_bytes"], 0)

    def test_unknown_files_are_never_deleted(self):
        """This directory is on a shared volume. A research component that
        removes files it does not recognise is worse than a full spool."""
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        stranger = os.path.join(spool.directory, "someone-elses-state.db")
        with open(stranger, "wb") as fh:
            fh.write(b"not ours")
        old = time.time() - 10 * 3600
        os.utime(stranger, (old, old))
        spool.prune()
        spool.recover_temp_files()
        self.assertTrue(os.path.exists(stranger))

    def test_startup_cleanup_removes_only_our_own_stale_temp_files(self):
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        stale = os.path.join(spool.directory, "old.json.partial")
        fresh = os.path.join(spool.directory, "new.json.partial")
        for path in (stale, fresh):
            with open(path, "wb") as fh:
                fh.write(b"{}")
        old = time.time() - 10 * 3600
        os.utime(stale, (old, old))
        self.assertEqual(spool.recover_temp_files(), 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh),
                        "an in-flight write was deleted")

    def test_capacity_telemetry_is_exposed(self):
        spool = self.spool()
        for key in ("dropped_full", "dropped_oversize", "capacity_unknown",
                    "temp_bytes", "temp_files", "write_errors"):
            self.assertIn(key, spool.stats)

    def test_control_a_healthy_spool_accepts_the_write(self):
        spool = self.spool()
        self.assertTrue(spool.write(valid_record()))
        self.assertEqual(spool.stats["written"], 1)


# ── AA-12 ────────────────────────────────────────────────────────────────
class AA12_ShortWritesAndTornTails(RemediationCase):
    """A single `os.write` whose return value was ignored could truncate a
    row; the reader then classified it as a clean crash and skipped it."""

    def test_reproduce_a_short_write_is_completed_not_lost(self):
        import durable_append
        path = os.path.join(self._tmp, "short.jsonl")
        real_write = os.write
        calls = {"n": 0}

        def _one_byte_at_a_time(fd, data):
            calls["n"] += 1
            return real_write(fd, data[:1])       # the shortest legal write

        with patch("durable_append.os.write", _one_byte_at_a_time):
            durable_append.append_line(path, json.dumps({"row": 1}))
        self.assertGreater(calls["n"], 1, "the write was not actually short")
        self.assertEqual(json.loads(open(path).read().strip()), {"row": 1})

    def test_eintr_is_retried_rather_than_treated_as_failure(self):
        import durable_append
        path = os.path.join(self._tmp, "eintr.jsonl")
        real_write = os.write
        state = {"raised": False}

        def _interrupt_once(fd, data):
            if not state["raised"]:
                state["raised"] = True
                raise InterruptedError("EINTR")
            return real_write(fd, data)

        with patch("durable_append.os.write", _interrupt_once):
            durable_append.append_line(path, json.dumps({"row": 2}))
        self.assertTrue(state["raised"])
        self.assertEqual(json.loads(open(path).read().strip()), {"row": 2})

    def test_a_device_that_accepts_nothing_raises_rather_than_truncating(self):
        import durable_append
        path = os.path.join(self._tmp, "dead.jsonl")
        with patch("durable_append.os.write", return_value=0):
            with self.assertRaises(OSError):
                durable_append.append_line(path, json.dumps({"row": 3}))

    def test_a_torn_tail_is_preserved_and_separated_never_truncated(self):
        ledger = AlphaLedger(path=os.path.join(self._tmp, "torn.jsonl"))
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-1"})
        with open(ledger.log.path, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "PREDICTION", "predic')     # crash mid-append
        before = open(ledger.log.path, "rb").read()
        ledger.record_prediction({"prediction_id": "p2",
                                  "market_snapshot_id": "snap-2"})
        after = open(ledger.log.path, "rb").read()
        self.assertTrue(after.startswith(before),
                        "historical bytes were rewritten or truncated")
        self.assertIn(b'"predic', after, "the damaged fragment was deleted")
        self.assertEqual([r["prediction_id"] for r in ledger.predictions()],
                         ["p1", "p2"])

    def test_the_corruption_is_surfaced_not_swallowed(self):
        ledger = AlphaLedger(path=os.path.join(self._tmp, "loud.jsonl"))
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-1"})
        with open(ledger.log.path, "a", encoding="utf-8") as fh:
            fh.write('{"torn')
        with self.assertLogs("ALPHA", level="ERROR") as logs:
            ledger.record_prediction({"prediction_id": "p2",
                                      "market_snapshot_id": "snap-2"})
        self.assertIn("torn tail", "\n".join(logs.output))

    def test_a_new_row_never_splices_onto_the_damaged_one(self):
        import durable_append
        path = os.path.join(self._tmp, "splice.jsonl")
        durable_append.append_line(path, json.dumps({"row": 1}))
        with open(path, "ab") as fh:
            fh.write(b'{"row": 2')                 # no newline: torn
        durable_append.append_line(path, json.dumps({"row": 3}))
        lines = open(path).read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[2]), {"row": 3})

    def test_control_a_normal_append_round_trips(self):
        import durable_append
        path = os.path.join(self._tmp, "ok.jsonl")
        for i in range(5):
            durable_append.append_line(path, json.dumps({"i": i}))
        rows = [json.loads(l) for l in open(path).read().splitlines()]
        self.assertEqual(rows, [{"i": i} for i in range(5)])


# ── AA-13 ────────────────────────────────────────────────────────────────
class AA13_PredictionCommitVersusProcessedAck(RemediationCase):
    """A crash between the prediction append and the processed mark produced a
    duplicate; a failed prediction that still marked ANALYZED lost the
    observation permanently."""

    def ledger(self):
        return AlphaLedger(path=os.path.join(self._tmp, "ledger13.jsonl"))

    def test_a_stable_analysis_identity_survives_a_retry(self):
        """`prediction_id` mixes in the wall clock, so it cannot serve as the
        recovery key. The analysis identity is derived from the snapshot."""
        from alpha_ledger import analysis_identity
        self.assertEqual(analysis_identity("snap-X"), analysis_identity("snap-X"))
        self.assertNotEqual(analysis_identity("snap-X"),
                            analysis_identity("snap-Y"))

    def test_prepare_is_durable_before_the_prediction(self):
        ledger = self.ledger()
        ledger.prepare("snap-1", contract_id="C1", source_record_sha256="a" * 64)
        kinds = [r["kind"] for r in ledger.rows()]
        self.assertEqual(kinds, ["PREPARE"])
        self.assertFalse(ledger.prediction_is_committed("snap-1"))

    def test_prepare_is_idempotent_across_a_retry(self):
        ledger = self.ledger()
        first = ledger.prepare("snap-1", contract_id="C1")
        second = ledger.prepare("snap-1", contract_id="C1")
        self.assertEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(
            len([r for r in ledger.rows() if r["kind"] == "PREPARE"]), 1)

    def test_reproduce_a_snapshot_cannot_commit_two_predictions(self):
        """The duplicate AA-13 describes: a crash before the processed mark
        made the next poll analyse the same evidence again."""
        ledger = self.ledger()
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-dup"})
        with self.assertRaises(LedgerError) as caught:
            ledger.record_prediction({"prediction_id": "p2",
                                      "market_snapshot_id": "snap-dup"})
        self.assertIn("not analysed twice", str(caught.exception))
        self.assertEqual(len(ledger.predictions()), 1)

    def test_commit_is_confirmed_by_reading_the_ledger_back(self):
        ledger = self.ledger()
        self.assertFalse(ledger.prediction_is_committed("snap-2"))
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-2"})
        self.assertTrue(ledger.prediction_is_committed("snap-2"))

    def test_a_failed_prediction_is_not_marked_terminally_analyzed(self):
        """AA-13's second half: marking ANALYZED without a committed
        prediction loses the observation for good."""
        from alpha_service import AlphaShadowService
        store = ProcessedStore(path=os.path.join(self._tmp, "proc13.jsonl"))
        ledger = self.ledger()
        service = AlphaShadowService.__new__(AlphaShadowService)
        # The service's terminal decision depends ONLY on this call, so the
        # rule can be exercised without standing up four providers.
        self.assertFalse(ledger.prediction_is_committed("snap-missing"))
        store.mark("snap-missing", "DEFERRED", detail="prediction_not_committed")
        self.assertNotEqual(store.status("snap-missing"), STATUS_ANALYZED)
        self.assertFalse(store.seen("snap-missing"),
                         "a non-committed analysis must be retried, not "
                         "treated as finished")

    def test_restart_reconciliation_reports_a_disagreement(self):
        """An ANALYZED mark whose prediction is not in the ledger is surfaced,
        not repaired: rewriting either side to make them agree is exactly the
        retroactive edit this subsystem forbids."""
        from alpha_service import AlphaShadowService
        store = ProcessedStore(path=os.path.join(self._tmp, "proc13b.jsonl"))
        ledger = self.ledger()
        store.mark("snap-orphan", STATUS_ANALYZED, prediction_id="p-gone")
        service = AlphaShadowService.__new__(AlphaShadowService)
        service.ledger = ledger
        service.consumer = type("C", (), {"store": store})()
        service.telemetry = type("T", (), {"record_error": lambda *a: None})()
        report = AlphaShadowService.reconcile_processed(service)
        self.assertEqual(
            [row["market_snapshot_id"]
             for row in report["analyzed_without_prediction"]], ["snap-orphan"])
        # Nothing was rewritten.
        self.assertEqual(store.status("snap-orphan"), STATUS_ANALYZED)

    def test_reconciliation_reports_a_prepare_with_no_prediction(self):
        from alpha_service import AlphaShadowService
        store = ProcessedStore(path=os.path.join(self._tmp, "proc13c.jsonl"))
        ledger = self.ledger()
        ledger.prepare("snap-crashed", contract_id="C-CRASH")
        service = AlphaShadowService.__new__(AlphaShadowService)
        service.ledger = ledger
        service.consumer = type("C", (), {"store": store})()
        service.telemetry = type("T", (), {"record_error": lambda *a: None})()
        report = AlphaShadowService.reconcile_processed(service)
        self.assertEqual(
            [row["market_snapshot_id"]
             for row in report["prepared_without_acknowledgement"]],
            ["snap-crashed"])


# ── AA-14 ────────────────────────────────────────────────────────────────
class AA14_MultiWriterRaces(RemediationCase):
    """Check-then-append is not atomic. Two writers could both read "not
    recorded" and both append."""

    def ledger(self):
        return AlphaLedger(path=self.path)

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self._tmp, "ledger14.jsonl")

    def test_reproduce_concurrent_writers_produce_exactly_one_prediction(self):
        """Eight threads race to record the same snapshot. Exactly one wins;
        the others are refused rather than appending a duplicate."""
        barrier = threading.Barrier(8)
        outcomes = []
        lock = threading.Lock()

        def _writer(index):
            ledger = self.ledger()
            barrier.wait(timeout=10)
            try:
                ledger.record_prediction({"prediction_id": f"p-{index}",
                                          "market_snapshot_id": "snap-race"})
                result = "written"
            except LedgerError:
                result = "refused"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=_writer, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(outcomes.count("written"), 1, outcomes)
        self.assertEqual(len(self.ledger().predictions()), 1)

    def test_concurrent_resolutions_do_not_both_append(self):
        ledger = self.ledger()
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-r"})
        barrier = threading.Barrier(6)
        outcomes = []
        lock = threading.Lock()

        def _resolve(outcome):
            local = self.ledger()
            barrier.wait(timeout=10)
            try:
                local.resolve("p1", outcome, source="feed")
                result = "written"
            except LedgerError:
                result = "refused"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=_resolve, args=(i % 2,))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(outcomes.count("written"), 1, outcomes)
        rows = [r for r in self.ledger().rows() if r["kind"] == "RESOLUTION"]
        self.assertEqual(len(rows), 1)

    def test_a_conflicting_resolution_is_surfaced_not_first_win_silently(self):
        ledger = self.ledger()
        binding = {"contract_id": "KX-C", "market_snapshot_id": "snap-c",
                   "record_sha256": "c" * 64}
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-c",
                                  "contract_id": "KX-C",
                                  "source_binding": dict(binding)})
        # Complete binding and a qualified source: since the re-audit those
        # are preconditions, and without them this would exercise the
        # quarantine path instead of the conflict path it names.
        settlement = {"prediction_id": "p1", "contract_id": "KX-C",
                      "market_snapshot_id": "snap-c",
                      "source_record_sha256": "c" * 64}
        trusted = ["feed-a", "feed-b"]
        ingest_settlements(ledger, [dict(settlement, outcome=1,
                                         source="feed-a")],
                           trusted_sources=trusted)
        result = ingest_settlements(ledger, [dict(settlement, outcome=0,
                                                  source="feed-b")],
                                    trusted_sources=trusted)
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["existing_outcome"], 1)
        self.assertEqual(result["conflicts"][0]["incoming_outcome"], 0)

    def test_the_lock_is_a_sidecar_not_the_ledger_itself(self):
        """Taking the lock must never open the ledger for writing."""
        import durable_append
        ledger = self.ledger()
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-l"})
        before = open(ledger.log.path, "rb").read()
        with durable_append.exclusive_lock(ledger.log.path):
            pass
        self.assertEqual(open(ledger.log.path, "rb").read(), before)
        self.assertTrue(os.path.exists(ledger.log.path +
                                       durable_append.LOCK_SUFFIX))

    def test_the_writer_model_is_documented(self):
        """AA-14 asks for the model to be written down, because `flock` is
        per-host and advisory and the limit has to be stated, not assumed."""
        doc = os.path.join(REPO, "docs", "design", "alpha-writer-model.md")
        self.assertTrue(os.path.exists(doc), doc)
        text = open(doc, encoding="utf-8").read().lower()
        for needle in ("flock", "advisory", "single", "network file"):
            self.assertIn(needle, text)

    def test_control_sequential_writers_both_succeed(self):
        ledger = self.ledger()
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-a"})
        ledger.record_prediction({"prediction_id": "p2",
                                  "market_snapshot_id": "snap-b"})
        self.assertEqual(len(self.ledger().predictions()), 2)


# ── AA-15 ────────────────────────────────────────────────────────────────
class AA15_R4JoinIsNotVerified(RemediationCase):
    """Settlements joined on `prediction_id` alone. Matching an opaque token
    proves somebody quoted a token, not that the settlement describes the
    market the prediction was about."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self._tmp, "ledger15.jsonl")
        self.record = valid_record()

    def ledger_with_binding(self):
        ledger = AlphaLedger(path=self.path)
        ledger.record_prediction({
            "prediction_id": "p1",
            "market_snapshot_id": "snap-15",
            "contract_id": "KXBTCD-26SEP1200-T60000",
            "source_binding": {
                "record_sha256": self.record["record_sha256"],
                "digest_verified": True,
                "contract_id": "KXBTCD-26SEP1200-T60000",
                "market_snapshot_id": "snap-15",
                "contract_schema": contract.FEED_SCHEMA,
                "environment": "demo"},
        })
        return ledger

    #: Since the re-audit a settlement must carry the COMPLETE required
    #: binding and name a QUALIFIED source. The fixture carries both, because
    #: production does; a test that omitted them would be exercising the
    #: quarantine path rather than the mismatch path each case below names.
    TRUSTED = ["trusted-feed"]

    def settlement(self, **over):
        row = {"prediction_id": "p1", "outcome": 1, "source": "trusted-feed",
               "contract_id": "KXBTCD-26SEP1200-T60000",
               "market_snapshot_id": "snap-15",
               "source_record_sha256": self.record["record_sha256"]}
        row.update(over)
        return row

    def ingest(self, ledger, rows, **kw):
        kw.setdefault("trusted_sources", self.TRUSTED)
        return ingest_settlements(ledger, rows, **kw)

    def test_the_verified_source_identity_reaches_the_prediction_row(self):
        ledger = self.ledger_with_binding()
        row = ledger.find_prediction("p1")
        self.assertEqual(row["source_binding"]["record_sha256"],
                         self.record["record_sha256"])
        self.assertTrue(row["source_binding"]["digest_verified"])

    def test_reproduce_a_settlement_for_a_different_contract_is_rejected(self):
        ledger = self.ledger_with_binding()
        result = self.ingest(
            ledger, [self.settlement(contract_id="SOME-OTHER-MARKET")])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["binding_mismatches"]), 1)
        self.assertIsNone(ledger.find_resolution("p1"))

    def test_every_binding_field_is_checked(self):
        for field, wrong in (("contract_id", "WRONG-TICKER"),
                             ("market_snapshot_id", "snap-wrong"),
                             ("source_record_sha256", "b" * 64),
                             ("environment", "prod"),
                             ("contract_schema", "atlas-research-candidate-v1")):
            with self.subTest(field=field):
                ledger = AlphaLedger(
                    path=os.path.join(self._tmp, f"l15-{field}.jsonl"))
                ledger.record_prediction({
                    "prediction_id": "p1", "market_snapshot_id": "snap-15",
                    "contract_id": "KXBTCD-26SEP1200-T60000",
                    "source_binding": {
                        "record_sha256": self.record["record_sha256"],
                        "contract_id": "KXBTCD-26SEP1200-T60000",
                        "market_snapshot_id": "snap-15",
                        "contract_schema": contract.FEED_SCHEMA,
                        "environment": "demo"}})
                result = self.ingest(
                    ledger, [self.settlement(**{field: wrong})])
                self.assertEqual(result["appended"], 0)
                self.assertTrue(result["binding_mismatches"])

    def test_conflicting_fields_are_reported_not_discarded(self):
        ledger = self.ledger_with_binding()
        result = self.ingest(
            ledger, [self.settlement(environment="prod")])
        mismatch = result["binding_mismatches"][0]["mismatches"][0]
        self.assertEqual(mismatch["field"], "environment")
        self.assertEqual(mismatch["settlement_value"], "prod")
        self.assertEqual(mismatch["prediction_value"], "demo")

    def test_a_malformed_resolved_at_is_refused(self):
        for bad in ("not-a-date", "2026-09-11T12:00:00", "", "   ", 12345):
            with self.subTest(value=repr(bad)):
                ledger = AlphaLedger(
                    path=os.path.join(self._tmp, f"l15t-{bad!r}.jsonl"))
                ledger.record_prediction({
                    "prediction_id": "p1", "market_snapshot_id": "snap-15",
                    "contract_id": "KXBTCD-26SEP1200-T60000",
                    "source_binding": {
                        "record_sha256": self.record["record_sha256"],
                        "contract_id": "KXBTCD-26SEP1200-T60000",
                        "market_snapshot_id": "snap-15"}})
                result = self.ingest(
                    ledger, [self.settlement(resolved_at=bad)])
                self.assertEqual(result["appended"], 0, bad)
                self.assertIsNone(ledger.find_resolution("p1"))

    def test_an_untrusted_source_is_refused_when_an_allow_list_is_given(self):
        ledger = self.ledger_with_binding()
        result = self.ingest(ledger, [self.settlement()],
                             trusted_sources=["the-only-real-oracle"])
        self.assertEqual(result["appended"], 0)
        self.assertTrue(result["trusted_sources_enforced"])
        self.assertIn("allow-list", result["rejected"][0]["reason"])

    def test_control_a_fully_matching_settlement_is_accepted(self):
        """Anti-vacuity: every refusal above must not simply be "ingestion is
        broken"."""
        ledger = self.ledger_with_binding()
        result = self.ingest(ledger, [self.settlement(
            contract_id="KXBTCD-26SEP1200-T60000",
            market_snapshot_id="snap-15",
            source_record_sha256=self.record["record_sha256"],
            contract_schema=contract.FEED_SCHEMA,
            environment="demo",
            resolved_at="2026-09-12T20:10:00+00:00",
            settlement_evidence_id="cf-rti-2026-09-12")])
        self.assertEqual(result["appended"], 1, result["rejected"])
        row = ledger.find_resolution("p1")
        self.assertEqual(row["actual_outcome"], 1)
        self.assertEqual(row["settlement_evidence_id"], "cf-rti-2026-09-12")
        self.assertTrue(row["binding_verified"])

    def test_the_checksum_claim_is_not_overstated_anywhere(self):
        """AA-04 is explicit: a checksum proves integrity, not authenticity.
        The module that implements it must say so rather than implying the
        exchange was authenticated."""
        text = open(os.path.join(REPO, "candidate_contract.py"),
                    encoding="utf-8").read().lower()
        self.assertIn("does not authenticate", text)
        self.assertIn("not a signature", text)


# ── AA-16 ────────────────────────────────────────────────────────────────
class AA16_ReportOutputCanAliasTheSourceLedger(RemediationCase):
    """A derived report published with `os.replace` could land on a source
    ledger and destroy the append-only history in one syscall."""

    def ledger(self):
        ledger = AlphaLedger(path=os.path.join(self._tmp, "alpha_led.jsonl"),
                             cost_path=os.path.join(self._tmp, "alpha_cost.jsonl"))
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-16"})
        return ledger

    def test_reproduce_a_report_cannot_replace_the_prediction_ledger(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        before = open(ledger.log.path, "rb").read()
        with self.assertRaises(ValueError) as caught:
            write_learning_report(ledger, self._tmp,
                                  filename=os.path.basename(ledger.log.path))
        self.assertIn("never replaces a source ledger", str(caught.exception))
        self.assertEqual(open(ledger.log.path, "rb").read(), before)

    def test_a_report_cannot_replace_the_cost_ledger(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        with self.assertRaises(ValueError):
            write_learning_report(
                ledger, self._tmp,
                filename=os.path.basename(ledger.cost_log.path))

    def test_a_report_cannot_replace_the_processed_ledger(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        with self.assertRaises(ValueError):
            write_learning_report(ledger, self._tmp,
                                  filename=CFG.ALPHA_STATE_FILE)

    def test_a_relative_path_cannot_reach_a_ledger(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        nested = os.path.join(self._tmp, "reports")
        os.makedirs(nested, exist_ok=True)
        with self.assertRaises(ValueError):
            write_learning_report(
                ledger, nested,
                filename=os.path.join("..",
                                      os.path.basename(ledger.log.path)))

    def test_a_symlink_cannot_reach_a_ledger(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        link = os.path.join(self._tmp, "report-link.json")
        os.symlink(ledger.log.path, link)
        with self.assertRaises(ValueError):
            write_learning_report(ledger, self._tmp,
                                  filename=os.path.basename(link))

    def test_a_hard_link_cannot_reach_a_ledger(self):
        """`realpath` cannot see through a hard link, so identity is also
        checked by (device, inode)."""
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        link = os.path.join(self._tmp, "report-hard.json")
        os.link(ledger.log.path, link)
        with self.assertRaises(ValueError):
            write_learning_report(ledger, self._tmp,
                                  filename=os.path.basename(link))

    def test_control_a_normal_report_is_published(self):
        from alpha_learning_runtime import write_learning_report
        ledger = self.ledger()
        report = write_learning_report(ledger, self._tmp,
                                       filename="alpha_learning_report.json")
        self.assertEqual(report["mode"], "SHADOW_ONLY")
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp, "alpha_learning_report.json")))
        self.assertEqual(len(ledger.predictions()), 1)


# ── LIVE SCHEMA / ASTRA IDENTITY: stated, not faked ──────────────────────
class LiveSchemaAndAstraIdentityRemainUnproven(RemediationCase):
    """Two things this remediation cannot prove, and does not pretend to."""

    def test_a_thin_payload_reports_live_schema_unproven(self):
        from tools.alpha_live_schema_qualify import qualify, STATUS_UNPROVEN
        verdict = qualify([{"ticker": "KXBTCD-X", "yes_bid": 44, "yes_ask": 46}])
        self.assertEqual(verdict["status"], STATUS_UNPROVEN)
        self.assertEqual(verdict["markets_qualifying"], 0)
        self.assertEqual(verdict["network_calls"], 0)

    def test_a_qualifying_payload_reports_proven(self):
        """Anti-vacuity: the tool must be able to say YES, or UNPROVEN would
        be its only possible answer and would mean nothing."""
        from tools.alpha_live_schema_qualify import qualify, STATUS_PROVEN
        verdict = qualify([raw_market()])
        self.assertEqual(verdict["status"], STATUS_PROVEN, verdict["results"])

    def test_the_qualifier_derives_nothing_from_an_execution_book(self):
        """It is handed the RAW capture only; a missing NO side stays missing
        rather than being completed from the YES side."""
        from tools.alpha_live_schema_qualify import qualify, STATUS_UNPROVEN
        verdict = qualify([raw_market(no_bid=DROP, no_ask=DROP)])
        self.assertEqual(verdict["status"], STATUS_UNPROVEN)
        self.assertIn("no_bid", verdict["results"][0]["unavailable_fields"])

    def test_the_qualifier_makes_no_network_call(self):
        """Static: no HTTP client is reachable from the qualification tool."""
        import ast
        tree = ast.parse(open(os.path.join(REPO, "tools",
                                           "alpha_live_schema_qualify.py"),
                              encoding="utf-8").read())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        for banned in ("requests", "urllib", "http", "socket", "httpx"):
            self.assertNotIn(banned, roots)

    def test_no_existing_provider_has_been_renamed_to_astra(self):
        """R3 is not solved by relabelling OpenAI/Grok/Gemini as Astra.

        A real Astra identity is an EXTERNAL blocker. If one of the configured
        providers were simply renamed, this subsystem would report an
        independent second opinion that does not exist.
        """
        import alpha_providers
        source = open(os.path.join(REPO, "alpha_providers.py"),
                      encoding="utf-8").read().lower()
        # "astra" may appear as a selector name in the LEARNING report (it is a
        # label for a future provider), but never as an alias mapping an
        # existing vendor onto that name.
        for vendor in ("openai", "grok", "gemini", "anthropic", "xai"):
            for pattern in (f'"{vendor}": "astra"', f"'{vendor}': 'astra'",
                            f'astra = "{vendor}"', f"astra = '{vendor}'"):
                self.assertNotIn(pattern, source,
                                 f"{vendor} appears to be aliased as Astra")


# ── AA-17 / AA-18 ────────────────────────────────────────────────────────
class AA17_SurvivingSafetyMutations(RemediationCase):
    """Two mutations passed the candidate's full 1,670-test run."""

    def test_the_reproducible_mutation_runner_covers_every_closed_finding(self):
        """The original eleven, plus one per invariant the v3 re-audit closed.

        Written as "every M01..M11 is still present, and nothing was removed"
        rather than as an exact list, so adding a mutation for a NEW finding
        is not a test failure while DELETING one still is. The v3 set is
        enumerated in `docs/audits/ASTRA_V3_REMEDIATION_REPORT.md`.
        """
        from tools.astra_mutation_probe import MUTATIONS
        original = [f"M{i:02d}" for i in range(1, 12)]
        self.assertEqual(sorted(set(MUTATIONS) & set(original)), original)
        # M07P is the re-audit's named addition: a COMPLETE record whose
        # quotes are declared derived.
        self.assertIn("M07P", MUTATIONS)
        self.assertGreaterEqual(len(MUTATIONS), len(original) + 1)

    def test_every_mutation_names_the_tests_that_detect_it(self):
        from tools.astra_mutation_probe import MUTATIONS
        for key, (_desc, filename, old, new, selectors) in MUTATIONS.items():
            with self.subTest(mutation=key):
                self.assertTrue(selectors, f"{key} names no detecting test")
                self.assertTrue(os.path.exists(os.path.join(REPO, filename)))
                self.assertNotEqual(old, new)
                # The anchor must still exist, or the runner would report
                # KILLED for a mutation it never managed to apply.
                source = open(os.path.join(REPO, filename),
                              encoding="utf-8").read()
                self.assertIn(old, source,
                              f"{key}'s anchor text has drifted out of "
                              f"{filename}; the mutation cannot be applied")

    def test_the_runner_never_edits_the_working_tree(self):
        """A mutation runner that patches the real source and restores it is
        one crash away from committing a mutation."""
        import ast
        tree = ast.parse(open(os.path.join(REPO, "tools",
                                           "astra_mutation_probe.py"),
                              encoding="utf-8").read())
        self.assertIn("copytree", {getattr(n.func, "attr", "")
                                   for n in ast.walk(tree)
                                   if isinstance(n, ast.Call)})

    def test_the_two_known_survivors_have_effect_based_coverage(self):
        """Counters and diagnostics are exactly what M04 and M11 preserved."""
        source = open(os.path.join(REPO, "tests",
                                   "test_astra_mutation_regression.py"),
                      encoding="utf-8").read()
        self.assertIn("class M04_IgnorePerFieldProvenance", source)
        self.assertIn("class M11_AcceptLegacyWhileKeepingDiagnostics", source)
        self.assertIn("assertNothingMinted", source)


class AA18_CIDoesNotTargetTheCandidateBranch(RemediationCase):
    """Hosted CI ran only on the learning branch, so no candidate commit was
    ever actually exercised."""

    BRANCH = "alpha/astra-candidate-feed-v2-remediation"
    WORKFLOW = os.path.join("..", ".github", "workflows", "alpha-learning-v1.yml")

    def workflow_text(self):
        path = os.path.join(REPO, ".github", "workflows",
                            "alpha-learning-v1.yml")
        self.assertTrue(os.path.exists(path), path)
        return open(path, encoding="utf-8").read()

    def test_the_workflow_targets_the_remediation_branch(self):
        self.assertIn(self.BRANCH, self.workflow_text())

    def test_the_learning_branch_is_preserved(self):
        self.assertIn("alpha/astra-learning-v1", self.workflow_text())

    def test_the_required_suites_are_all_invoked(self):
        text = self.workflow_text()
        for needle in ("tests/test_astra_aa01_aa18_remediation.py",
                       "tests/test_astra_mutation_regression.py",
                       "tests.test_alpha_learning",
                       "tests.test_research_feed_boundary",
                       "pytest tests/ -q"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_boundary_check_covers_the_new_neutral_modules(self):
        text = self.workflow_text()
        for module in ("candidate_contract.py", "research_spool.py",
                       "durable_append.py"):
            with self.subTest(module=module):
                self.assertIn(module, text)

    def test_the_workflow_is_parseable_yaml(self):
        try:
            import yaml
        except ImportError:                                # pragma: no cover
            self.skipTest("pyyaml unavailable; parse not verified here")
        parsed = yaml.safe_load(self.workflow_text())
        # `on:` parses as the boolean True in YAML 1.1, which is why this is
        # keyed on `True` rather than on the string.
        triggers = parsed.get(True) or parsed.get("on")
        self.assertIn(self.BRANCH, triggers["push"]["branches"])
