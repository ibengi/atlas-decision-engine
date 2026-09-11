# -*- coding: utf-8 -*-
"""AA-17: the mutations that survived the candidate's 1,670-test run.

Astra found two safety mutations that the whole suite failed to notice:

    M04  ignore per-field provenance                   -> 1,670 tests passed
    M11  accept legacy v1 while preserving diagnostics -> 1,670 tests passed

Both survived for the same reason: the suite asserted COUNTERS and SHAPES.
`stats["unattributed"] == 1` is satisfied by a component that increments a
counter and then processes the record anyway, and "the diagnostic mentions
legacy" is satisfied by a component that logs and then mints.

So every case here asserts an EFFECT at the end of the chain:

    no snapshot minted  AND  no prediction row appended

which is the only thing that actually matters. A mutation that keeps the
counters and the log lines intact but lets the record through still fails
here, because the ledger is empty either way and the ledger is what the
calibration numbers are computed from.

The reproducible mutation runner lives in `tools/astra_mutation_probe.py`;
these tests are its in-suite counterpart so CI catches a regression without
having to patch source files.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                   # noqa: E402
from _candidate import DROP, EXECUTION_BOOK, raw_market, valid_record  # noqa: E402

import candidate_contract as contract                          # noqa: E402
from alpha_consumer import LocalSpoolSource, ProcessedStore, SpoolConsumer  # noqa: E402
from alpha_ledger import AlphaLedger                            # noqa: E402
from config import CFG                                          # noqa: E402
from research_feed import ResearchFeed, candidate_from_market    # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EffectCase(AlphaCase):
    """Runs a record all the way to the ledger and asserts what ARRIVED."""

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()
        self.spool = os.path.join(self._tmp, "spool")
        os.makedirs(self.spool, exist_ok=True)
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))

    def place(self, record, name="r.json"):
        with open(os.path.join(self.spool, name), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def consume(self):
        consumer = SpoolConsumer(source=LocalSpoolSource(self.spool),
                                 store=ProcessedStore(
                                     path=os.path.join(self._tmp, "p.jsonl")))
        return consumer, consumer.pending()

    def assertNothingMinted(self, pending, why):
        self.assertEqual(pending, [], f"a snapshot was minted {why}")
        self.assertEqual(self.ledger.predictions(), [],
                         f"a prediction was appended {why}")

    def assertMinted(self, pending):
        """The anti-vacuity control for every case in this file."""
        self.assertEqual(len(pending), 1,
                         "the control record was not minted; the refusals "
                         "above may be passing for an unrelated reason")


class M04_IgnorePerFieldProvenance(EffectCase):
    """SURVIVED in the rejected candidate. Asserted by effect now."""

    def test_a_record_with_no_provenance_container_mints_nothing(self):
        record = valid_record()
        record.pop("field_provenance")
        record["record_sha256"] = contract.compute_checksum(record)
        self.place(record)
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "without any provenance")

    def test_a_record_with_empty_provenance_mints_nothing(self):
        record = valid_record()
        record["field_provenance"] = {}
        record["record_sha256"] = contract.compute_checksum(record)
        self.place(record)
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "with empty provenance")

    def test_a_record_with_false_provenance_mints_nothing(self):
        """`yes_ask` claiming to have been read from the market TITLE. This is
        the mutation in its most literal form: the field is populated, the
        provenance is a non-empty string, and it is a lie."""
        record = valid_record()
        record["field_provenance"]["yes_ask"] = "market.title"
        record["record_sha256"] = contract.compute_checksum(record)
        self.place(record)
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "with false provenance")

    def test_provenance_for_one_field_missing_mints_nothing(self):
        for field in contract.REQUIRED_FIELDS:
            with self.subTest(field=field):
                self.setUp()
                record = valid_record()
                record["field_provenance"].pop(field, None)
                record["record_sha256"] = contract.compute_checksum(record)
                self.place(record)
                _consumer, pending = self.consume()
                self.assertNothingMinted(pending, f"without {field} provenance")

    def test_control_the_same_record_with_provenance_is_minted(self):
        self.place(valid_record())
        _consumer, pending = self.consume()
        self.assertMinted(pending)


class M11_AcceptLegacyWhileKeepingDiagnostics(EffectCase):
    """SURVIVED in the rejected candidate: the suite checked that the legacy
    diagnostic was emitted, not that the record was refused."""

    def legacy(self, schema):
        record = valid_record()
        record["schema"] = schema
        record["record_sha256"] = contract.compute_checksum(record)
        return record

    def test_a_standalone_legacy_v1_record_mints_nothing(self):
        self.place(self.legacy("atlas-research-candidate-v1"))
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "from a legacy v1 record")

    def test_a_standalone_legacy_v2_record_mints_nothing(self):
        self.place(self.legacy("atlas-research-candidate-v2"))
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "from a legacy v2 record")

    def test_the_diagnostic_is_emitted_AND_the_record_is_refused(self):
        """The mutation's exact shape: keeping the log line is not enough."""
        self.place(self.legacy("atlas-research-candidate-v1"))
        consumer = SpoolConsumer(
            source=LocalSpoolSource(self.spool),
            store=ProcessedStore(path=os.path.join(self._tmp, "p.jsonl")))
        with self.assertLogs("ALPHA", level="WARNING") as logs:
            pending = consumer.pending()
        self.assertIn("legacy", "\n".join(logs.output).lower())
        self.assertEqual(consumer.stats["legacy_schema"], 1)
        self.assertNothingMinted(pending, "despite the legacy diagnostic")

    def test_a_legacy_record_beside_a_good_one_does_not_mint(self):
        """A mixed batch: the good record must mint, the legacy one must not.
        Without this, "nothing minted" could mean the batch failed entirely."""
        self.place(self.legacy("atlas-research-candidate-v1"), "a-legacy.json")
        self.place(valid_record(raw_market(ticker="KX-GOOD")), "b-good.json")
        _consumer, pending = self.consume()
        self.assertEqual([s.contract_id for s, _ in pending], ["KX-GOOD"])

    def test_control_the_current_schema_is_minted(self):
        self.place(valid_record())
        _consumer, pending = self.consume()
        self.assertMinted(pending)


