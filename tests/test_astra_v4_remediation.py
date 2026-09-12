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
