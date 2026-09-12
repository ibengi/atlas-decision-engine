"""V4-RA-01..04 originals and independent v5 neighboring counterexamples.

All payloads and files are synthetic. The engine hook is called unbound on a
holder, never constructing an engine, broker, account or provider.
"""
import errno
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
from _candidate import EXECUTION_BOOK, raw_market, valid_candidate
from config import CFG
import research_feed as rf
import research_spool as rs


class V5ProducerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-v5-producer-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(CFG, "DATA_DIR", self.temp.name).start()
        patch.object(CFG, "RESEARCH_FEED_ENABLED", True).start()
        self.feed = rf.ResearchFeed(
            directory=os.path.join(self.temp.name, "spool"), start_writer=False)
        self.addCleanup(self.feed.writer.stop)

    def records(self):
        directory = self.feed.directory
        if not os.path.isdir(directory):
            return []
        result = []
        for name in os.listdir(directory):
            if name.endswith(".json"):
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    result.append(json.load(f))
        return result

    def drain(self):
        self.feed.writer.start()
        self.assertTrue(self.feed.writer.drain(timeout=5))


class V5SourceSchema(V5ProducerCase):
    def test_new_records_retain_all_raw_alias_structures_inside_checksum(self):
        from source_identity import verify_settlement_source_evidence
        market = raw_market(
            settlement_sources=[{"name": "A", "url": "https://synthetic.invalid/a"}],
            settlement_source={"url": "https://synthetic.invalid/a", "name": "A"})
        record = self.feed._build(valid_candidate(market))
        evidence = record["settlement_source_evidence"]
        self.assertEqual(evidence["schema"], "atlas-settlement-source-v1")
        self.assertEqual(evidence["aliases"], {
            key: market[key] for key in ("settlement_sources", "settlement_source")})
        self.assertTrue(verify_settlement_source_evidence(record)["verified"])
        prior = record["record_sha256"]
        evidence["aliases"]["settlement_sources"][0]["url"] += "/changed"
        self.assertNotEqual(rf.compute_checksum(record), prior)

    def test_rehashed_malformed_source_evidence_refuses_consumer_and_readiness(self):
        import copy
        from alpha_consumer import SpoolConsumer, ProcessedStore
        from alpha_feed_readiness import assess_record
        from candidate_contract import validate_record
        base = self.feed._build(valid_candidate(raw_market(settlement_sources=[{"name": "A"}])))
        proofs = [
            {"schema": "unknown", "aliases": {"settlement_sources": [{"name": "A"}]}},
            {"schema": "atlas-settlement-source-v1", "aliases": {
                "settlement_sources": [{"name": "A", "extension": {"id": "B"}}]}},
            {"schema": "atlas-settlement-source-v1", "aliases": {
                "settlement_sources": [[{"name": "A"}]]}},
            {"schema": "atlas-settlement-source-v1", "aliases": {
                "settlement_sources": [{"name": "B"}]}},
            {"schema": "atlas-settlement-source-v1", "aliases": {
                "settlement_source": [{"name": "A"}]}},
            {"schema": "atlas-settlement-source-v1", "aliases": {
                "settlement_sources": [{"name": "A"}], "unknown_alias": "A"}},
        ]
        for index, proof in enumerate(proofs):
            record = copy.deepcopy(base)
            record["settlement_source_evidence"] = proof
            record["record_sha256"] = rf.compute_checksum(record)
            with self.subTest(proof=index):
                self.assertTrue(validate_record(record))
                self.assertFalse(assess_record(record)["ready"])
                consumer = SpoolConsumer(
                    source=SimpleNamespace(records=lambda: [record]),
                    store=ProcessedStore(os.path.join(self.temp.name, f"processed-{index}.jsonl")))
                self.assertEqual(consumer.pending(), [])
                self.assertEqual(consumer.stats["minted"], 0)

    def test_legacy_rendered_record_is_recoverable_but_cannot_prove_source_structure(self):
        from candidate_contract import validate_record
        from source_identity import verify_settlement_source_evidence
        record = self.feed._build(valid_candidate())
        record.pop("settlement_source_evidence")
        record["record_sha256"] = rf.compute_checksum(record)
        self.assertEqual(validate_record(record), [])
        self.assertFalse(verify_settlement_source_evidence(record)["verified"])

    def test_original_unknown_extensions_and_nested_members_cannot_publish(self):
        cases = [
            [{"name": "A", "unsupported": {"ref": "B"}}],
            [[{"name": "A"}]], [{"name": "A"}, []],
            [{"name": "A", "authority_id": "ONE"}],
            [{"name": "A", "authority_id": "TWO"}],
            [{"name": "A", "version": 1}],
            [{"name": "A", "url": {"extension": "nested"}}],
            [{"name": "A"}, None], [{"name": "A"}, True],
            ({"name": "A"},),
        ]
        for index, source in enumerate(cases):
            with self.subTest(source=index):
                candidate = valid_candidate(raw_market(
                    ticker=f"SYNTHETIC-{index}", settlement_sources=source))
                self.assertIsNone(self.feed._build(candidate))
                self.feed.emit_candidate(candidate)
        self.drain()
        self.assertEqual(self.records(), [])

    def test_original_alias_flattening_cannot_hide_unreadable_structure(self):
        pairs = [
            ([[{"name": "A"}]], [{"name": "A"}]),
            ([{"name": "A"}, []], [{"name": "A"}]),
            ([{"name": "A", "authority_id": "ONE"}],
             [{"name": "A", "authority_id": "TWO"}]),
        ]
        for left, right in pairs:
            with self.subTest(left=left):
                candidate = valid_candidate(raw_market(
                    settlement_sources=left, settlement_source=right))
                self.assertIsNone(self.feed._build(candidate))

    def test_supported_structural_identities_remain_distinct(self):
        sources = [
            [{"name": "A, B"}], [{"name": "A"}, {"name": "B"}],
            [{"name": "A", "url": "https://synthetic.invalid/one"}],
            [{"name": "A", "url": "https://synthetic.invalid/two"}],
            [{"name": "A | B"}], [{"name": "A <B>"}],
            [{"name": "A \\| B"}],
        ]
        identities = [rf.settlement_source_identity(value) for value in sources]
        renderings = [rf.render_settlement_source(value) for value in identities]
        self.assertEqual(len(set(identities)), len(sources))
        self.assertEqual(len(set(renderings)), len(sources))
        for source in sources:
            self.assertIsNotNone(self.feed._build(valid_candidate(
                raw_market(settlement_sources=source))))

    def test_nested_mutation_after_handoff_cannot_change_source_evidence(self):
        market = raw_market(settlement_sources=[{"name": "A", "url":
                                                "https://synthetic.invalid/a"}])
        self.assertTrue(self.feed.emit_market(market, EXECUTION_BOOK))
        market["settlement_sources"][0]["url"] = "https://synthetic.invalid/b"
        market["yes_bid"] = 1
        self.drain()
        record, = self.records()
        self.assertIn("synthetic.invalid/a", record["resolution_source"])
        self.assertEqual(record["yes_bid"], .44)