class M01_M03_SubstitutedMarketFacts(EffectCase):
    """M01 (`"kalshi"` as a settlement source), M02 (`0.0` as a volume) and
    M03 (close time as the resolution time) were the original inventions. Each
    is asserted by effect, at the end of the chain."""

    def emit_and_consume(self, market_payload):
        feed = ResearchFeed(directory=self.spool, start_writer=False)
        record = feed._build(candidate_from_market(
            market_payload, EXECUTION_BOOK, raw_book=market_payload))
        if record is not None:
            self.place(record)
        return feed, self.consume()[1]

    def test_m01_an_absent_settlement_source_mints_nothing(self):
        feed, pending = self.emit_and_consume(raw_market(settlement_sources=DROP))
        self.assertNothingMinted(pending, "with no observed settlement source")
        self.assertGreater(feed.refused_incomplete, 0)

    def test_m02_an_absent_volume_mints_nothing(self):
        _feed, pending = self.emit_and_consume(raw_market(volume=DROP))
        self.assertNothingMinted(pending, "with no observed volume")

    def test_m02_an_absent_open_interest_mints_nothing(self):
        _feed, pending = self.emit_and_consume(raw_market(open_interest=DROP))
        self.assertNothingMinted(pending, "with no observed open interest")

    def test_m03_a_close_time_is_never_used_as_the_resolution_time(self):
        payload = raw_market(expiration_time=DROP)
        _feed, pending = self.emit_and_consume(payload)
        self.assertNothingMinted(pending, "using the close time as the "
                                          "resolution time")
        # ...and the close time is genuinely present, so the refusal is about
        # the SUBSTITUTION being refused, not about an empty payload.
        self.assertTrue(payload["close_time"])

    def test_m07_a_missing_book_side_mints_nothing(self):
        for side in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            with self.subTest(side=side):
                self.setUp()
                _feed, pending = self.emit_and_consume(
                    raw_market(**{side: DROP}))
                self.assertNothingMinted(pending, f"without {side}")

    def test_control_a_complete_observation_mints(self):
        _feed, pending = self.emit_and_consume(raw_market())
        self.assertMinted(pending)


