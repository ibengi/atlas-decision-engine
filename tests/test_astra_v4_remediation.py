# -*- coding: utf-8 -*-
"""Astra counter-audit RA-01..RA-15, each reproduced before it was closed.

SHADOW ONLY. Nothing here places an order, touches CAPITAL, or imports a
broker or execution module.

WHY THESE TESTS LOOK LIKE THIS
    Every case below was written as a FAILING test against the v3 head
    (`762c794`) first, from the counterexample the independent counter-audit
    described, and only then was the code changed. The reproductions are kept
    rather than replaced by tests of the fix, because a test that only
    describes the fix cannot tell you whether the fix addressed the defect.

    They are also deliberately written in the auditor's own idiom: a synthetic
    hostile witness constructed from the outside, asserting on what ends up in
    the record, the spool, the ledger or the report -- never on a counter or a
    log line, which is the exact class of assertion that let two safety
    mutations survive 1,670 tests before AA-17.
"""
import errno
import json
import os
import stat as stat_module
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

import candidate_contract                                     # noqa: E402
import durable_append                                         # noqa: E402
import research_feed                                          # noqa: E402
import research_spool                                         # noqa: E402
from _alpha import AlphaCase                                  # noqa: E402
from _candidate import DROP, raw_market, valid_candidate      # noqa: E402
from config import CFG                                        # noqa: E402
from research_feed import candidate_from_market               # noqa: E402


def emitted(feed, candidate):
    """The record the producer would spool for `candidate`, or None.

    RA-03 moved checksum, hashing, validation and diagnostics off the
    observer thread, so the record is finalized by the writer. Tests that
    care about the RECORD call the finalizer directly; tests that care about
    the THREAD are the RA-03 cases below.
    """
    return feed._build(candidate)


def feed_without_writer():
    return research_feed.ResearchFeed(start_writer=False)


# ════════════════════════════════════════════════════════════════════════
# RA-01 — a valid name short-circuited validation of the rest of the member
# ════════════════════════════════════════════════════════════════════════
class RA01_ValidNameSkippedTheRestOfTheContainer(AlphaCase):
    """`_settlement_source_name` returned on the FIRST present key.

    AA-02 taught it to type-check before coercing, and it does -- for
    whichever of `name`/`url` it happens to look at first. The loop

        for key in ("name", "url"):
            if key not in value: continue
            ...
            return text or None

    returns as soon as `name` is present and readable, so `url` is NEVER
    examined. A settlement source published as

        {"name": "CF Benchmarks RTI", "url": 8080}

    was therefore accepted as the settlement authority "CF Benchmarks RTI",
    with the malformed half of the same object silently unread. The member is
    not half-valid: an object whose URL is an integer is a member this
    producer cannot claim to have understood, and AA-02's own rule -- one
    malformed member taints the collection -- was simply never reached.
    """

    def test_a_member_with_a_numeric_url_is_not_half_valid(self):
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI", "url": 8080}])
        candidate = valid_candidate(market)
        self.assertIsNone(
            candidate["resolution_source"],
            "the name was accepted and the numeric URL in the SAME object "
            "was never looked at")
        self.assertIsNone(emitted(feed_without_writer(), candidate),
                          "a record was emitted from a member the producer "
                          "only partly validated")

    def test_a_member_with_a_structured_url_is_refused(self):
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI", "url": {"href": "https://x"}}])
        self.assertIsNone(valid_candidate(market)["resolution_source"])

    def test_a_member_with_a_blank_url_is_refused(self):
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI", "url": "   "}])
        self.assertIsNone(
            valid_candidate(market)["resolution_source"],
            "'published an empty URL' is not 'published no URL'")

    def test_one_malformed_member_still_taints_the_whole_collection(self):
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI"},
            {"name": "Coinbase", "url": 8080}])
        self.assertIsNone(valid_candidate(market)["resolution_source"])

    def test_an_explicit_json_null_url_is_absent_not_malformed(self):
        """The one case that is NOT a refusal, stated so the rule is falsifiable.

        A JSON `null` is how a feed says "not published". Treating it as a
        malformed value would refuse the ordinary case and teach nobody
        anything; treating a non-null non-string as absent is what RA-01 is
        about.
        """
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI", "url": None}])
        self.assertEqual(valid_candidate(market)["resolution_source"],
                         "CF Benchmarks RTI")

    def test_the_container_itself_must_have_a_shape_we_recognise(self):
        for hostile in ({"sources": [{"name": "A"}]},
                        {"name": 12345},
                        [{"name": "A"}, 7],
                        [{"name": "A"}, {"ref": "B"}],
                        123,
                        True):
            with self.subTest(container=hostile):
                market = raw_market(settlement_sources=hostile)
                self.assertIsNone(
                    valid_candidate(market)["resolution_source"],
                    f"{hostile!r} was normalized into a settlement authority")