class V5ObserverIsolation(V5ProducerCase):
    def hook(self, market):
        from execution_engine import ExecutionEngine
        holder = SimpleNamespace(research_feed=self.feed)
        ExecutionEngine._shadow_observer(
            holder, SimpleNamespace(raw_market=market), EXECUTION_BOOK,
            SimpleNamespace(strategy="synthetic", decision_id="synthetic-1"))

    def bounded_call(self, callback):
        done = threading.Event()
        errors = []

        def run():
            try:
                callback()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(done.wait(.75), "research blocked the engine hook")
        thread.join(1)
        self.assertEqual(errors, [])

    def test_original_huge_alias_with_stalled_bot_handler_is_nonblocking(self):
        import execution_engine
        release = threading.Event()
        called = threading.Event()

        class Held(logging.Handler):
            def emit(self, record):
                called.set()
                release.wait(3)
        handler = Held()
        logger = execution_engine.log
        self.addCleanup(release.set)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, logger.level)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        market = raw_market(event_ticker=10**4400,
                            event_id="SYNTHETIC-CONTRADICTION")
        self.bounded_call(lambda: self.hook(market))
        self.assertFalse(called.is_set())
        self.assertEqual(self.records(), [])

    def test_source_normalization_and_failure_formatting_run_on_worker(self):
        release = threading.Event()
        entered = threading.Event()
        threads = []
        original = rf.candidate_from_market

        def held(*args, **kwargs):
            threads.append(threading.current_thread().name)
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        self.addCleanup(release.set)
        self.feed.writer.start()
        with patch.object(rf, "candidate_from_market", held):
            self.bounded_call(lambda: self.hook(raw_market()))
            self.assertTrue(entered.wait(1))
            for _ in range(5):
                self.bounded_call(lambda: self.hook(raw_market()))
            self.assertEqual(set(threads), {"atlas-research-writer"})
            release.set()
            self.assertTrue(self.feed.writer.drain(5))

    def test_contended_accounting_or_queue_mutex_drops_without_wait(self):
        for lock in (self.feed.writer._lock, self.feed.writer._queue.mutex):
            with self.subTest(lock=repr(lock)):
                lock.acquire()
                try:
                    self.bounded_call(lambda: self.hook(raw_market()))
                    self.bounded_call(lambda: self.feed.emit_candidate(
                        valid_candidate()))
                    self.bounded_call(lambda: self.feed._note(
                        logging.WARNING, "synthetic"))
                finally:
                    lock.release()
        self.assertEqual(self.feed.writer.stats["queued"], 0)
        self.assertGreaterEqual(self.feed.writer.stats["dropped_queue_busy"], 6)

    def test_expensive_conversion_hooks_are_never_called_during_admission(self):
        class Untrusted:
            def __repr__(self):
                raise AssertionError("repr must not be called")
            def __str__(self):
                raise AssertionError("str must not be called")
        for bad in (Untrusted(), "x" * 300000, 10**4400,
                    [None] * 3000):
            self.bounded_call(lambda: self.hook(raw_market(title=bad)))
        self.assertEqual(self.feed.writer.stats["queued"], 0)

    def test_observer_error_cannot_call_synchronous_logging(self):
        import execution_engine
        with patch.object(self.feed, "emit_market",
                          side_effect=RuntimeError("synthetic")), \
                patch.object(execution_engine.log, "debug") as diagnostic:
            self.hook(raw_market())
        diagnostic.assert_not_called()


