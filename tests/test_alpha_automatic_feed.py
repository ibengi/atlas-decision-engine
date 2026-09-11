# -*- coding: utf-8 -*-
"""Alpha Gateway phase 2, section 1 — the automatic candidate feed.

THE AUTHORITY BOUNDARY
    The producer (engine process) owns the spool: it writes and prunes.
    The consumer (Alpha service process) owns its own state file and never
    writes into the spool. That is not a promise about intent, it is a
    property of which paths each side opens for writing, and it is asserted
    here by comparing the spool's bytes before and after a full consume.

THE PRODUCER CANNOT HURT THE ENGINE
    `emit_candidate` never raises, never trips the persistence sentinel, and
    is bounded. A research feed that can propagate an exception into a
    decision cycle, or fill the volume the money path needs, is worse than
    no research feed.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                  # noqa: E402

import research_feed                                          # noqa: E402
from alpha_consumer import (STATUS_ANALYZED, ProcessedStore,   # noqa: E402
                            SpoolConsumer)
from config import CFG                                        # noqa: E402
from persistence import PersistenceSentinel                    # noqa: E402
from research_feed import (ResearchFeed, candidate_from_market,  # noqa: E402
                           spool_dir)


def market(ticker="KXBTCD-1", hours=4):
    now = datetime.now(timezone.utc)
    return {"ticker": ticker, "event_ticker": "EV",
            "title": "Will BTC be above 60000?",
            "rules_primary": "CF Benchmarks RTI",
            # The exchange publishes its settlement sources. The fixture
            # carries them because a real market payload does: the producer
            # refuses to invent one, so a fixture without them would be
            # testing an incomplete market, not a complete candidate.
            "settlement_sources": [{"name": "CF Benchmarks RTI"}],
            "volume": 1200, "open_interest": 3400,
            "close_time": (now + timedelta(hours=hours - 1)).isoformat(),
            "expiration_time": (now + timedelta(hours=hours)).isoformat(),
            # AA-01: the exchange publishes its quotes ON the market object,
            # and that RAW observation is the only book the research feed may
            # read. `BOOK` below stands for the EXECUTION-normalized structure.
            **BOOK}


BOOK = {"yes_bid": 44, "yes_ask": 46, "no_bid": 54, "no_ask": 56}


class FeedCase(AlphaCase):

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()
        self.feed = ResearchFeed()

    def emit(self, ticker="KXBTCD-1", **kw):
        """Emit and WAIT. AA-10 made the spool write asynchronous, so a test
        that reads the spool afterwards has to drain the writer explicitly;
        the engine never does, and must never."""
        payload = market(ticker, **kw)
        accepted = self.feed.emit_candidate(
            candidate_from_market(payload, BOOK, raw_book=payload,
                                  cycle_id="c1"))
        self.feed.writer.drain(timeout=5.0)
        return accepted

    def spool_bytes(self) -> dict:
        directory = spool_dir()
        if not os.path.isdir(directory):
            return {}
        return {n: open(os.path.join(directory, n), "rb").read()
                for n in sorted(os.listdir(directory))}


class TheProducerEmitsUsableCandidates(FeedCase):

    def test_a_candidate_becomes_a_spool_record(self):
        self.assertTrue(self.emit())
        records = self.spool_bytes()
        self.assertEqual(len(records), 1)
        payload = json.loads(list(records.values())[0])
        self.assertEqual(payload["schema"], research_feed.FEED_SCHEMA)
        self.assertEqual(payload["contract_id"], "KXBTCD-1")
        self.assertEqual(len(payload["record_sha256"]), 64)

    def test_cent_prices_become_probabilities(self):
        """The book speaks cents; `atlas-alpha-v2` speaks probabilities."""
        self.emit()
        payload = json.loads(list(self.spool_bytes().values())[0])
        self.assertAlmostEqual(payload["yes_ask"], 0.46, places=6)
        self.assertAlmostEqual(payload["no_bid"], 0.54, places=6)

    def test_it_is_off_by_default(self):
        with patch.object(CFG, "RESEARCH_FEED_ENABLED", False):
            self.assertFalse(self.emit())
        self.assertEqual(self.spool_bytes(), {})

    def test_the_engine_carries_no_decision_across_the_boundary(self):
        """The research path must not learn what the engine decided, or the
        two stop being independent estimates of the same market."""
        self.emit()
        payload = json.loads(list(self.spool_bytes().values())[0])
        for leaked in ("decision", "accepted", "side", "net_edge", "strategy",
                       "model_probability", "confidence", "size"):
            self.assertNotIn(leaked, payload)

    def test_a_malformed_candidate_is_dropped_not_spooled(self):
        for bad in ({}, {"contract_id": "X"},
                    {**candidate_from_market(market(), BOOK),
                     "yes_ask": None},
                    {**candidate_from_market(market(), BOOK),
                     "yes_ask": 1.4},
                    {**candidate_from_market(market(), BOOK),
                     "yes_ask": float("nan")}):
            with self.subTest(candidate=str(bad)[:40]):
                self.assertFalse(self.feed.emit_candidate(bad))
        self.assertEqual(self.spool_bytes(), {})


class TheProducerCannotHurtTheEngine(FeedCase):

    def test_emit_never_raises(self):
        """Neither a dead filesystem nor a bug in the producer may reach the
        caller, which is a decision cycle.

        AA-10 moved the filesystem out of `emit_candidate`, so the two halves
        are now asserted separately: a `_build` failure is still visible to the
        caller as a refusal, while a filesystem failure happens on the writer
        thread and shows up as "nothing was spooled" rather than as a False
        return. The engine never learns about the disk -- by design.
        """
        with patch("research_spool.os.makedirs",
                   side_effect=PermissionError("read-only volume")):
            self.emit()                      # must not raise
        self.assertEqual(self.spool_bytes(), {})
        self.assertEqual(self.feed.writer.spool.stats["written"], 0)
        with patch.object(ResearchFeed, "_build",
                          side_effect=RuntimeError("boom")):
            self.assertFalse(self.emit())

    def test_a_feed_failure_never_trips_the_persistence_sentinel(self):
        """A failed research write is not a critical persistence failure and
        must never block an order the risk engine approved -- nor unblock
        one."""
        PersistenceSentinel.reset()
        with patch("research_spool.os.makedirs",
                   side_effect=OSError("disk full")):
            self.emit()
        self.assertTrue(PersistenceSentinel.healthy())

    def test_the_spool_is_bounded_by_count(self):
        """An unbounded research spool on a shared volume is a slow way to
        take the engine down with ENOSPC."""
        with patch.object(CFG, "RESEARCH_FEED_MAX_SPOOL", 3):
            for i in range(10):
                self.feed.emit_candidate(candidate_from_market(
                    market(f"KX-{i}"), BOOK))
            self.assertLessEqual(len(self.spool_bytes()), 3)

    def test_the_spool_is_bounded_by_age(self):
        self.emit("KX-OLD")
        old = os.path.join(spool_dir(), sorted(os.listdir(spool_dir()))[0])
        os.utime(old, (0, 0))
        with patch.object(CFG, "RESEARCH_FEED_MAX_AGE_S", 60.0):
            self.emit("KX-NEW")
        names = list(self.spool_bytes())
        self.assertEqual(len(names), 1)

    def test_it_writes_only_inside_its_own_directory(self):
        before = sorted(os.listdir(self._tmp))
        self.emit()
        after = sorted(os.listdir(self._tmp))
        self.assertEqual(set(after) - set(before),
                         {research_feed.SPOOL_DIRNAME})


class TheConsumerMintsAndDeduplicates(FeedCase):

    def test_a_record_becomes_an_immutable_snapshot(self):
        self.emit()
        pending = SpoolConsumer().pending()
        self.assertEqual(len(pending), 1)
        snapshot, record = pending[0]
        snapshot.verify()
        self.assertEqual(snapshot.contract_id, "KXBTCD-1")
        self.assertAlmostEqual(snapshot.yes_ask, 0.46, places=6)
        self.assertTrue(snapshot.market_snapshot_id.startswith("snap-"))

    def test_the_consumer_never_writes_into_the_spool(self):
        """The authority boundary, as a byte comparison."""
        self.emit("KX-A")
        self.emit("KX-B")
        before = self.spool_bytes()
        consumer = SpoolConsumer()
        for snapshot, _ in consumer.pending():
            consumer.store.mark(snapshot.market_snapshot_id, STATUS_ANALYZED,
                                contract_id=snapshot.contract_id)
        self.assertEqual(self.spool_bytes(), before,
                         "the consumer modified the producer's spool")

    def test_the_same_snapshot_is_analysed_once(self):
        self.emit()
        consumer = SpoolConsumer()
        first = consumer.pending()
        self.assertEqual(len(first), 1)
        consumer.store.mark(first[0][0].market_snapshot_id, STATUS_ANALYZED)
        self.assertEqual(SpoolConsumer().pending(), [])

    def test_deduplication_survives_a_restart(self):
        """Otherwise every restart re-pays three vendors for work already
        done."""
        self.emit()
        consumer = SpoolConsumer()
        snapshot = consumer.pending()[0][0]
        consumer.store.mark(snapshot.market_snapshot_id, STATUS_ANALYZED)
        for _ in range(3):
            self.assertEqual(SpoolConsumer(store=ProcessedStore()).pending(), [])

    def test_duplicates_within_one_batch_are_collapsed(self):
        self.emit()
        # a second identical record, written directly to simulate two
        # scanner cycles landing in the same second
        name = sorted(os.listdir(spool_dir()))[0]
        payload = open(os.path.join(spool_dir(), name), "rb").read()
        with open(os.path.join(spool_dir(), "zz-copy.json"), "wb") as fh:
            fh.write(payload)
        consumer = SpoolConsumer()
        self.assertEqual(len(consumer.pending()), 1)
        self.assertEqual(consumer.stats["duplicates"], 1)

    def test_a_moved_price_is_a_new_snapshot_not_a_duplicate(self):
        """Identity is content-derived, so a genuinely new observation is
        analysed rather than suppressed."""
        self.emit()
        consumer = SpoolConsumer()
        first = consumer.pending()[0][0]
        consumer.store.mark(first.market_snapshot_id, STATUS_ANALYZED)
        payload = market()
        moved = dict(candidate_from_market(payload, BOOK, raw_book=payload))
        moved["yes_ask"] = 0.55
        moved["emitted_at_utc"] = (datetime.now(timezone.utc)
                                   + timedelta(seconds=30)
                                   ).isoformat(timespec="seconds")
        self.feed.emit_candidate(moved)
        self.feed.writer.drain(timeout=5.0)
        second = SpoolConsumer().pending()
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0][0].market_snapshot_id,
                            first.market_snapshot_id)

    def test_an_unmintable_record_is_recorded_rejected_not_retried_forever(self):
        os.makedirs(spool_dir(), exist_ok=True)
        payload = market()
        broken = dict(candidate_from_market(payload, BOOK, raw_book=payload))
        broken["expected_resolution_time_utc"] = "not-a-timestamp"
        broken["schema"] = research_feed.FEED_SCHEMA
        # AA-04: the digest is recomputed and compared, so a hand-edited
        # record is now refused at the checksum before anything tries to mint
        # it. Either way it must end up marked REJECTED rather than re-read on
        # every poll -- that is what this case is really about.
        broken["record_sha256"] = "f" * 64
        with open(os.path.join(spool_dir(), "broken.json"), "w") as fh:
            json.dump(broken, fh)
        consumer = SpoolConsumer()
        self.assertEqual(consumer.pending(), [])
        self.assertEqual(SpoolConsumer().pending(), [])
        self.assertIn("REJECTED", consumer.store.counts())

    def test_unreadable_spool_files_are_skipped_not_fatal(self):
        os.makedirs(spool_dir(), exist_ok=True)
        with open(os.path.join(spool_dir(), "garbage.json"), "w") as fh:
            fh.write("{{{ not json")
        self.emit()
        consumer = SpoolConsumer()
        self.assertEqual(len(consumer.pending()), 1)
        self.assertEqual(consumer.stats["malformed"], 1)

    def test_a_torn_state_row_does_not_lose_the_rows_before_it(self):
        self.emit()
        consumer = SpoolConsumer()
        snapshot = consumer.pending()[0][0]
        consumer.store.mark(snapshot.market_snapshot_id, STATUS_ANALYZED)
        with open(os.path.join(self._tmp, CFG.ALPHA_STATE_FILE), "a") as fh:
            fh.write('{"market_snapshot_id": "snap-tor')
        self.assertTrue(ProcessedStore().seen(snapshot.market_snapshot_id))


class TheEngineHookIsInert(FeedCase):
    """The engine's only involvement is a write to a spool directory."""

    def test_the_engine_imports_no_alpha_module_for_this(self):
        import ast
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tree = ast.parse(open(os.path.join(repo, "execution_engine.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertIn("research_feed", imported)
        self.assertEqual([m for m in imported if m.startswith("alpha_")], [])

    def test_research_feed_imports_nothing_from_either_subsystem(self):
        """The boundary module is the one place both sides touch, so its
        import list is pinned."""
        import ast
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tree = ast.parse(open(os.path.join(repo, "research_feed.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        # Widened deliberately for AA-01/AA-10; see the per-module pins in
        # `tests/test_research_feed_boundary.py`. None of these is an Alpha
        # module, and none of them reaches execution.
        self.assertEqual(imported,
                         {"datetime", "logging", "os", "config",
                          "candidate_contract", "research_spool"})


if __name__ == "__main__":
    import unittest
    unittest.main()