# ════════════════════════════════════════════════════════════════════════
# RA-02 — the alias comparator flattened structure, so conflicts vanished
# ════════════════════════════════════════════════════════════════════════
class RA02_StructuredSourceIdentitiesWereFlattenedBeforeComparison(AlphaCase):
    """`", ".join(names)` is not a canonical identity, it is a rendering.

    `_settlement_source_name` is handed to `resolve_alias` as the COMPARATOR
    for `resolution_source`, which is how AA-03's contradiction detection
    decides whether two alias keys agree. Flattening a list of objects into
    one comma-joined string destroys exactly the two things that distinguish
    two settlement authorities:

      COLLECTION BOUNDARIES   `[{"name": "A"}, {"name": "B"}]` -- two
                              authorities -- and `[{"name": "A, B"}]` -- one
                              authority whose name contains a comma -- both
                              render as `"A, B"`. The comparator therefore
                              reported them as the SAME fact and the
                              contradiction was resolved silently in favour
                              of whichever alias is listed first in a tuple
                              in `candidate_contract`.

      URLS                    the URL was dropped entirely, so two aliases
                              naming one authority at two DIFFERENT locations
                              compared equal, and the location the exchange
                              actually published never reached the record.

    Both are AA-03 re-opened through a different door: a contradiction the
    producer cannot see is a contradiction it files as agreement.
    """

    def test_two_authorities_and_one_comma_named_authority_are_not_equal(self):
        market = raw_market(
            settlement_sources=[{"name": "A"}, {"name": "B"}],
            settlement_source=[{"name": "A, B"}])
        candidate = valid_candidate(market)
        self.assertIn("resolution_source", candidate["contradictory_fields"],
                      "two authorities and one comma-named authority "
                      "compared equal, so the disagreement was never seen")
        self.assertIsNone(emitted(feed_without_writer(), candidate),
                          "a record carrying an unseen source contradiction "
                          "reached the spool")

    def test_the_same_authority_at_two_urls_is_a_contradiction(self):
        market = raw_market(
            settlement_sources=[{"name": "CF Benchmarks RTI",
                                 "url": "https://a.example/rti"}],
            settlement_source=[{"name": "CF Benchmarks RTI",
                                "url": "https://b.example/rti"}])
        candidate = valid_candidate(market)
        self.assertIn("resolution_source", candidate["contradictory_fields"],
                      "the URL was dropped before comparison, so two "
                      "different locations read as one fact")

    def test_a_published_url_survives_into_the_record(self):
        market = raw_market(settlement_sources=[
            {"name": "CF Benchmarks RTI",
             "url": "https://www.cfbenchmarks.com/data/indices/BRTI"}])
        source = valid_candidate(market)["resolution_source"]
        self.assertIsNotNone(source)
        self.assertIn("cfbenchmarks.com", source,
                      "the URL the exchange published was dropped; the "
                      "record cannot say WHERE the authority was")

    def test_the_rendered_identity_is_injective(self):
        one = valid_candidate(raw_market(
            settlement_sources=[{"name": "A, B"}]))["resolution_source"]
        two = valid_candidate(raw_market(
            settlement_sources=[{"name": "A"},
                                {"name": "B"}]))["resolution_source"]
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        self.assertNotEqual(one, two,
                            "one authority named 'A, B' and two authorities "
                            "'A' and 'B' produced the same record field")

    def test_agreeing_aliases_in_different_shapes_still_agree(self):
        """The comparator must not manufacture contradictions either.

        A single object and a one-element list carrying that object are the
        same claim in two shapes, and refusing them would make the producer
        useless against a feed that uses both.
        """
        market = raw_market(
            settlement_sources=[{"name": "CF Benchmarks RTI"}],
            settlement_source={"name": "CF Benchmarks RTI"})
        candidate = valid_candidate(market)
        self.assertEqual(candidate["contradictory_fields"], {})
        self.assertEqual(candidate["resolution_source"], "CF Benchmarks RTI")

    def test_the_conflict_evidence_names_both_alias_keys(self):
        market = raw_market(
            settlement_sources=[{"name": "A"}, {"name": "B"}],
            settlement_source=[{"name": "A, B"}])
        values = valid_candidate(market)["contradictory_fields"][
            "resolution_source"]
        self.assertEqual(set(values), {"settlement_sources",
                                       "settlement_source"},
                         "the contradiction was recorded without saying "
                         "which alias claimed what")