class M05_ProvenanceExcludedFromTheChecksum(EffectCase):
    """If provenance were outside the digest, it could be rewritten after the
    fact and the record would still verify."""

    def test_editing_provenance_invalidates_the_record(self):
        record = valid_record()
        record["field_provenance"]["question"] = "market.ticker"
        self.place(record)                      # digest NOT recomputed
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "after provenance was edited")

    def test_editing_the_quote_verdict_invalidates_the_record(self):
        record = valid_record()
        record["quote_observation"]["no_bid"] = contract.QUOTE_DERIVED
        self.place(record)
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "after the quote verdict was edited")

    def test_editing_unavailable_fields_invalidates_the_record(self):
        record = valid_record()
        record["unavailable_fields"] = []
        self.place(record)
        _consumer, pending = self.consume()
        self.assertNothingMinted(pending, "after unavailable_fields was edited")

    def test_the_canonical_content_covers_every_field_but_the_digest(self):
        record = valid_record()
        covered = set(contract.canonical_content(record))
        self.assertNotIn("record_sha256", covered)
        for field in ("field_provenance", "quote_observation",
                      "unavailable_fields", "emitted_at_utc"):
            self.assertIn(field, covered)


class M09_ProducerExceptionPropagation(EffectCase):
    """A research failure must never reach the decision cycle."""

    def test_a_producer_bug_never_raises_into_the_caller(self):
        feed = ResearchFeed(directory=self.spool, start_writer=False)
        with patch.object(ResearchFeed, "_build",
                          side_effect=RuntimeError("boom")):
            self.assertFalse(feed.emit_candidate({"anything": True}))

    def test_a_malformed_candidate_never_raises(self):
        feed = ResearchFeed(directory=self.spool, start_writer=False)
        for payload in (None, [], "", 7, {"ticker": object()}, {"volume": []}):
            with self.subTest(payload=repr(payload)[:30]):
                self.assertFalse(feed.emit_candidate(payload))

    def test_a_checksum_failure_never_raises(self):
        feed = ResearchFeed(directory=self.spool, start_writer=False)
        with patch("research_feed.compute_checksum",
                   side_effect=TypeError("unserializable")):
            self.assertFalse(feed.emit_candidate({
                "field_provenance": {}, "unavailable_fields": [],
                "quote_observation": {}}))


class M10_OverwriteHistoricalBytes(EffectCase):
    """History is append-only. Nothing may rewrite or truncate it."""

    def test_appending_never_rewrites_earlier_bytes(self):
        ledger = self.ledger
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-1"})
        first = open(ledger.log.path, "rb").read()
        ledger.record_prediction({"prediction_id": "p2",
                                  "market_snapshot_id": "snap-2"})
        self.assertTrue(open(ledger.log.path, "rb").read().startswith(first))

    def test_a_resolution_never_edits_the_prediction_row(self):
        ledger = self.ledger
        ledger.record_prediction({"prediction_id": "p1",
                                  "market_snapshot_id": "snap-1",
                                  "p_yes": 0.61})
        ledger.resolve("p1", 1, source="feed")
        rows = [r for r in ledger.rows() if r["kind"] == "PREDICTION"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["p_yes"], 0.61)
        self.assertNotIn("actual_outcome", rows[0])

    def test_the_ledger_only_ever_opens_for_append(self):
        """Static: no O_TRUNC, no truncate, no write-seek in the ledger writer.

        Inspects the NODES rather than a dump of the whole tree: the module's
        own docstring explains why it never truncates, and a substring search
        over the dump matches that prose and passes for the wrong reason.
        """
        import ast
        tree = ast.parse(open(os.path.join(REPO, "durable_append.py"),
                              encoding="utf-8").read())
        flags, calls = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("O_"):
                    flags.add(node.attr)
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name:
                    calls.add(name)
        self.assertIn("O_APPEND", flags)
        self.assertNotIn("O_TRUNC", flags)
        self.assertNotIn("truncate", calls)
        self.assertNotIn("ftruncate", calls)
        # Scoped to `append_line`, the only function that opens the LEDGER for
        # writing. `exclusive_lock` also opens a descriptor, but on the sidecar
        # lock file, which is not history and is deliberately not append-only.
        append_line = next(n for n in ast.walk(tree)
                           if isinstance(n, ast.FunctionDef)
                           and n.name == "append_line")
        writable_opens = [
            n for n in ast.walk(append_line)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "open"
            and any("O_WRONLY" in ast.dump(a) or "O_RDWR" in ast.dump(a)
                    for a in n.args)]
        self.assertTrue(writable_opens, "no writable open found to check")
        for node in writable_opens:
            self.assertIn("O_APPEND", ast.dump(node))
        # And nothing in that function seeks: an append-only writer that can
        # seek is an append-only writer only by convention.
        self.assertNotIn("seek", {getattr(n.func, "attr", "")
                                  for n in ast.walk(append_line)
                                  if isinstance(n, ast.Call)})


if __name__ == "__main__":
    unittest.main()