class V5CapacityUncertainty(V5ProducerCase):
    def test_original_transient_directory_or_member_enoent_refuses(self):
        spool = self.feed.writer.spool
        spool.max_records = 1
        os.makedirs(spool.directory)
        original = os.path.join(spool.directory, "retained.json")
        with open(original, "wb") as f:
            f.write(b"{}")
        record = self.feed._build(valid_candidate())
        for operation in ("stat", "listdir"):
            real = getattr(rs.os, operation)

            def uncertain(path, *args, **kwargs):
                target = original if operation == "stat" else spool.directory
                if str(path) == target:
                    raise FileNotFoundError(errno.ENOENT, "synthetic uncertainty")
                return real(path, *args, **kwargs)
            with self.subTest(operation=operation), patch.object(
                    rs.os, operation, uncertain):
                self.assertFalse(spool.write(record))
            with open(original, "rb") as f:
                self.assertEqual(f.read(), b"{}")
            self.assertEqual(spool.capacity()["occupied"], 1)
        self.assertEqual(spool.stats["capacity_unknown"], 2)

    def test_missing_initial_directory_can_be_created_before_authoritative_scan(self):
        spool = self.feed.writer.spool
        self.assertFalse(os.path.exists(spool.directory))
        self.assertTrue(spool.write(self.feed._build(valid_candidate())))
        self.assertEqual(spool.capacity()["occupied"], 1)

    def test_missing_partial_metadata_never_disappears_from_byte_budget(self):
        spool = self.feed.writer.spool
        os.makedirs(spool.directory)
        partial = os.path.join(spool.directory, "synthetic.partial")
        with open(partial, "wb") as f:
            f.write(b"x" * 4000)
        real = rs.os.stat

        def uncertain(path, *args, **kwargs):
            if str(path) == partial:
                raise FileNotFoundError(errno.ENOENT, "synthetic uncertainty")
            return real(path, *args, **kwargs)
        with patch.object(rs.os, "stat", uncertain):
            self.assertFalse(spool.write(self.feed._build(valid_candidate())))
        self.assertEqual(spool.capacity()["partial_bytes"], 4000)
        self.assertEqual(spool.stats["written"], 0)

    def test_legacy_cleanup_cannot_delete_an_unowned_old_partial(self):
        spool = self.feed.writer.spool
        os.makedirs(spool.directory)
        unknown = os.path.join(spool.directory, "someone-elses.partial")
        owned = os.path.join(spool.directory, "crashed.999999999.partial")
        live = os.path.join(spool.directory, f"active.{os.getpid()}.partial")
        for path in (unknown, owned, live):
            with open(path, "wb") as f:
                f.write(b"retained evidence")
            os.utime(path, (1, 1))
        with patch.object(rs, "owner_is_alive",
                          side_effect=lambda pid: pid != 999999999):
            self.assertEqual(spool.recover_temp_files(), 1)
        self.assertFalse(os.path.exists(owned))
        for path in (unknown, live):
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"retained evidence")


if __name__ == "__main__":
    unittest.main()