# ════════════════════════════════════════════════════════════════════════
# RA-03 — hashing, validation and diagnostics still ran on the observer
# ════════════════════════════════════════════════════════════════════════
class RA03_TheObserverThreadStillHashedAndValidated(AlphaCase):
    """AA-10 moved `write`, `fsync`, `prune` and then `log` off the cycle.

    It did not move the CPU work. `ResearchFeed._build` ran, on the engine's
    own thread, for every candidate of every cycle:

        content["record_sha256"] = compute_checksum(content)   # json + sha256
        errors = validate_record(content)                      # full contract

    `compute_checksum` serializes the whole record with `json.dumps` and
    hashes it; `validate_record` walks every field, every provenance path and
    every quote verdict, and on refusal the producer then FORMATTED the
    diagnostic -- interpolating the entire `errors` list into an f-string --
    before handing it over. All of that is unbounded work proportional to the
    record, performed inside the decision cycle, for a subsystem whose whole
    design claim is that it cannot delay the money path.

    AA-10's own argument applies unchanged: the observer's only obligation is
    to hand over a dict and return.
    """

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()

    def feed(self):
        spool = research_spool.BoundedSpool(
            os.path.join(self._tmp, "spool"), max_records=100,
            max_bytes=10 ** 7, max_record_bytes=10 ** 6, max_age_s=3600)
        writer = research_spool.ResearchWriter(spool, start=False)
        self.addCleanup(writer.stop)
        return research_feed.ResearchFeed(writer=writer)

    def test_the_digest_is_not_computed_on_the_calling_thread(self):
        feed = self.feed()
        callers = []
        real = research_feed.compute_checksum

        def watched(record):
            callers.append(threading.current_thread().name)
            return real(record)

        with patch.object(research_feed, "compute_checksum", watched):
            feed.emit_candidate(valid_candidate())
            self.assertNotIn(threading.current_thread().name, callers,
                             "the engine thread serialized and hashed the "
                             "whole record itself")

    def test_the_contract_is_not_validated_on_the_calling_thread(self):
        feed = self.feed()
        callers = []
        real = research_feed.validate_record

        def watched(record, **kw):
            callers.append(threading.current_thread().name)
            return real(record, **kw)

        with patch.object(research_feed, "validate_record", watched):
            feed.emit_candidate(valid_candidate())
            feed.emit_candidate(valid_candidate(raw_market(title=DROP)))
            self.assertNotIn(threading.current_thread().name, callers,
                             "the engine thread ran the full contract "
                             "validation itself")

    def test_a_slow_digest_does_not_slow_the_observer(self):
        feed = self.feed()
        real = research_feed.compute_checksum

        def slow(record):
            time.sleep(0.2)
            return real(record)

        with patch.object(research_feed, "compute_checksum", slow):
            started = time.time()
            for index in range(20):
                feed.emit_candidate(valid_candidate(
                    raw_market(ticker=f"KX-{index}")))
            elapsed = time.time() - started
        self.assertLess(elapsed, 1.0,
                        f"20 candidates cost the engine {elapsed:.2f}s of "
                        f"hashing it should never have done")

    def test_the_work_is_deferred_not_skipped(self):
        """Off the observer must not mean never: the writer still validates."""
        feed = self.feed()
        self.assertTrue(feed.emit_candidate(valid_candidate()))
        feed.emit_candidate(valid_candidate(raw_market(rules_primary=DROP)))
        feed.writer.start()
        self.addCleanup(feed.writer.stop)
        self.assertTrue(feed.writer.drain(timeout=10))
        spooled = [n for n in os.listdir(os.path.join(self._tmp, "spool"))
                   if n.endswith(".json")]
        self.assertEqual(len(spooled), 1,
                         "the writer either dropped the valid record or "
                         "spooled the invalid one")
        self.assertEqual(feed.refused_incomplete, 1)

    def test_the_finalizer_is_total_so_a_refusal_is_never_a_silent_drop(self):
        """NEW-01, arriving by a different route.

        Moving the work onto the writer changed who catches its failures. A
        record the contract cannot even HASH -- `canonical_json` refuses NaN
        outright -- used to raise into `emit_candidate`, which counted it. On
        the writer's thread the same exception lands in the general catch
        that keeps the thread alive, which logs and moves on: the record is
        still refused and the producer's own counters say nothing happened.

        So the totality lives where the counters are.
        """
        feed = self.feed()
        for breakage in (ValueError("unserializable"),
                         OverflowError("too big"),
                         RecursionError("too deep"),
                         MemoryError()):
            with self.subTest(breakage=type(breakage).__name__):
                before = feed.rejected
                with patch.object(research_feed, "compute_checksum",
                                  side_effect=breakage):
                    self.assertIsNone(feed._build(valid_candidate()))
                self.assertEqual(feed.rejected, before + 1,
                                 "the refusal was dropped without being "
                                 "counted")

    def test_a_nan_quote_is_refused_and_counted_not_dropped(self):
        """The concrete record that cannot be hashed at all."""
        feed = self.feed()
        candidate = valid_candidate()
        candidate["yes_bid"] = float("nan")
        before = feed.rejected
        self.assertIsNone(feed._build(candidate))
        self.assertEqual(feed.rejected, before + 1)

    def test_no_hashing_or_validation_is_reachable_from_the_emit_path(self):
        """Static, because a timing test can only prove the calls that ran."""
        import ast
        with open("research_feed.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        emit_path = {"emit_candidate", "_admit", "_size", "_note"}
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}
        self.assertEqual(emit_path - defined, set(),
                         "this test names functions that no longer exist, so "
                         "it proves nothing")
        forbidden = {"compute_checksum", "validate_record", "canonical_json",
                     "canonical_content"}
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) \
                    or node.name not in emit_path:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "id", None) or \
                        getattr(inner.func, "attr", None)
                    if name in forbidden:
                        offences.append(f"{node.name}:{inner.lineno} {name}()")
                    if isinstance(inner.func, ast.Attribute) and \
                            isinstance(inner.func.value, ast.Name) and \
                            inner.func.value.id == "log":
                        offences.append(f"{node.name}:{inner.lineno} "
                                        f"log.{inner.func.attr}()")
        self.assertEqual(offences, [], "\n".join(offences))


# ════════════════════════════════════════════════════════════════════════
# RA-04 — the spool scan took a second look and believed it
# ════════════════════════════════════════════════════════════════════════
class RA04_TheSecondStatCouldDisagreeWithTheFirst(AlphaCase):
    """`os.stat(path)` and then `os.path.isfile(path)` are two observations.

    `_scan` stats every entry -- raising `SpoolCapacityUnknown` when the stat
    fails, which is right -- and then asks `os.path.isfile(path)`, which
    stats the SAME path a second time. Two problems, both of which end with
    the bound being wrong in the unsafe direction:

      * `os.path.isfile` swallows every `OSError` and returns False. So the
        one case `_scan` was careful to fail closed on -- metadata we cannot
        read -- silently becomes "not a file, do not count it" when it
        happens on the second look instead of the first.

      * between the two calls the entry can change. A record counted by the
        first stat can be absent from the second, and the file then occupies
        the volume while being invisible to every budget that is supposed to
        bound it.

    The fix is not to retry: it is to use the metadata already in hand.
    """

    def spool(self, **kw):
        directory = os.path.join(self._tmp, "spool")
        return research_spool.BoundedSpool(
            directory, max_records=kw.get("max_records", 3),
            max_bytes=kw.get("max_bytes", 10 ** 7),
            max_record_bytes=10 ** 6, max_age_s=3600)

    def test_a_disagreeing_second_look_cannot_erase_a_record(self):
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        for index in range(3):
            with open(os.path.join(spool.directory, f"r{index}.json"),
                      "w", encoding="utf-8") as fh:
                fh.write("{}")
        real_isfile = os.path.isfile

        def hostile(path):
            if str(path).startswith(spool.directory):
                return False          # the second look disagrees
            return real_isfile(path)

        with patch("os.path.isfile", side_effect=hostile):
            capacity = spool.capacity()
        self.assertEqual(capacity["records"], 3,
                         "a second, disagreeing stat erased three records "
                         "from the bound they are supposed to occupy")

    def test_uncertain_metadata_on_our_own_suffix_fails_closed(self):
        spool = self.spool()
        os.makedirs(os.path.join(spool.directory, "impostor.json"),
                    exist_ok=True)
        with self.assertRaises(research_spool.SpoolCapacityUnknown):
            spool.capacity()

    def test_the_reservation_lock_is_still_not_counted(self):
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        with open(os.path.join(spool.directory,
                               research_spool.RESERVATION_LOCK + ".lock"),
                  "w", encoding="utf-8") as fh:
            fh.write("")
        capacity = spool.capacity()
        self.assertEqual(capacity["occupied"], 0)


# ════════════════════════════════════════════════════════════════════════
# RA-05 — unknown durability was reported as a successful durable append
# ════════════════════════════════════════════════════════════════════════
class RA05_UnknownDurabilityWasReportedAsSuccess(AlphaCase):
    """`append_line`'s contract is the strongest one in the subsystem.

    Its docstring says it "either completes the whole sequence or raises --
    there is no outcome in which a caller is told 'written' without
    durability having been attempted AND confirmed". Three paths inside it
    broke that promise by swallowing the uncertainty:

      `tail_is_torn`      returned False on ANY `OSError`. An unreadable tail
                          is not an intact tail: if we cannot see whether the
                          last record is complete, appending directly may
                          splice the new row onto a broken one -- the exact
                          AA-12 loss the separator exists to prevent -- and
                          the caller is told it succeeded.

      `_fsync_directory`  returned silently when the directory could not be
                          OPENED, and passed silently when its `fsync`
                          failed. The bytes are then durable under a NAME
                          that is not, which reads after a crash as a ledger
                          that never existed -- and `append_line` returned
                          normally, so the caller published a terminal
                          acknowledgement for it.

    Everything above the append -- PREPARE, the COMMIT receipt, the processed
    mark -- is built on "this returned, therefore it is durable". These three
    holes made that sentence false.
    """

    def path(self):
        return os.path.join(self._tmp, "ledger.jsonl")

    def test_an_unreadable_tail_is_not_an_intact_tail(self):
        path = self.path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}')          # torn: no trailing newline
        real = durable_append.os.path.getsize

        def hostile(target):
            if str(target) == path:
                raise OSError(errno.EIO, "the device cannot be read")
            return real(target)

        with patch.object(durable_append.os.path, "getsize", hostile):
            with self.assertRaises(durable_append.DurabilityUnknown):
                durable_append.append_line(path, '{"b": 2}')

    def test_an_unreadable_last_byte_is_not_an_intact_tail(self):
        path = self.path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}')
        real_open = durable_append.os.open

        def hostile(target, flags, *a, **kw):
            if str(target) == path and flags == os.O_RDONLY:
                raise OSError(errno.EIO, "the device cannot be read")
            return real_open(target, flags, *a, **kw)

        with patch.object(durable_append, "open",
                          side_effect=OSError(errno.EIO, "unreadable"),
                          create=True):
            with self.assertRaises(durable_append.DurabilityUnknown):
                durable_append.append_line(path, '{"b": 2}')

    def test_a_directory_that_cannot_be_opened_is_not_a_durable_name(self):
        path = self.path()
        real_open = durable_append.os.open
        parent = os.path.dirname(os.path.abspath(path))

        def hostile(target, flags, *a, **kw):
            if str(target) == parent:
                raise OSError(errno.EACCES, "cannot open the directory")
            return real_open(target, flags, *a, **kw)

        with patch.object(durable_append.os, "open", hostile):
            with self.assertRaises(durable_append.DurabilityUnknown):
                durable_append.append_line(path, '{"a": 1}')

    def test_a_directory_fsync_that_fails_is_not_a_durable_name(self):
        path = self.path()
        real_fsync = durable_append.os.fsync

        def hostile(fd):
            if stat_module.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "directory fsync failed")
            return real_fsync(fd)

        with patch.object(durable_append.os, "fsync", hostile):
            with self.assertRaises(durable_append.DurabilityUnknown):
                durable_append.append_line(path, '{"a": 1}')

    def test_a_file_that_does_not_exist_yet_is_not_uncertain(self):
        """The rule must not fire on the ordinary first append."""
        path = self.path()
        durable_append.append_line(path, '{"a": 1}')
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"a": 1}\n')

    def test_the_directory_name_is_made_durable_on_creation(self):
        path = self.path()
        synced = []
        real_fsync = durable_append.os.fsync

        def watched(fd):
            if stat_module.S_ISDIR(os.fstat(fd).st_mode):
                synced.append(fd)
            return real_fsync(fd)

        with patch.object(durable_append.os, "fsync", watched):
            durable_append.append_line(path, '{"a": 1}')
        self.assertTrue(synced, "the directory entry was never fsynced")


# ════════════════════════════════════════════════════════════════════════
# RA-06 — the budget ledger had its own, weaker append
# ════════════════════════════════════════════════════════════════════════
class RA06_TheBudgetLedgerBypassedTheDurableProtocol(AlphaCase):
    """AA-12 and AA-14 were applied to two of the three append-only files.

    The calibration ledger learned the protocol; the processed store learned
    it in v3; `BudgetLedger.record` kept its own `os.open` + a single
    `os.write` + `fsync`, with no short-write loop, no torn-tail separation
    and no writer lock. The consequences are not cosmetic, because this is
    the file every cost cap is enforced against:

      * a torn row spliced the next row onto itself, and `rows()` then hit
        the combined unparsable line LAST and `break`-ed -- so the spend was
        silently dropped and the daily cap was enforced against a number
        that was too small;

      * an unparsable row anywhere else was logged and SKIPPED, which is the
        same under-count with a log line attached;

      * a failed `record_actual` was swallowed entirely, so money that had
        genuinely been spent was invisible to every later check.

    Under-counting spend is not a conservative failure. `BudgetGuard.check`
    already refuses when the ledger cannot be READ; it has to refuse just as
    firmly when the ledger can be read but cannot be believed.
    """

    def ledger(self):
        from alpha_cost import BudgetLedger
        return BudgetLedger(os.path.join(self._tmp, "budget.jsonl"))

    def test_a_torn_row_does_not_swallow_the_next_one(self):
        """AA-12's separator, applied to the third ledger.

        Both halves are asserted, because both are the finding: the damaged
        fragment stays exactly where it was -- it is the only evidence that a
        write was attempted -- and the new row is a whole line of its own
        instead of being spliced onto it.
        """
        fragment = '{"ts": 1, "api_cost_usd": 0.5'
        ledger = self.ledger()
        with open(ledger.path, "w", encoding="utf-8") as fh:
            fh.write(fragment)                            # crash mid-append
        ledger.record({"provider": "p", "api_cost_usd": 0.25})
        with open(ledger.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], fragment,
                         "the historical fragment was altered or swallowed")
        self.assertEqual(json.loads(lines[1])["api_cost_usd"], 0.25,
                         "the new row was spliced onto the torn one and both "
                         "were lost")
        # And the total is now KNOWN-uncertain rather than quietly too small:
        # a fragment on disk is a spend nobody can bound.
        with self.assertRaises(RuntimeError):
            ledger.spent_today()

    def test_an_unparsable_row_makes_spend_unknown_not_smaller(self):
        ledger = self.ledger()
        ledger.record({"provider": "p", "api_cost_usd": 1.0})
        with open(ledger.path, "a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        ledger.record({"provider": "p", "api_cost_usd": 1.0})
        with self.assertRaises(RuntimeError):
            ledger.spent_today()

    def test_unknown_spend_blocks_the_next_provider_call(self):
        from alpha_cost import REASON_BUDGET, BudgetGuard, PricingTable
        from _alpha import write_pricing
        pricing_path = os.path.join(self._tmp, "pricing.json")
        write_pricing(pricing_path)
        ledger = self.ledger()
        ledger.record({"provider": "grok", "api_cost_usd": 0.01})
        with open(ledger.path, "a", encoding="utf-8") as fh:
            fh.write("torn\n")
        guard = BudgetGuard(PricingTable(pricing_path), ledger)
        verdict = guard.check("grok", "grok", prompt_chars=100)
        self.assertFalse(verdict["allowed"],
                         "a provider was called while the budget ledger "
                         "could be read but not believed")
        self.assertEqual(verdict["reason"], REASON_BUDGET)

    def test_a_spend_that_could_not_be_recorded_blocks_the_next_call(self):
        from alpha_cost import REASON_BUDGET, BudgetGuard, PricingTable
        from _alpha import write_pricing
        pricing_path = os.path.join(self._tmp, "pricing.json")
        write_pricing(pricing_path)
        guard = BudgetGuard(PricingTable(pricing_path), self.ledger())
        with patch.object(type(guard.ledger), "record",
                          side_effect=OSError(errno.EIO, "no space")):
            guard.record_actual({"provider": "grok", "model": "grok",
                                 "api_cost_usd": 5.0, "cost_priced": True})
        verdict = guard.check("grok", "grok", prompt_chars=100)
        self.assertFalse(verdict["allowed"],
                         "money was spent, the row could not be written, and "
                         "the next call was allowed anyway")
        self.assertEqual(verdict["reason"], REASON_BUDGET)

    def test_the_budget_ledger_uses_the_shared_serialized_append(self):
        """Static: one durable-append protocol, not three."""
        import ast
        with open("alpha_cost.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        record = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "record")
        calls = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                 for c in ast.walk(record) if isinstance(c, ast.Call)}
        self.assertIn("serialized_append", calls)
        self.assertNotIn("write", calls,
                         "BudgetLedger.record still calls os.write itself")


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()


# ════════════════════════════════════════════════════════════════════════
# Shared service harness for RA-07..RA-10
# ════════════════════════════════════════════════════════════════════════
class _CountingProvider:
    """A provider double that records whether it was called at all."""

    def __init__(self, name="grok"):
        from _alpha import FakeProvider
        self._inner = FakeProvider(name)
        self.name = name
        self.model = name
        self.calls = 0

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def analyze(self, snapshot, timeout):
        self.calls += 1
        return self._inner.analyze(snapshot, timeout)


class _EmptySource:
    directory = None

    def records(self):
        return []


class ServiceCase(AlphaCase):
    """One AlphaShadowService on throwaway files, no network, no broker."""

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "ALPHA_GATEWAY_ENABLED", True))
        self._patches[-1].start()

    def service(self, providers, *, caps=None):
        from _alpha import write_pricing
        from alpha_consumer import ProcessedStore, SpoolConsumer
        from alpha_cost import BudgetGuard, BudgetLedger, PricingTable
        from alpha_ledger import AlphaLedger
        from alpha_service import AlphaShadowService
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.store = ProcessedStore(path=os.path.join(self._tmp, "p.jsonl"))
        pricing = PricingTable(write_pricing(
            os.path.join(self._tmp, "pricing.json"),
            models=[p.name for p in providers]))
        for key, value in (caps or {}).items():
            patcher = patch.object(CFG, key, value)
            self._patches.append(patcher)
            patcher.start()
        return AlphaShadowService(
            providers=providers, ledger=self.ledger,
            consumer=SpoolConsumer(source=_EmptySource(), store=self.store),
            budget=BudgetGuard(pricing=pricing, ledger=BudgetLedger(
                os.path.join(self._tmp, "budget.jsonl"))),
            quote_fn=lambda: {"yes_bid": 0.44, "yes_ask": 0.46,
                              "no_bid": 0.54, "no_ask": 0.56})

    def record(self):
        from _candidate import valid_record
        return valid_record()


# ════════════════════════════════════════════════════════════════════════
# RA-07 — the acknowledgement named an id the ledger had never written
# ════════════════════════════════════════════════════════════════════════
class RA07_TheGeneratedIdWasAcknowledgedInsteadOfTheDurableOne(ServiceCase):
    """AA-13 correction 7 closed one door and left the adjacent one open.

    `record_prediction` has a recovery branch: when a PREDICTION row exists
    for this analysis with no COMMIT receipt -- the previous attempt's append
    landed and its fsync failed -- it FINISHES that commit and returns the
    ORIGINAL row, so one analysis keeps one identity. That part is right.

    Nothing reads the return value. `AlphaGateway.analyze` calls
    `self.ledger.record_prediction(opportunity)` and discards the row, so
    `opportunity["prediction_id"]` is still the freshly generated
    `pred-<sha of snapshot|now>` -- an id that was never written to the
    ledger, because the recovery branch deliberately did not write it.

    `alpha_service` then:

      * marks the snapshot ANALYZED and STORES that generated id, so every
        later join from a settlement back to the prediction it settles finds
        nothing;
      * schedules follow-up price observations against it, so the
        OBSERVATION rows name a prediction that does not exist either.

    `prediction_is_committed` says True throughout, and it is right: a
    prediction for this snapshot IS committed. It is simply not the one the
    service is talking about.
    """

    def uncommitted_prediction(self, snapshot, prediction_id="pred-crashed"):
        """A PREDICTION row with no receipt: the crash RA-07 is about."""
        from alpha_ledger import (LEDGER_SCHEMA, ROW_PREDICTION,
                                  analysis_identity)
        self.ledger.log.append({
            "schema": LEDGER_SCHEMA, "kind": ROW_PREDICTION,
            "at": "2026-01-01T00:00:00+00:00",
            "analysis_id": analysis_identity(snapshot.market_snapshot_id),
            "prediction_id": prediction_id,
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id})
        self.assertFalse(
            self.ledger.prediction_is_committed(snapshot.market_snapshot_id),
            "the fixture is wrong: this row must have no receipt")
        return prediction_id

    def test_the_acknowledgement_names_the_row_that_is_actually_on_disk(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        durable = self.uncommitted_prediction(snapshot)

        service._analyze_one(snapshot, self.record())

        row = self.store._load()[snapshot.market_snapshot_id]
        self.assertEqual(
            row["prediction_id"], durable,
            "the snapshot was acknowledged with a prediction id the ledger "
            "never wrote")
        self.assertIsNotNone(self.ledger.find_prediction(row["prediction_id"]),
                             "the acknowledged prediction id is not in the "
                             "ledger at all")

    def test_the_result_reports_the_durable_identity(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        durable = self.uncommitted_prediction(snapshot)
        result = service._analyze_one(snapshot, self.record())
        self.assertEqual(result["prediction_id"], durable)

    def test_observations_are_scheduled_against_the_durable_identity(self):
        service = self.service(
            [_CountingProvider()],
            caps={"ALPHA_OBSERVATION_INTERVALS_S": "60,300"})
        snapshot = self.snapshot()
        durable = self.uncommitted_prediction(snapshot)
        service._analyze_one(snapshot, self.record())
        scheduled = {pid for _due, pid, _interval
                     in service._pending_observations}
        self.assertEqual(
            scheduled, {durable},
            "follow-up observations were scheduled against a prediction the "
            "ledger does not contain")

    def test_exactly_one_prediction_row_survives_the_recovery(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        self.uncommitted_prediction(snapshot)
        service._analyze_one(snapshot, self.record())
        self.assertEqual(len(self.ledger.predictions()), 1)

    def test_an_ordinary_analysis_still_reports_its_own_id(self):
        """Anti-vacuity: the normal path must not be rewritten by this."""
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        result = service._analyze_one(snapshot, self.record())
        self.assertTrue(result["prediction_id"].startswith("pred-"))
        self.assertIsNotNone(
            self.ledger.find_prediction(result["prediction_id"]))


# ════════════════════════════════════════════════════════════════════════
# RA-08 — a retry treated readable PREPARE bytes as a durable PREPARE
# ════════════════════════════════════════════════════════════════════════
class RA08_PrepareDurabilityWasNotRecheckedOnRetry(ServiceCase):
    """AA-13 made PREPARE a precondition for dispatch. It is checked once.

    `AlphaLedger.prepare` is idempotent by LOOKUP: `find_prepare(analysis_id)`
    and, if a row comes back, return it. That row is READ from the file --
    which is precisely the thing AA-13's re-audit established is not proof of
    durability, and the reason the COMMIT receipt exists for predictions.

    The failure is the ordinary one. An append whose `write` landed and whose
    `fsync` failed leaves bytes that read back perfectly while still being one
    power cut from never having existed. `prepare()` raised on that attempt,
    so the service correctly deferred and spent nothing. On the NEXT poll
    `find_prepare` returns those same readable bytes, `prepare()` returns
    without touching the device, the precondition is declared satisfied, and
    every provider is paid -- on a ledger that is still not writable, so the
    prediction that follows cannot be committed either.

    PREPARE now carries its own receipt, exactly as a prediction does, and
    the receipt's append is what makes the row durable (an fsync flushes the
    whole file). No receipt, no dispatch, on the first attempt and on every
    retry.
    """

    def hostile_fsync(self):
        """Every fsync on a regular file fails; directory fsyncs still work.

        That is the shape of the real failure: the bytes are accepted by the
        page cache and the device refuses to commit them.
        """
        real = durable_append.os.fsync

        def refuse(fd):
            if stat_module.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "the device will not commit")
            return real(fd)
        return patch.object(durable_append.os, "fsync", refuse)

    def test_a_retry_does_not_dispatch_on_readable_prepare_bytes(self):
        provider = _CountingProvider()
        service = self.service([provider])
        snapshot = self.snapshot()
        record = self.record()
        with self.hostile_fsync():
            first = service._analyze_one(snapshot, record)
            second = service._analyze_one(snapshot, record)
        self.assertTrue(first["deferred"])
        self.assertTrue(
            second["deferred"],
            "the retry declared PREPARE satisfied from bytes it only read")
        self.assertEqual(
            provider.calls, 0,
            "a provider was paid on a retry whose PREPARE was never durable")

    def test_the_prepare_bytes_really_are_readable_after_the_failure(self):
        """Anti-vacuity: if nothing was written, the case above proves nothing."""
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        with self.hostile_fsync():
            service._analyze_one(snapshot, self.record())
        from alpha_ledger import analysis_identity
        self.assertIsNotNone(
            self.ledger.find_prepare(
                analysis_identity(snapshot.market_snapshot_id)),
            "the PREPARE row is not on disk, so this scenario is not the one "
            "RA-08 describes")

    def test_a_durable_prepare_carries_a_receipt(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        from alpha_ledger import analysis_identity
        self.assertTrue(
            self.ledger.prepare_is_durable(
                analysis_identity(snapshot.market_snapshot_id)),
            "a successful PREPARE left no proof that it completed")

    def test_a_retry_after_the_device_recovers_completes_the_prepare(self):
        provider = _CountingProvider()
        service = self.service([provider])
        snapshot = self.snapshot()
        record = self.record()
        with self.hostile_fsync():
            service._analyze_one(snapshot, record)
        result = service._analyze_one(snapshot, record)
        self.assertFalse(result["deferred"])
        self.assertGreater(provider.calls, 0)
        from alpha_ledger import ROW_PREPARE, analysis_identity
        prepares = [r for r in self.ledger.rows()
                    if r.get("kind") == ROW_PREPARE]
        self.assertEqual(len(prepares), 1,
                         "the retry announced the analysis a second time")
        self.assertTrue(self.ledger.prepare_is_durable(
            analysis_identity(snapshot.market_snapshot_id)))


# ════════════════════════════════════════════════════════════════════════
# RA-09 — the processed cache was refreshed outside the append lock
# ════════════════════════════════════════════════════════════════════════
class RA09_TheProcessedCacheAdvancedPastAnotherWritersRow(AlphaCase):
    """AA-14 gave `ProcessedStore` a generation-keyed cache. The order is wrong.

    `mark()` appends under `serialized_append`, RELEASES the lock, and only
    then patches `_cache` and re-stamps `_generation`:

        with serialized_append(self.path) as append:
            append(line)                      # lock released here
        if self._cache is not None:
            self._cache[snapshot_id] = row
            self._generation = self._current_generation()

    Any row another writer appends between those two moments is inside the
    generation this store stamps and outside the cache it stamped it for. The
    cache then looks FRESH -- size, mtime and inode all match -- while missing
    a row that is on disk, and it stays that way until something else changes
    the file.

    A missing processed row reads as "this snapshot was never analysed", so
    the service pays for an analysis another writer has already committed --
    which is the AA-13 double-spend, reached through the cache instead of
    through the crash.
    """

    def store(self, name="p.jsonl"):
        from alpha_consumer import ProcessedStore
        return ProcessedStore(path=os.path.join(self._tmp, name))

    def test_a_row_written_by_another_store_is_not_hidden_by_our_mark(self):
        from alpha_consumer import STATUS_ANALYZED
        mine, theirs = self.store(), self.store()
        mine.mark("snap-mine-0", STATUS_ANALYZED)      # fills my cache
        self.assertIsNotNone(mine.status("snap-mine-0"))

        theirs.mark("snap-theirs", STATUS_ANALYZED)    # another writer
        mine.mark("snap-mine-1", STATUS_ANALYZED)      # my next append

        self.assertIsNotNone(
            mine.status("snap-theirs"),
            "my cache was stamped with a generation that includes another "
            "writer's row while not containing it")
        self.assertIsNotNone(mine.status("snap-mine-1"))

    def test_the_row_really_is_on_disk(self):
        """Anti-vacuity."""
        from alpha_consumer import STATUS_ANALYZED
        mine, theirs = self.store(), self.store()
        mine.mark("snap-mine-0", STATUS_ANALYZED)
        theirs.mark("snap-theirs", STATUS_ANALYZED)
        mine.mark("snap-mine-1", STATUS_ANALYZED)
        with open(mine.path, encoding="utf-8") as fh:
            ids = [json.loads(line)["market_snapshot_id"]
                   for line in fh if line.strip()]
        self.assertEqual(ids, ["snap-mine-0", "snap-theirs", "snap-mine-1"])

    def test_a_concurrent_writer_in_another_process_is_seen(self):
        """The real shape of it: a second OS process, not a second object."""
        import subprocess
        from alpha_consumer import STATUS_ANALYZED
        mine = self.store()
        mine.mark("snap-mine-0", STATUS_ANALYZED)
        program = (
            "import sys; sys.path.insert(0, %r);\n"
            "import _bootstrap\n"
            "from alpha_consumer import ProcessedStore\n"
            "ProcessedStore(path=%r).mark('snap-other-process', 'ANALYZED')\n"
            % (os.path.dirname(os.path.abspath(__file__)), mine.path))
        subprocess.run([sys.executable, "-c", program], check=True,
                       cwd=os.path.dirname(os.path.dirname(
                           os.path.abspath(__file__))))
        mine.mark("snap-mine-1", STATUS_ANALYZED)
        self.assertIsNotNone(mine.status("snap-other-process"))

    def test_the_cache_is_invalidated_inside_the_lock(self):
        """Static: the ordering is the finding, not the symptom."""
        import ast
        with open("alpha_consumer.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        mark = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "mark")
        appends = [n for n in ast.walk(mark) if isinstance(n, ast.With)]
        self.assertTrue(appends, "mark() no longer takes the append lock")
        inside = {getattr(t, "attr", None)
                  for w in appends for n in ast.walk(w)
                  for t in ast.walk(n) if isinstance(t, ast.Attribute)}
        self.assertIn("_generation", inside,
                      "the generation is stamped outside the append lock, "
                      "which is the whole of RA-09")


# ════════════════════════════════════════════════════════════════════════
# RA-10 — a budget refusal became a completed analysis during recovery
# ════════════════════════════════════════════════════════════════════════
class RA10_ABudgetRefusalTurnedTerminalOnRecovery(ServiceCase):
    """`_acknowledge_recovered` marks `STATUS_ANALYZED`. Unconditionally.

    A snapshot refused on spend is not an analysis. Every provider was
    EXCLUDED before being called, `p_meta` is None, the state is
    `BUDGET_EXHAUSTED`, and `_analyze_one` correctly marks it DEFERRED --
    non-terminal, so a later poll retries it when the cap resets.

    A prediction ROW is still written for it, because the refusal itself is
    evidence worth keeping. So on the next poll `committed_prediction` finds
    that row, recovery fires, and `_acknowledge_recovered` marks the snapshot
    ANALYZED. `seen()` then returns True and the observation is never
    retried: a cap that was meant to defer work has silently discarded it,
    and the ledger now contains a terminal analysis whose `p_meta` is None.

    Recovery may only re-publish the state the recovered row actually has. A
    refusal stays a refusal.
    """

    def refusing_service(self):
        """Caps set so low that every provider is refused before the call."""
        return self.service([_CountingProvider()],
                            caps={"ALPHA_MAX_COST_PER_ANALYSIS_USD": 1e-9,
                                  "ALPHA_MAX_COST_PER_DAY_USD": 1e-9})

    def test_a_budget_refusal_is_deferred_on_the_first_pass(self):
        """Anti-vacuity: v3 already got this half right."""
        from alpha_consumer import STATUS_DEFERRED
        service = self.refusing_service()
        snapshot = self.snapshot()
        result = service._analyze_one(snapshot, self.record())
        self.assertTrue(result["deferred"])
        self.assertEqual(self.store.status(snapshot.market_snapshot_id),
                         STATUS_DEFERRED)
        self.assertFalse(self.store.seen(snapshot.market_snapshot_id))

    def test_recovery_does_not_promote_a_refusal_to_terminal(self):
        from alpha_consumer import STATUS_DEFERRED
        service = self.refusing_service()
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        recovered = service._analyze_one(snapshot, self.record())
        self.assertEqual(
            self.store.status(snapshot.market_snapshot_id), STATUS_DEFERRED,
            "a budget refusal became a completed analysis merely by being "
            "recovered")
        self.assertFalse(
            self.store.seen(snapshot.market_snapshot_id),
            "the refused snapshot is now terminal and will never be retried")
        self.assertTrue(recovered["deferred"])

    def test_no_observations_are_scheduled_for_a_recovered_refusal(self):
        service = self.refusing_service()
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        service._analyze_one(snapshot, self.record())
        self.assertEqual(service._pending_observations, [],
                         "follow-up observations were scheduled for an "
                         "analysis that was never performed")

    def test_the_refusal_is_retried_once_the_cap_allows_it(self):
        """The point of DEFERRED: the work comes back."""
        provider = _CountingProvider()
        service = self.service([provider])
        snapshot = self.snapshot()
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1e-9):
            service._analyze_one(snapshot, self.record())
        self.assertEqual(provider.calls, 0)
        service._analyze_one(snapshot, self.record())
        self.assertGreater(provider.calls, 0,
                           "the deferred snapshot was never re-analysed")

    def test_a_genuine_analysis_is_still_recovered_as_terminal(self):
        """Anti-vacuity: RA-10 must not make recovery useless."""
        from alpha_consumer import STATUS_ANALYZED
        provider = _CountingProvider()
        service = self.service([provider])
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        calls = provider.calls
        self.assertGreater(calls, 0)
        recovered = service._analyze_one(snapshot, self.record())
        self.assertEqual(provider.calls, calls, "it was analysed twice")
        self.assertFalse(recovered["deferred"])
        self.assertEqual(self.store.status(snapshot.market_snapshot_id),
                         STATUS_ANALYZED)

    def test_a_refusal_already_in_the_ledger_is_recovered_as_deferred(self):
        """The append-only half of RA-10.

        A spend refusal is no longer WRITTEN as a prediction, so the primary
        path cannot reach `_acknowledge_recovered` with one. History is
        append-only, though: rows like that are already on disk from earlier
        runs, and recovery must not promote them either. Without this case the
        guard in `_acknowledge_recovered` would be unreachable code that reads
        as a safety property.
        """
        from alpha_consumer import STATUS_DEFERRED
        from alpha_service import STATE_BUDGET_EXHAUSTED
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        self.ledger.record_prediction({
            "prediction_id": "pred-legacy-refusal",
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id,
            "state": STATE_BUDGET_EXHAUSTED,
            "state_reason": "every provider was refused before being called",
            "p_meta": None})

        result = service._analyze_one(snapshot, self.record())

        self.assertTrue(result["recovered"])
        self.assertTrue(result["deferred"])
        self.assertEqual(self.store.status(snapshot.market_snapshot_id),
                         STATUS_DEFERRED)
        self.assertFalse(self.store.seen(snapshot.market_snapshot_id))
        self.assertEqual(service._pending_observations, [])

    def test_a_spend_refusal_writes_no_prediction_row(self):
        """Nothing was analysed, so there is nothing to record."""
        service = self.refusing_service()
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        self.assertEqual(self.ledger.predictions(), [],
                         "a refusal in which no provider was asked was "
                         "recorded as a prediction")
        self.assertIsNone(self.ledger.committed_prediction(
            snapshot.market_snapshot_id))

    def test_the_announcement_survives_the_refusal(self):
        """The audit trail still says the analysis was announced and deferred."""
        from alpha_ledger import ROW_PREPARE, analysis_identity
        service = self.refusing_service()
        snapshot = self.snapshot()
        service._analyze_one(snapshot, self.record())
        prepares = [r for r in self.ledger.rows()
                    if r.get("kind") == ROW_PREPARE]
        self.assertEqual(len(prepares), 1)
        self.assertTrue(self.ledger.prepare_is_durable(
            analysis_identity(snapshot.market_snapshot_id)))
