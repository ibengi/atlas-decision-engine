# -*- coding: utf-8 -*-
"""The eleven findings Astra's re-audit of `a304adb` returned as REJECTED.

Each class below names one finding, states the counterexample Astra supplied,
and asserts the behaviour that closes it. Every one of these tests FAILED
against `a304adb` before the corresponding fix -- that is the point of the
file, and the reproductions are kept rather than replaced by the fix so a
regression reintroduces a named, dated counterexample instead of an anonymous
failure.

WHAT THIS FILE DOES NOT DO
    It does not revisit AA-01, AA-04, AA-05, AA-06, AA-07, AA-08, AA-09 or
    AA-18, which the re-audit passed. Their tests live in
    `test_astra_aa01_aa18_remediation.py` and are untouched; a fix here that
    weakened one of them would be caught there.
"""
import json
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                   # noqa: E402
from _candidate import (DROP, EXECUTION_BOOK, raw_market,      # noqa: E402
                        valid_candidate, valid_record)

import candidate_contract as contract                          # noqa: E402
import research_feed                                           # noqa: E402
from candidate_contract import (ContractError, FEED_SCHEMA,     # noqa: E402
                                validate_record)
from config import CFG                                         # noqa: E402


# ════════════════════════════════════════════════════════════════════════
# NEW-01 — a malformed number escapes the contract as an exception
# ════════════════════════════════════════════════════════════════════════
class NEW01_MalformedNumbersRaiseInsteadOfFailingClosed(AlphaCase):
    """`10**500` is a perfectly ordinary thing for a hostile or broken feed to
    send. `float(10**500)` raises `OverflowError`, and `_check_book` caught
    only `KeyError, TypeError, ValueError` -- so the contract, whose entire
    job is to return a structured refusal, raised instead.

    That is worse than accepting the value. An exception out of
    `validate_record` propagates through `SpoolConsumer.pending()`, which
    means one malformed row again stops the whole batch -- the AA-09 failure,
    reintroduced through a different door.
    """

    HOSTILE = 10 ** 500

    def test_validate_record_returns_errors_rather_than_raising(self):
        record = dict(valid_record(), yes_ask=self.HOSTILE)
        errors = validate_record(record)          # must not raise
        self.assertTrue(errors)
        self.assertTrue(any("yes_ask" in e for e in errors), errors)

    def test_every_numeric_field_survives_the_same_input(self):
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask", "volume",
                      "open_interest"):
            with self.subTest(field=field):
                record = dict(valid_record(), **{field: self.HOSTILE})
                errors = validate_record(record)
                self.assertTrue(any(field in e for e in errors), errors)

    def test_strict_number_itself_refuses_it(self):
        with self.assertRaises(ContractError):
            contract.strict_number(self.HOSTILE, field="yes_ask")

    def test_the_readiness_gate_returns_a_structured_refusal(self):
        import alpha_feed_readiness as readiness
        verdict = readiness.assess_record(
            dict(valid_record(), yes_ask=self.HOSTILE))
        self.assertIs(verdict["ready"], False)
        self.assertTrue(verdict["contract_errors"])

    def test_validate_record_is_total_for_any_internal_failure(self):
        """The wrapper, tested on its own.

        Found by the mutation probe: removing the wrapper's fallback still
        left every test above passing, because `_check_book` had ALSO been
        fixed to use `strict_number`, so this particular input no longer
        reaches it. The wrapper is defence in depth and was only being
        exercised through the one hole it was added to cover -- which is not
        a test of the wrapper, it is a second test of the hole.

        So: make an internal helper raise something the contract has no
        opinion about, and require a structured refusal anyway. A validator
        that can raise is a validator that can take a batch down with it.
        """
        record = valid_record()          # built BEFORE anything is patched
        for boom in (OverflowError("int too large"), RecursionError("deep"),
                     MemoryError(), ZeroDivisionError("nope")):
            with self.subTest(error=type(boom).__name__):
                with patch.object(contract, "_check_quote_observation",
                                  side_effect=boom):
                    errors = validate_record(record)
                self.assertTrue(errors)
                self.assertTrue(
                    any(type(boom).__name__ in e for e in errors), errors)
                self.assertTrue(any("refused" in e for e in errors), errors)

    def test_a_record_that_cannot_be_canonicalized_is_refused_not_raised(self):
        """The digest path has its own total guarantee."""
        class _Hostile:
            def __repr__(self):
                return "<unserializable>"

        errors = validate_record(dict(valid_record(), volume=_Hostile()))
        self.assertTrue(errors)

    def test_one_hostile_row_does_not_stop_the_batch(self):
        """The AA-09 property, re-asserted against this input."""
        from alpha_consumer import ProcessedStore, SpoolConsumer
        good = valid_record()
        hostile = dict(valid_record(market=raw_market(ticker="KX-HOSTILE")),
                       yes_ask=self.HOSTILE)

        class _Source:
            directory = None

            def records(self):
                return [hostile, good]

        consumer = SpoolConsumer(
            source=_Source(),
            store=ProcessedStore(path=os.path.join(self._tmp, "p.jsonl")))
        pending = consumer.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(consumer.stats["malformed"], 1)
        self.assertEqual(pending[0][1]["contract_id"], good["contract_id"])


# ════════════════════════════════════════════════════════════════════════
# AA-02 — numeric settlement-source members are coerced into names
# ════════════════════════════════════════════════════════════════════════
class AA02_NumericSettlementMembersAreCoercedToText(AlphaCase):
    """`{"name": 12345}` became the settlement source `"12345"`.

    `str(name).strip()` ran before anything checked that `name` was text, so
    an integer, a float, or a number the exchange used as an internal id
    turned into a plausible-looking settlement authority string. A
    calibration ledger that records `"12345"` as the body which settles a
    contract is recording a fact nobody published.

    The rule is the same one the rest of the contract already follows:
    validate the TYPE, then read the value. Never coerce first.
    """

    def emit(self, sources):
        return research_feed._settlement_source_name(sources)

    def test_a_numeric_name_is_not_a_settlement_source(self):
        for bad in (12345, 12.5, 0, -1):
            with self.subTest(value=bad):
                self.assertIsNone(self.emit([{"name": bad}]),
                                  f"{bad!r} was coerced into a source name")

    def test_a_numeric_url_is_not_a_settlement_source(self):
        self.assertIsNone(self.emit([{"url": 8080}]))

    def test_a_bare_numeric_member_taints_the_collection(self):
        self.assertIsNone(self.emit([{"name": "CF Benchmarks RTI"}, 42]))

    def test_a_numeric_scalar_is_not_a_settlement_source(self):
        self.assertIsNone(self.emit(12345))
        self.assertIsNone(self.emit(12.5))

    def test_a_genuine_published_name_still_reads(self):
        self.assertEqual(self.emit([{"name": "CF Benchmarks RTI"}]),
                         "CF Benchmarks RTI")

    def test_the_producer_refuses_the_whole_candidate(self):
        """End to end: a numeric settlement member must not reach the spool
        with the number wearing a string's clothes."""
        market = raw_market(settlement_sources=[{"name": 12345}])
        candidate = valid_candidate(market)
        self.assertIsNone(candidate["resolution_source"])
        self.assertIn("resolution_source", candidate["unavailable_fields"])

    def test_a_falsy_name_does_not_silently_fall_through_to_url(self):
        """`value.get("name") or value.get("url")` read the URL whenever the
        name was falsy -- including the empty string, which is a DIFFERENT
        fact from "no name key at all"."""
        self.assertIsNone(self.emit([{"name": "", "url": "http://x"}]))


# ════════════════════════════════════════════════════════════════════════
# AA-03 — a contradiction was downgraded to an ordinary absence
# ════════════════════════════════════════════════════════════════════════
class AA03_ContradictionWasDowngradedToAbsence(AlphaCase):
    """`resolve_alias` correctly REFUSED to choose between `event_ticker=A`
    and `event_id=B`. The caller then wrote the field into
    `unavailable_fields` and carried on.

    That is the same word for two different facts:

        the exchange did not publish this            (absence)
        the exchange published two incompatible       (contradiction)
        answers and we cannot tell which is true

    The first is a quiet market. The second is a source that contradicts
    itself, and a feed which reports it as the first has destroyed the only
    signal an operator could act on. Worse, `event_id` is OPTIONAL: a
    contradiction on an optional field became an absence, the absence was
    permitted, and the record was emitted as if the source had been silent.

    So a contradiction is now recorded as a contradiction, travels inside the
    checksum, and REFUSES the record.
    """

    def contradictory_market(self):
        return raw_market(event_ticker="EV-ALPHA", event_id="EV-BETA")

    def test_the_contradiction_is_not_reported_as_an_absence(self):
        candidate = valid_candidate(self.contradictory_market())
        self.assertNotIn("event_id", candidate["unavailable_fields"],
                         "a contradiction was filed as an ordinary absence")

    def test_both_conflicting_values_are_preserved_by_name(self):
        candidate = valid_candidate(self.contradictory_market())
        clash = candidate["contradictory_fields"]["event_id"]
        self.assertEqual(clash["event_ticker"], "EV-ALPHA")
        self.assertEqual(clash["event_id"], "EV-BETA")

    def test_the_record_is_refused_even_though_the_field_is_optional(self):
        feed = research_feed.ResearchFeed(start_writer=False)
        record = feed._build(valid_candidate(self.contradictory_market()))
        self.assertIsNone(record, "a self-contradictory source was emitted")
        self.assertTrue(any("contradict" in e for e in feed.last_errors),
                        feed.last_errors)

    def test_a_contradiction_on_a_required_field_is_also_refused(self):
        market = self.contradictory_market()
        market["expected_expiration_time"] = "2026-09-12T10:00:00+00:00"
        market["expiration_time"] = "2026-09-12T18:00:00+00:00"
        candidate = valid_candidate(market)
        self.assertIn("expected_resolution_time_utc",
                      candidate["contradictory_fields"])
        feed = research_feed.ResearchFeed(start_writer=False)
        self.assertIsNone(feed._build(candidate))

    def test_the_contract_refuses_a_record_carrying_a_contradiction(self):
        """Asserted on the CONSUMER side too: a record that reached the spool
        before this rule existed must not mint a snapshot now."""
        record = dict(valid_record(),
                      contradictory_fields={"event_id": {"event_ticker": "A",
                                                         "event_id": "B"}})
        record["record_sha256"] = contract.compute_checksum(record)
        errors = validate_record(record)
        self.assertTrue(any("contradict" in e for e in errors), errors)

    def test_the_contradiction_is_inside_the_checksum(self):
        record = dict(valid_record(),
                      contradictory_fields={"event_id": {"a": "1", "b": "2"}})
        self.assertNotEqual(contract.compute_checksum(record),
                            record["record_sha256"])

    def test_an_agreeing_alias_pair_is_not_a_contradiction(self):
        market = raw_market(event_ticker="EV-SAME", event_id="EV-SAME")
        candidate = valid_candidate(market)
        self.assertEqual(candidate["contradictory_fields"], {})
        self.assertIsNotNone(
            research_feed.ResearchFeed(start_writer=False)._build(candidate))

    def test_a_genuine_absence_is_still_an_absence(self):
        candidate = valid_candidate(raw_market(event_ticker=DROP))
        self.assertIn("event_id", candidate["unavailable_fields"])
        self.assertEqual(candidate["contradictory_fields"], {})


# ════════════════════════════════════════════════════════════════════════
# AA-10 — research LOGGING was still synchronous in the engine cycle
# ════════════════════════════════════════════════════════════════════════
class _BlockingHandler(__import__("logging").Handler):
    """A log handler that behaves like a file handler on a stalled volume.

    `hold` is deliberately SMALL. The property under test is "the engine
    thread does not wait on the handler at all", and 50 emits against a
    0.25s handler is 12.5s against a 2s budget -- decisive either way. A
    30-second hold proves exactly the same thing while making the mutation
    probe, which runs this class against deliberately broken code, take
    twenty minutes for a single mutation.

    RA-15 (v4): the gate used to be called `self.release`, which SHADOWS
    `logging.Handler.release` -- the method `Handler.handle` calls to drop
    the handler lock after `emit`. So every emit through this handler raised
    `TypeError: 'Event' object is not callable` AFTER appending the record,
    which killed the research writer thread and surfaced only as a pytest
    warning. The assertions still held, for the wrong reason. It is named
    `let_go` now, and the handler it stands in for is a real one again.
    """

    def __init__(self, hold: float = 0.25):
        super().__init__()
        self.hold = hold
        self.entered = __import__("threading").Event()
        self.let_go = __import__("threading").Event()
        self.records = []

    def emit(self, record):
        self.records.append(record)
        self.entered.set()
        self.let_go.wait(self.hold)


class AA10_ResearchLoggingStillBlockedTheCycle(AlphaCase):
    """The v2 remediation moved `write`, `fsync` and `prune` onto a writer
    thread and then left `log.info`, `log.warning` and `log.debug` on the
    CALLER'S thread.

    A log call is not free. The research logger's handler is a file handler
    on the same volume the fsync was moved off, and `logging.Handler.emit`
    holds a lock and writes synchronously. So the exact failure AA-10
    describes -- a stalled volume stalling the decision cycle -- survived the
    fix that was supposed to close it, through the diagnostics rather than
    through the data.

    Diagnostics now go through the SAME bounded, non-blocking mechanism as
    the records: offered to the writer, dropped under pressure, emitted on
    the writer's thread.
    """

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()
        self.handler = _BlockingHandler()
        logging = __import__("logging")
        self.logger = logging.getLogger("RESEARCH_FEED")
        self.logger.addHandler(self.handler)
        previous = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(self.logger.setLevel, previous)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(self.handler.let_go.set)

    def feed(self):
        from research_spool import BoundedSpool, ResearchWriter
        spool = BoundedSpool(os.path.join(self._tmp, "spool"),
                             max_records=100, max_bytes=10 ** 7,
                             max_record_bytes=10 ** 6, max_age_s=3600)
        writer = ResearchWriter(spool, start=False)     # no writer thread
        self.addCleanup(writer.stop)
        return research_feed.ResearchFeed(writer=writer)

    def test_a_refused_candidate_logs_nothing_on_the_caller_thread(self):
        """The refusal path is the one that logged the most."""
        feed = self.feed()
        started = time.time()
        for _ in range(50):
            feed.emit_candidate(valid_candidate(
                raw_market(rules_primary=DROP)))
        self.assertLess(time.time() - started, 2.0,
                        "the engine thread waited on a log handler")
        self.assertEqual(self.handler.records, [],
                         "the engine thread emitted a log record itself")

    def test_a_dropped_candidate_logs_nothing_on_the_caller_thread(self):
        feed = self.feed()
        started = time.time()
        for _ in range(50):
            feed.emit_candidate("not a dict at all")
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(self.handler.records, [])

    def test_an_accepted_candidate_logs_nothing_on_the_caller_thread(self):
        feed = self.feed()
        for _ in range(10):
            feed.emit_candidate(valid_candidate())
        self.assertEqual(self.handler.records, [])

    def test_the_diagnostics_are_not_lost_they_are_deferred(self):
        """Non-blocking must not mean silent: the writer thread emits them."""
        feed = self.feed()
        feed.emit_candidate(valid_candidate(raw_market(rules_primary=DROP)))
        self.handler.let_go.set()            # let the handler run freely
        feed.writer.start()
        self.addCleanup(feed.writer.stop)
        self.assertTrue(feed.writer.drain(timeout=5))
        deadline = time.time() + 2
        while time.time() < deadline and not self.handler.records:
            time.sleep(0.01)
        self.assertTrue(self.handler.records,
                        "the refusal was silently discarded")

    def test_no_logging_call_is_reachable_from_the_emit_path(self):
        """Static, because a timing test can only prove the calls that ran."""
        import ast
        tree = ast.parse(open("research_feed.py", encoding="utf-8").read())
        # RA-03 split the producer in two: `_admit` is what the observer's
        # thread runs, `_build`/`_finalize` are the writer's. Both halves stay
        # on this list -- the property AA-10 pins is that NOTHING on the path
        # from the engine to the queue logs, and keeping the writer-side
        # functions here as well is strictly stronger, not weaker.
        emit_path = {"emit_candidate", "_admit", "_build", "_finalize",
                     "_finalize_record", "_size", "candidate_from_market",
                     "_settlement_source_name", "settlement_source_identity",
                     "settlement_source_comparator", "_identity_text",
                     "render_settlement_source", "_escape_identity",
                     "observed_cents", "_diagnostic"}
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef,)):
                continue
            if node.name not in emit_path:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and \
                        isinstance(inner.func, ast.Attribute) and \
                        isinstance(inner.func.value, ast.Name) and \
                        inner.func.value.id == "log":
                    offences.append(f"{node.name}:{inner.lineno} "
                                    f"log.{inner.func.attr}()")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_diagnostics_are_bounded_and_drop_rather_than_grow(self):
        # This case is about the BOUND, not about blocking, so the handler is
        # let go first: a mutation that logs synchronously should fail the
        # timing cases above, not spend an hour here.
        self.handler.let_go.set()
        feed = self.feed()
        for _ in range(2000):
            feed.emit_candidate(valid_candidate(raw_market(title=DROP)))
        telemetry = feed.writer.telemetry()
        self.assertLessEqual(telemetry["queue_depth"],
                             feed.writer.max_queue)
        self.assertGreater(telemetry["dropped_queue_full"]
                           + telemetry["dropped_notes"], 0)


# ════════════════════════════════════════════════════════════════════════
# AA-11 — the spool bound did not account for partial writes
# ════════════════════════════════════════════════════════════════════════
class AA11_PartialWritesEscapedTheBound(AlphaCase):
    """Three separate holes, all reachable from one crash.

    COUNT     `len(records) >= max_records` counted COMPLETE files only.
              Partial files were counted in the byte budget and nowhere in
              the record budget, so N interrupted writes let the spool hold
              `max_records + N` files. A bound with a hole in it that opens
              exactly when writes are being interrupted is not a bound.

    RECOVERY  `recover_temp_files()` existed and nothing ever called it, and
              it only removed partials OLDER than `max_age_s` -- six hours by
              default. So a crash left its partials occupying the budget for
              six hours, which is the window in which the next crash happens.

    RESERVE   `scan -> decide -> create` was three steps with no lock. Two
              writers both saw one free slot and both took it. Capacity that
              is checked but not RESERVED is capacity that is double-spent.

    Recovery removes only files this producer owns -- our suffix AND a dead
    owner. An unknown file on a shared volume is never touched, and neither
    is a partial whose owner is still alive.
    """

    def spool(self, **over):
        from research_spool import BoundedSpool
        kw = dict(max_records=3, max_bytes=10 ** 6,
                  max_record_bytes=10 ** 5, max_age_s=3600)
        kw.update(over)
        directory = os.path.join(self._tmp, "spool")
        os.makedirs(directory, exist_ok=True)
        return BoundedSpool(directory, **kw)

    def partial(self, spool, name, *, pid, size=128):
        from research_spool import TEMP_SUFFIX
        path = os.path.join(spool.directory, f"{name}.{pid}{TEMP_SUFFIX}")
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def record_names(self, spool):
        return sorted(n for n in os.listdir(spool.directory)
                      if n.endswith(".json"))

    # ── COUNT ───────────────────────────────────────────────────────────
    def test_partial_files_count_against_the_record_limit(self):
        spool = self.spool(max_records=2)
        self.assertTrue(spool.write(valid_record(raw_market(ticker="KX-1"))))
        self.partial(spool, "inflight", pid=os.getpid())
        refused = spool.write(valid_record(raw_market(ticker="KX-2")))
        self.assertFalse(refused,
                         "a partial write did not occupy a record slot")
        self.assertEqual(spool.stats["dropped_full"], 1)

    def test_the_capacity_report_names_complete_partial_and_bytes(self):
        spool = self.spool()
        spool.write(valid_record())
        self.partial(spool, "inflight", pid=os.getpid(), size=64)
        capacity = spool.capacity()
        self.assertEqual(capacity["records"], 1)
        self.assertEqual(capacity["partials"], 1)
        self.assertEqual(capacity["partial_bytes"], 64)
        self.assertGreater(capacity["used_bytes"], 64)
        self.assertEqual(capacity["occupied"],
                         capacity["records"] + capacity["partials"])

    # ── RECOVERY ────────────────────────────────────────────────────────
    def test_a_partial_from_a_dead_owner_is_recovered_at_startup(self):
        spool = self.spool()
        dead = self.partial(spool, "crashed", pid=self.dead_pid())
        recovered = spool.recover_owned_partials()
        self.assertEqual(recovered, 1)
        self.assertFalse(os.path.exists(dead),
                         "a crashed write held a slot after restart")

    def test_recovery_does_not_wait_for_the_age_bound(self):
        """The old rule freed the slot six hours after the crash."""
        spool = self.spool(max_age_s=6 * 3600)
        dead = self.partial(spool, "crashed", pid=self.dead_pid())
        os.utime(dead, None)                       # brand new, not aged out
        self.assertEqual(spool.recover_owned_partials(), 1)

    def test_a_partial_whose_owner_is_alive_is_left_alone(self):
        spool = self.spool()
        mine = self.partial(spool, "inflight", pid=os.getpid())
        self.assertEqual(spool.recover_owned_partials(), 0)
        self.assertTrue(os.path.exists(mine))

    def test_an_unknown_file_is_never_removed(self):
        spool = self.spool()
        stranger = os.path.join(spool.directory, "someone-elses.dat")
        with open(stranger, "wb") as fh:
            fh.write(b"not ours")
        spool.recover_owned_partials()
        spool.prune()
        self.assertTrue(os.path.exists(stranger),
                        "the spool deleted a file it does not own")

    def test_the_producer_recovers_at_startup_without_being_asked(self):
        from research_spool import BoundedSpool
        directory = os.path.join(self._tmp, "spool2")
        os.makedirs(directory)
        seed = BoundedSpool(directory, max_records=3, max_bytes=10 ** 6,
                            max_record_bytes=10 ** 5, max_age_s=3600)
        dead = self.partial(seed, "crashed", pid=self.dead_pid())
        with patch.object(CFG, "RESEARCH_FEED_ENABLED", True), \
                patch.object(CFG, "DATA_DIR", self._tmp):
            feed = research_feed.ResearchFeed(directory=directory,
                                              start_writer=False)
            self.addCleanup(feed.writer.stop)
        self.assertFalse(os.path.exists(dead))

    # ── RESERVE ─────────────────────────────────────────────────────────
    def test_two_writers_cannot_both_take_the_last_slot(self):
        import threading
        spool_a = self.spool(max_records=1)
        from research_spool import BoundedSpool
        spool_b = BoundedSpool(spool_a.directory, max_records=1,
                               max_bytes=10 ** 6, max_record_bytes=10 ** 5,
                               max_age_s=3600)
        start = threading.Barrier(2)
        results = {}

        def attempt(name, spool, ticker):
            start.wait()
            results[name] = spool.write(
                valid_record(raw_market(ticker=ticker)))

        threads = [threading.Thread(target=attempt, args=("a", spool_a, "KX-A")),
                   threading.Thread(target=attempt, args=("b", spool_b, "KX-B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(sum(1 for v in results.values() if v), 1,
                         f"both writers took the last slot: {results}")
        self.assertEqual(len(self.record_names(spool_a)), 1)

    def test_an_unopenable_reservation_lock_fails_closed_without_raising(self):
        """`write()` promises never to raise for an expected condition.

        Found by self-review: `exclusive_lock` is a generator-based context
        manager, so calling it runs nothing and every failure it can have
        surfaces at `__enter__`. A `try/except` wrapped around the CALL was
        dead code, and an unopenable sidecar would have escaped `write()`.
        """
        spool = self.spool()
        real_open = os.open

        def refuse_the_lock(path, *a, **kw):
            if str(path).endswith(".lock"):
                raise OSError(13, "Permission denied")
            return real_open(path, *a, **kw)

        with patch("durable_append.os.open", side_effect=refuse_the_lock):
            self.assertFalse(spool.write(valid_record()))
        self.assertEqual(spool.stats["capacity_unknown"], 1)
        self.assertEqual(self.record_names(spool), [])

    def test_capacity_still_fails_closed_when_it_cannot_be_established(self):
        spool = self.spool()
        with patch("os.listdir", side_effect=OSError("volume gone")):
            self.assertFalse(spool.write(valid_record()))
        self.assertEqual(spool.stats["capacity_unknown"], 1)

    @staticmethod
    def dead_pid() -> int:
        """A pid that is certainly not running: fork a child and reap it."""
        pid = os.fork()
        if pid == 0:                                       # pragma: no cover
            os._exit(0)
        os.waitpid(pid, 0)
        return pid


# ════════════════════════════════════════════════════════════════════════
# AA-12 — the processed store never got the durable append protocol
# ════════════════════════════════════════════════════════════════════════
class AA12_ProcessedStoreBypassedTheDurableProtocol(AlphaCase):
    """`durable_append` was written for AA-12 and the ledger was taught to use
    it. `ProcessedStore.mark` was not: it kept its own `os.open` +
    `write_all` + `fsync`, with no torn-tail separation and no lock.

    The consequence is specific and bad. If a previous mark was interrupted,
    the file does not end in a newline. Appending directly splices the new
    row onto the broken one, and BOTH are lost: the fragment stops being
    recoverable evidence that a write was attempted, and the new mark is
    unparseable. The store then reports the snapshot as unprocessed, so the
    service pays a second time for an analysis it may already have committed.

    `_load()` made it worse by treating a torn last line as a clean
    end-of-file and returning silently, where the ledger reports it.
    """

    def store(self, name="processed.jsonl"):
        from alpha_consumer import ProcessedStore
        return ProcessedStore(path=os.path.join(self._tmp, name))

    def lines(self, store):
        with open(store.path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_a_torn_tail_is_separated_not_spliced(self):
        store = self.store()
        store.mark("snap-1", "ANALYZED")
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write('{"market_snapshot_id": "snap-2", "stat')   # crash
        store.mark("snap-3", "ANALYZED")

        lines = self.lines(store)
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("snap-2", lines[1])
        self.assertTrue(lines[1].endswith('"stat'),
                        "the damaged fragment was altered")
        self.assertEqual(json.loads(lines[2])["market_snapshot_id"], "snap-3")

    def test_the_new_mark_survives_a_torn_tail(self):
        store = self.store()
        with open(store.path, "w", encoding="utf-8") as fh:
            fh.write('{"market_snapshot_id": "torn"')
        store.mark("snap-after", "ANALYZED")
        self.assertEqual(self.store().status("snap-after"), "ANALYZED")

    def test_a_torn_tail_is_reported_rather_than_read_as_eof(self):
        store = self.store()
        store.mark("snap-1", "ANALYZED")
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write('{"market_snapshot_id": "snap-2"')
        with self.assertLogs("ALPHA", level="ERROR") as logs:
            reopened = self.store()
            reopened.status("snap-1")
        self.assertTrue(any("torn" in line.lower() for line in logs.output),
                        logs.output)

    def test_history_before_the_tear_is_still_readable(self):
        store = self.store()
        for i in range(3):
            store.mark(f"snap-{i}", "ANALYZED")
        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write('{"market_snapshot_id": "snap-torn"')
        reopened = self.store()
        for i in range(3):
            self.assertTrue(reopened.seen(f"snap-{i}"))

    def test_it_uses_the_shared_durable_append(self):
        """Static: one protocol, one implementation. Two copies of a
        durability rule is one copy that will be fixed and one that will
        not."""
        import ast
        tree = ast.parse(open("alpha_consumer.py", encoding="utf-8").read())
        mark = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "mark")
        calls = {getattr(n.func, "attr", getattr(n.func, "id", ""))
                 for n in ast.walk(mark) if isinstance(n, ast.Call)}
        self.assertIn("serialized_append", calls,
                      "the processed store still hand-rolls its own append")
        # Nothing hand-rolled survives beside it: no direct descriptor work,
        # no private fsync, no second opinion about durability.
        self.assertFalse(calls & {"fsync", "open", "write_all"}, calls)


# ════════════════════════════════════════════════════════════════════════
# AA-13 — readable bytes were treated as a durable commit
# ════════════════════════════════════════════════════════════════════════
class AA13_ReadableBytesWereTreatedAsCommitted(AlphaCase):
    """`prediction_is_committed()` answered by READING the ledger back and
    finding the row.

    That conflates two different things. `write()` returning means the bytes
    reached the kernel's page cache -- from which they read back perfectly.
    `fsync()` returning means they reached the device. If the fsync fails,
    `record_prediction` raises and the caller believes the prediction was
    lost, while `prediction_is_committed` reads the very same bytes and says
    it is safe. The service then published a TERMINAL acknowledgement for a
    prediction that may exist nowhere but in a cache about to be discarded.

    A commit is now proven by a RECEIPT -- a COMMIT row appended after the
    prediction and fsynced in its own right -- not by the readability of the
    row it describes.
    """

    def ledger(self):
        from alpha_ledger import AlphaLedger
        return AlphaLedger(path=os.path.join(self._tmp, "ledger.jsonl"),
                           cost_path=os.path.join(self._tmp, "cost.jsonl"))

    def prediction(self, pid="p-1", snapshot="snap-1"):
        return {"prediction_id": pid, "market_snapshot_id": snapshot,
                "contract_id": "KX-1", "p_yes": 0.6, "executed": False}

    def test_a_failed_fsync_is_not_a_commit_even_though_bytes_are_readable(self):
        from alpha_ledger import LedgerError
        ledger = self.ledger()
        real_fsync = os.fsync

        def failing_fsync(fd):
            raise OSError(5, "I/O error")

        with patch("durable_append.os.fsync", failing_fsync):
            with self.assertRaises(LedgerError):
                ledger.record_prediction(self.prediction())
        # The bytes ARE readable -- that is the whole point of the finding.
        self.assertTrue(any(r.get("prediction_id") == "p-1"
                            for r in ledger.rows()))
        # ... and they are still NOT a commit.
        self.assertFalse(ledger.prediction_is_committed("snap-1"),
                         "readable bytes were accepted as a durable commit")
        os.fsync = real_fsync

    def test_a_healthy_append_does_commit(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        self.assertTrue(ledger.prediction_is_committed("snap-1"))

    def test_the_commit_receipt_is_its_own_row(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        kinds = [r["kind"] for r in ledger.rows()]
        self.assertEqual(kinds, ["PREDICTION", "COMMIT"])

    def test_a_prediction_without_its_receipt_is_not_committed(self):
        """Hand-edited file: the row is there, the receipt is not."""
        ledger = self.ledger()
        ledger.record_prediction(self.prediction())
        rows = [r for r in ledger.rows() if r["kind"] != "COMMIT"]
        with open(ledger.log.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        self.assertFalse(self.ledger().prediction_is_committed("snap-1"))

    def test_a_failed_fsync_does_not_poison_the_snapshot_forever(self):
        """The receipt must not turn one bad fsync into a permanent refusal.

        Found by self-review of this very fix: with the receipt required and
        the duplicate check unchanged, a prediction row whose fsync failed was
        still FOUND by the duplicate check, so every retry was refused as
        "already committed" while `prediction_is_committed` said False. The
        snapshot could then never be committed and never be retried.

        The recovery is to FINISH the commit rather than start another: the
        receipt's own append fsyncs the whole file, so the earlier row becomes
        durable at the same moment its receipt does. One prediction, one
        identity, and the ORIGINAL id survives -- a second id would name a row
        nobody can join to.
        """
        from alpha_ledger import LedgerError
        ledger = self.ledger()
        with patch("durable_append.os.fsync",
                   side_effect=OSError(5, "I/O error")):
            with self.assertRaises(LedgerError):
                ledger.record_prediction(self.prediction("p-first", "snap-1"))
        self.assertFalse(ledger.prediction_is_committed("snap-1"))

        row = ledger.record_prediction(self.prediction("p-second", "snap-1"))
        self.assertEqual(row["prediction_id"], "p-first",
                         "the retry wrote a second identity")
        self.assertTrue(ledger.prediction_is_committed("snap-1"))
        self.assertEqual(len(ledger.predictions()), 1)

    def test_a_genuinely_committed_analysis_is_still_refused_twice(self):
        """Anti-vacuity: the recovery above must not become a way to record
        one snapshot twice."""
        from alpha_ledger import LedgerError
        ledger = self.ledger()
        ledger.record_prediction(self.prediction("p-1", "snap-1"))
        with self.assertRaises(LedgerError):
            ledger.record_prediction(self.prediction("p-2", "snap-1"))

    def test_a_receipt_for_a_different_prediction_does_not_count(self):
        ledger = self.ledger()
        ledger.record_prediction(self.prediction("p-1", "snap-1"))
        self.assertFalse(ledger.prediction_is_committed("snap-OTHER"))


# ════════════════════════════════════════════════════════════════════════
# AA-14 — only two of the six appenders were serialized
# ════════════════════════════════════════════════════════════════════════
class AA14_UnserializedAppendersAndStaleCaches(AlphaCase):
    """`record_prediction` and `resolve` took the writer lock. `invalidate`,
    `record_observation` and `record_costs` did not, and neither did the
    processed store.

    Every one of them is a check-then-append:

        invalidate         "is it already invalidated? no -> append"
        record_observation "is this interval already sampled? no -> append"
        record_costs       appends N rows, each of which reads the tail
        mark               reads the tail to decide whether to separate it

    Two writers interleave between the check and the append and both write.
    The result is a duplicate invalidation, a duplicated observation interval
    that skews the latency-decay series, or a spliced row. Being append-only
    does not make a writer safe; it makes the damage permanent.

    The cache was the other half: `ProcessedStore._cache` was filled once and
    never invalidated, so one writer's marks stayed invisible to another for
    the life of the process.
    """

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self._tmp, "ledger.jsonl")
        self.cost_path = os.path.join(self._tmp, "cost.jsonl")

    def ledger(self):
        from alpha_ledger import AlphaLedger
        return AlphaLedger(path=self.path, cost_path=self.cost_path)

    def seed(self, ledger, pid="p-1", snapshot="snap-1"):
        ledger.record_prediction({"prediction_id": pid,
                                  "market_snapshot_id": snapshot,
                                  "contract_id": "KX-1"})

    def race(self, worker, count=8):
        import threading
        barrier = threading.Barrier(count)
        errors = []

        def run(index):
            try:
                barrier.wait(timeout=20)
                worker(index)
            except Exception as exc:                          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,))
                   for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return errors

    def test_racing_invalidations_write_exactly_one_row(self):
        self.seed(self.ledger())
        self.race(lambda i: self.ledger().invalidate("p-1", "catalyst_occurred",
                                                     f"writer {i}"))
        rows = [r for r in self.ledger().rows() if r["kind"] == "INVALIDATION"]
        self.assertEqual(len(rows), 1, rows)

    def test_racing_observations_write_exactly_one_row_per_interval(self):
        self.seed(self.ledger())
        self.race(lambda i: self.ledger().record_observation(
            "p-1", interval_s=300, quote={"yes_bid": 0.4}))
        rows = [r for r in self.ledger().rows() if r["kind"] == "OBSERVATION"]
        self.assertEqual(len(rows), 1, rows)

    def test_racing_cost_writers_lose_no_row_and_tear_none(self):
        from alpha_ledger import AlphaLedger, _AppendOnlyLog
        log = _AppendOnlyLog(self.cost_path)
        self.race(lambda i: log.append({"kind": "COST", "writer": i,
                                        "padding": "x" * 4000}), count=8)
        with open(self.cost_path, encoding="utf-8") as fh:
            lines = [x for x in fh.read().splitlines() if x.strip()]
        self.assertEqual(len(lines), 8, f"{len(lines)} lines, expected 8")
        for line in lines:
            json.loads(line)                    # every row intact

    def test_racing_processed_marks_lose_nothing(self):
        from alpha_consumer import ProcessedStore
        path = os.path.join(self._tmp, "processed.jsonl")
        self.race(lambda i: ProcessedStore(path=path).mark(
            f"snap-{i}", "ANALYZED"), count=8)
        store = ProcessedStore(path=path)
        for i in range(8):
            self.assertTrue(store.seen(f"snap-{i}"), f"snap-{i} lost")

    def test_a_warm_cache_notices_another_writers_mark(self):
        from alpha_consumer import ProcessedStore
        path = os.path.join(self._tmp, "processed.jsonl")
        reader = ProcessedStore(path=path)
        self.assertFalse(reader.seen("snap-elsewhere"))     # cache warms here
        ProcessedStore(path=path).mark("snap-elsewhere", "ANALYZED")
        self.assertTrue(reader.seen("snap-elsewhere"),
                        "the cache hid another writer's mark")

    def test_a_warm_ledger_cache_notices_another_writers_row(self):
        ledger = self.ledger()
        self.assertEqual(ledger.rows(), [])                 # warms any cache
        self.seed(self.ledger(), pid="p-late", snapshot="snap-late")
        self.assertTrue(ledger.prediction_is_committed("snap-late"),
                        "the ledger cache hid another writer's prediction")

    def test_every_appender_is_serialized(self):
        """Static: each method that appends to a shared file must reach the
        serialized helper, so a new appender cannot quietly skip the lock."""
        import ast
        tree = ast.parse(open("alpha_ledger.py", encoding="utf-8").read())
        appenders = {"record_costs", "record_prediction", "resolve",
                     "invalidate", "record_observation", "prepare"}
        unserialized = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or \
                    node.name not in appenders:
                continue
            names = {getattr(n.func, "attr", getattr(n.func, "id", ""))
                     for n in ast.walk(node) if isinstance(n, ast.Call)}
            if not names & {"lock", "serialized_append"}:
                unserialized.append(node.name)
        self.assertEqual(unserialized, [],
                         f"unserialized appenders: {unserialized}")


# ════════════════════════════════════════════════════════════════════════
# AA-13 (2) — restart re-dispatched work that was already committed
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


class AA13b_RestartPaidTwiceForACommittedAnalysis(AlphaCase):
    """A crash between the prediction commit and the processed mark is the
    ordinary case, not the exotic one -- they are two files.

    On the next poll the snapshot was still unacknowledged, so the service
    minted it, DISPATCHED IT TO EVERY PROVIDER AGAIN, and only then asked the
    ledger to record it. The ledger refused correctly -- one analysis, one
    prediction -- but by then the money was spent and the prediction_id the
    service reported was the NEW one, which names a prediction that was never
    written. The acknowledgement therefore pointed at nothing.

    Recovery now happens BEFORE dispatch: the committed prediction is looked
    up by its stable analysis identity, its own id is returned, and no
    provider is called.
    """

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "ALPHA_GATEWAY_ENABLED", True))
        self._patches[-1].start()

    def service(self, providers):
        from alpha_consumer import ProcessedStore, SpoolConsumer
        from alpha_ledger import AlphaLedger
        from alpha_service import AlphaShadowService
        from alpha_cost import BudgetGuard
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.store = ProcessedStore(path=os.path.join(self._tmp, "p.jsonl"))

        class _Empty:
            directory = None

            def records(self):
                return []

        from _alpha import write_pricing
        from alpha_cost import BudgetLedger, PricingTable
        pricing = PricingTable(write_pricing(
            os.path.join(self._tmp, "pricing.json"),
            models=[p.name for p in providers]))
        return AlphaShadowService(
            providers=providers, ledger=self.ledger,
            consumer=SpoolConsumer(source=_Empty(), store=self.store),
            budget=BudgetGuard(pricing=pricing, ledger=BudgetLedger(
                os.path.join(self._tmp, "budget.jsonl"))),
            quote_fn=lambda: {"yes_bid": 0.44, "yes_ask": 0.46,
                              "no_bid": 0.54, "no_ask": 0.56})

    def test_a_committed_analysis_is_recovered_without_re_dispatch(self):
        provider = _CountingProvider()
        service = self.service([provider])
        snapshot = self.snapshot()
        self.ledger.record_prediction({
            "prediction_id": "p-already-committed",
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id})

        result = service._analyze_one(snapshot, valid_record())

        self.assertEqual(provider.calls, 0,
                         "the providers were paid twice for one analysis")
        self.assertEqual(result["prediction_id"], "p-already-committed")
        self.assertFalse(result["deferred"])

    def test_the_recovered_analysis_is_acknowledged_with_the_committed_id(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        self.ledger.record_prediction({
            "prediction_id": "p-already-committed",
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id})
        service._analyze_one(snapshot, valid_record())
        row = self.store._load()[snapshot.market_snapshot_id]
        self.assertEqual(row["status"], "ANALYZED")
        self.assertEqual(row["prediction_id"], "p-already-committed")

    def test_no_second_prediction_row_is_written(self):
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        self.ledger.record_prediction({
            "prediction_id": "p-already-committed",
            "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_id": snapshot.contract_id})
        service._analyze_one(snapshot, valid_record())
        self.assertEqual(len(self.ledger.predictions()), 1)

    def test_an_uncommitted_snapshot_is_still_analysed_normally(self):
        """Anti-vacuity: recovery must not swallow genuinely new work."""
        provider = _CountingProvider()
        service = self.service([provider])
        service._analyze_one(self.snapshot(), valid_record())
        self.assertGreater(provider.calls, 0)


# ════════════════════════════════════════════════════════════════════════
# AA-13 (3) — dispatch proceeded without a durable PREPARE
# ════════════════════════════════════════════════════════════════════════
class AA13c_PrepareFailureDidNotStopDispatch(AlphaCase):
    """PREPARE is what makes a crash mid-analysis distinguishable from work
    that never started. The service wrote it, caught every exception, called
    the failure "not fatal", and dispatched anyway.

    So the one case PREPARE exists for -- the ledger is not writable -- is
    exactly the case in which the service spent money on providers and had
    nowhere durable to record that it had. Without a durable PREPARE the
    subsequent prediction cannot be committed either, so the spend is
    guaranteed to be unrecoverable before it is incurred.

    PREPARE is now a PRECONDITION: no durable PREPARE, no dispatch, and the
    snapshot is DEFERRED so a later poll retries it.
    """

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "ALPHA_GATEWAY_ENABLED", True))
        self._patches[-1].start()

    def service(self, providers):
        from alpha_consumer import ProcessedStore, SpoolConsumer
        from alpha_ledger import AlphaLedger
        from alpha_service import AlphaShadowService
        from alpha_cost import BudgetGuard
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.store = ProcessedStore(path=os.path.join(self._tmp, "p.jsonl"))

        class _Empty:
            directory = None

            def records(self):
                return []

        from _alpha import write_pricing
        from alpha_cost import BudgetLedger, PricingTable
        pricing = PricingTable(write_pricing(
            os.path.join(self._tmp, "pricing.json"),
            models=[p.name for p in providers]))
        return AlphaShadowService(
            providers=providers, ledger=self.ledger,
            consumer=SpoolConsumer(source=_Empty(), store=self.store),
            budget=BudgetGuard(pricing=pricing, ledger=BudgetLedger(
                os.path.join(self._tmp, "budget.jsonl"))),
            quote_fn=lambda: {"yes_bid": 0.44, "yes_ask": 0.46,
                              "no_bid": 0.54, "no_ask": 0.56})

    def test_no_durable_prepare_means_no_provider_is_called(self):
        from alpha_ledger import LedgerError
        provider = _CountingProvider()
        service = self.service([provider])
        with patch.object(type(self.ledger), "prepare",
                          side_effect=LedgerError("read-only filesystem")):
            result = service._analyze_one(self.snapshot(), valid_record())
        self.assertEqual(provider.calls, 0,
                         "providers were paid with nowhere to record it")
        self.assertTrue(result["deferred"])

    def test_the_snapshot_is_deferred_not_acknowledged(self):
        from alpha_ledger import LedgerError
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        with patch.object(type(self.ledger), "prepare",
                          side_effect=LedgerError("read-only filesystem")):
            service._analyze_one(snapshot, valid_record())
        row = self.store._load()[snapshot.market_snapshot_id]
        self.assertEqual(row["status"], "DEFERRED")

    def test_no_prediction_id_is_acknowledged_for_an_uncommitted_analysis(self):
        """Correction 8, second half: an acknowledgement must never name a
        prediction id that was not durably committed."""
        from alpha_ledger import LedgerError
        service = self.service([_CountingProvider()])
        snapshot = self.snapshot()
        with patch.object(type(self.ledger), "prepare",
                          side_effect=LedgerError("read-only filesystem")):
            service._analyze_one(snapshot, valid_record())
        row = self.store._load()[snapshot.market_snapshot_id]
        self.assertEqual(row["prediction_id"], "",
                         "an uncommitted prediction id was acknowledged")

    def test_a_healthy_prepare_still_dispatches(self):
        provider = _CountingProvider()
        service = self.service([provider])
        service._analyze_one(self.snapshot(), valid_record())
        self.assertGreater(provider.calls, 0)
        self.assertEqual([r["kind"] for r in self.ledger.rows()][0], "PREPARE")


# ════════════════════════════════════════════════════════════════════════
# AA-15 — the "verified join" verified only what it was given
# ════════════════════════════════════════════════════════════════════════
class AA15_PartialBindingWasAcceptedAsVerified(AlphaCase):
    """The v2 remediation checked every binding field the settlement SUPPLIED
    and said so plainly: "fields the settlement does not supply are simply
    not checked".

    Written out, that rule is: a settlement supplying no binding at all
    passes every check there is. `prediction_id` alone was enough -- and a
    prediction_id is an opaque token, so matching it proves somebody quoted a
    token, not that this settlement describes that market. The resolution was
    then stored with `binding_verified: False` and nothing downstream looked
    at the flag.

    A binding is now COMPLETE or it is not a binding. The required fields
    must all be present and must all agree; a settlement missing any of them
    is quarantined with the missing names reported, rather than resolved on
    the strength of the fields it happened to include.
    """

    def setUp(self):
        super().setUp()
        from alpha_ledger import AlphaLedger
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.record = valid_record()
        self.binding = {
            "contract_id": "KX-1",
            "market_snapshot_id": "snap-1",
            "record_sha256": self.record["record_sha256"],
            "contract_schema": self.record["schema"],
            "environment": "test",
            "digest_verified": True,
        }
        self.ledger.record_prediction({
            "prediction_id": "p-1", "market_snapshot_id": "snap-1",
            "contract_id": "KX-1", "source_binding": dict(self.binding)})

    def settlement(self, **over):
        row = {"prediction_id": "p-1", "outcome": 1,
               "source": "CF Benchmarks RTI",
               "contract_id": "KX-1", "market_snapshot_id": "snap-1",
               "source_record_sha256": self.record["record_sha256"]}
        row.update(over)
        return {k: v for k, v in row.items() if v is not DROP}

    def ingest(self, rows, **kw):
        from alpha_resolution_ingest import ingest_settlements
        kw.setdefault("trusted_sources", ["CF Benchmarks RTI"])
        return ingest_settlements(self.ledger, rows, **kw)

    def test_a_settlement_with_no_binding_is_quarantined(self):
        result = self.ingest([{"prediction_id": "p-1", "outcome": 1,
                               "source": "CF Benchmarks RTI"}])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertIsNone(self.ledger.find_resolution("p-1"))

    def test_each_required_binding_field_is_individually_required(self):
        for field in ("contract_id", "market_snapshot_id",
                      "source_record_sha256"):
            with self.subTest(missing=field):
                self.setUp()
                result = self.ingest([self.settlement(**{field: DROP})])
                self.assertEqual(result["appended"], 0)
                self.assertEqual(len(result["quarantined"]), 1)
                self.assertIn(field,
                              result["quarantined"][0]["missing_binding"])

    def test_the_quarantine_names_what_is_missing(self):
        result = self.ingest([self.settlement(source_record_sha256=DROP)])
        entry = result["quarantined"][0]
        self.assertEqual(entry["prediction_id"], "p-1")
        self.assertEqual(entry["missing_binding"], ["source_record_sha256"])

    def test_a_complete_binding_still_resolves(self):
        """Anti-vacuity: the strict rule must not refuse everything."""
        result = self.ingest([self.settlement()])
        self.assertEqual(result["appended"], 1)
        self.assertTrue(self.ledger.find_resolution("p-1"))

    def test_a_complete_but_disagreeing_binding_is_still_rejected(self):
        result = self.ingest([self.settlement(contract_id="KX-OTHER")])
        self.assertEqual(result["appended"], 0)
        self.assertEqual(len(result["binding_mismatches"]), 1)

    def test_a_prediction_with_no_binding_cannot_be_settled(self):
        """The other direction: if the PREDICTION carries no binding there is
        nothing to corroborate against, and a settlement must not be
        accepted on the strength of an id alone."""
        self.ledger.record_prediction({"prediction_id": "p-bare",
                                       "market_snapshot_id": "snap-bare"})
        result = self.ingest([{"prediction_id": "p-bare", "outcome": 1,
                               "source": "CF Benchmarks RTI",
                               "contract_id": "KX-1",
                               "market_snapshot_id": "snap-bare",
                               "source_record_sha256": "a" * 64}])
        self.assertEqual(result["appended"], 0)


# ════════════════════════════════════════════════════════════════════════
# AA-15 (2) — an unqualified settlement source was trusted by default
# ════════════════════════════════════════════════════════════════════════
class AA15b_UnqualifiedSourcesWereTrustedByDefault(AlphaCase):
    """`trusted_sources` was OFF by default, and off meant "accept anything".

    The report said so honestly -- "it is off by default because no
    settlement authority has been qualified" -- but the behaviour that
    followed from it is the opposite of what the sentence describes. An
    unqualified authority is not a reason to accept every string; it is a
    reason to accept none of them.

    Default is now REFUSE. An operator states which feed they verified, and
    that statement is preserved into the learning rows, so a calibration
    number can be traced back to the authority that produced its outcomes.
    """

    def setUp(self):
        super().setUp()
        from alpha_ledger import AlphaLedger
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.ledger.record_prediction({
            "prediction_id": "p-1", "market_snapshot_id": "snap-1",
            "contract_id": "KX-1", "p_yes": 0.6,
            "source_binding": {"contract_id": "KX-1",
                               "market_snapshot_id": "snap-1",
                               "record_sha256": "d" * 64}})

    def settlement(self, source="CF Benchmarks RTI"):
        return {"prediction_id": "p-1", "outcome": 1, "source": source,
                "contract_id": "KX-1", "market_snapshot_id": "snap-1",
                "source_record_sha256": "d" * 64}

    def test_an_unqualified_source_is_refused_by_default(self):
        from alpha_resolution_ingest import ingest_settlements
        result = ingest_settlements(self.ledger, [self.settlement()])
        self.assertEqual(result["appended"], 0)
        self.assertTrue(result["rejected"])
        self.assertIsNone(self.ledger.find_resolution("p-1"))

    def test_the_refusal_says_no_authority_has_been_qualified(self):
        from alpha_resolution_ingest import ingest_settlements
        result = ingest_settlements(self.ledger, [self.settlement()])
        self.assertIn("qualified", result["rejected"][0]["reason"])

    def test_a_named_trusted_source_is_accepted(self):
        from alpha_resolution_ingest import ingest_settlements
        result = ingest_settlements(
            self.ledger, [self.settlement()],
            trusted_sources=["CF Benchmarks RTI"])
        self.assertEqual(result["appended"], 1)

    def test_a_source_outside_the_allow_list_is_still_refused(self):
        from alpha_resolution_ingest import ingest_settlements
        result = ingest_settlements(
            self.ledger, [self.settlement(source="some blog")],
            trusted_sources=["CF Benchmarks RTI"])
        self.assertEqual(result["appended"], 0)

    def test_trust_metadata_reaches_the_learning_rows(self):
        from alpha_resolution_ingest import ingest_settlements
        ingest_settlements(self.ledger, [self.settlement()],
                           trusted_sources=["CF Benchmarks RTI"])
        row = self.ledger.resolved()[0]
        self.assertEqual(row["resolution_source"], "CF Benchmarks RTI")
        self.assertIs(row["binding_verified"], True)
        self.assertIs(row["source_trusted"], True)
        self.assertEqual(row["settlement_binding"]["contract_id"], "KX-1")


# ════════════════════════════════════════════════════════════════════════
# AA-15 (3) — the digest outlived the evidence it was a digest of
# ════════════════════════════════════════════════════════════════════════
class AA15c_SourceDigestCouldNotBeReverifiedAfterPruning(AlphaCase):
    """The prediction row carried `record_sha256` and nothing else about the
    source.

    A digest is a claim ABOUT some bytes. The bytes lived in the spool, and
    the spool is BOUNDED -- pruned by age and by size, on purpose, so
    research can never fill the volume the money path needs. Six hours after
    a prediction, the evidence its digest describes is gone, and the digest
    becomes a 64-character string that nothing can check.

    That matters exactly when it is most needed: a settlement arriving days
    later supplies `source_record_sha256`, the binding check compares it to
    the stored string, and both sides could be wrong together with no way to
    tell. Comparing two copies of an unverifiable claim is not verification.

    The canonical source content is therefore persisted WITH the prediction,
    so the digest can be recomputed from evidence that is still present.
    """

    def setUp(self):
        super().setUp()
        from alpha_ledger import AlphaLedger
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))
        self.record = valid_record()

    def binding(self):
        from alpha_service import source_binding_for
        return source_binding_for(self.record,
                                  contract_id=self.record["contract_id"],
                                  market_snapshot_id="snap-1",
                                  digest_verified=True)

    def test_the_binding_carries_the_canonical_source_evidence(self):
        binding = self.binding()
        self.assertIn("source_evidence", binding)
        self.assertEqual(binding["source_evidence"]["contract_id"],
                         self.record["contract_id"])

    def test_the_digest_is_recomputable_from_what_was_persisted(self):
        from alpha_ledger import verify_source_evidence
        self.ledger.record_prediction({
            "prediction_id": "p-1", "market_snapshot_id": "snap-1",
            "contract_id": self.record["contract_id"],
            "source_binding": self.binding()})
        # The spool is gone; nothing of the original bytes survives outside
        # the prediction row.
        verdict = verify_source_evidence(self.ledger.find_prediction("p-1"))
        self.assertIs(verdict["verified"], True)
        self.assertEqual(verdict["recomputed"], self.record["record_sha256"])

    def test_an_edited_evidence_blob_fails_its_own_digest(self):
        from alpha_ledger import verify_source_evidence
        binding = self.binding()
        binding["source_evidence"]["question"] = "a different question"
        self.ledger.record_prediction({
            "prediction_id": "p-1", "market_snapshot_id": "snap-1",
            "source_binding": binding})
        verdict = verify_source_evidence(self.ledger.find_prediction("p-1"))
        self.assertIs(verdict["verified"], False)
        self.assertIn("mismatch", verdict["reason"])

    def test_a_prediction_with_no_evidence_says_so_rather_than_passing(self):
        from alpha_ledger import verify_source_evidence
        self.ledger.record_prediction({"prediction_id": "p-bare"})
        verdict = verify_source_evidence(self.ledger.find_prediction("p-bare"))
        self.assertIs(verdict["verified"], False)
        self.assertIn("no source evidence", verdict["reason"])

    def test_the_evidence_is_a_copy_not_a_reference(self):
        """A later mutation of the record must not reach the ledger row."""
        binding = self.binding()
        self.record["question"] = "mutated after the fact"
        self.assertNotEqual(binding["source_evidence"]["question"],
                            "mutated after the fact")


# ════════════════════════════════════════════════════════════════════════
# AA-16 — report protection guarded a path the store does not use
# ════════════════════════════════════════════════════════════════════════
class AA16_ProtectionMissedTheConfiguredProcessedPath(AlphaCase):
    """The ledgers were protected by their real paths, taken from the ledger
    object. The processed store was protected by a GUESS:
    `os.path.join(directory, CFG.ALPHA_STATE_FILE)`.

    `ProcessedStore` resolves its path as `_p(CFG.ALPHA_STATE_FILE)`, i.e.
    against `CFG.DATA_DIR`, and the report's `directory` argument is not
    required to be `DATA_DIR`. Configure a nondefault relative state file, or
    publish the report anywhere other than the data directory, and the
    protected path is a file nobody writes -- while the file the service
    actually appends its processed marks to is left unguarded, and a report
    published over it destroys the record of every analysis already paid for.

    Protection now follows the ACTUAL configured path, and the store's own
    path when a store is available, instead of reconstructing a guess.
    """

    def setUp(self):
        super().setUp()
        from alpha_ledger import AlphaLedger
        self.ledger = AlphaLedger(
            path=os.path.join(self._tmp, "ledger.jsonl"),
            cost_path=os.path.join(self._tmp, "cost.jsonl"))

    def publish(self, directory, filename, **kw):
        from alpha_learning_runtime import write_learning_report
        return write_learning_report(self.ledger, directory,
                                     filename=filename, **kw)

    def test_a_nondefault_relative_state_path_is_protected(self):
        from alpha_consumer import ProcessedStore
        nested = os.path.join("state", "alpha_processed.jsonl")
        with patch.object(CFG, "ALPHA_STATE_FILE", nested), \
                patch.object(CFG, "DATA_DIR", self._tmp):
            store = ProcessedStore()
            store.mark("snap-1", "ANALYZED")
            before = open(store.path, "rb").read()
            reports = os.path.join(self._tmp, "reports")
            os.makedirs(reports, exist_ok=True)
            with self.assertRaises(ValueError):
                self.publish(reports, os.path.relpath(store.path, reports))
            self.assertEqual(open(store.path, "rb").read(), before)

    def test_the_report_directory_need_not_be_the_data_directory(self):
        from alpha_consumer import ProcessedStore
        with patch.object(CFG, "DATA_DIR", self._tmp):
            store = ProcessedStore()
            store.mark("snap-1", "ANALYZED")
            before = open(store.path, "rb").read()
            elsewhere = os.path.join(self._tmp, "elsewhere")
            os.makedirs(elsewhere, exist_ok=True)
            with self.assertRaises(ValueError):
                self.publish(elsewhere, os.path.relpath(store.path, elsewhere))
            self.assertEqual(open(store.path, "rb").read(), before)

    def test_a_stores_own_path_is_protected_when_it_is_supplied(self):
        from alpha_consumer import ProcessedStore
        store = ProcessedStore(path=os.path.join(self._tmp, "custom.jsonl"))
        store.mark("snap-1", "ANALYZED")
        before = open(store.path, "rb").read()
        with self.assertRaises(ValueError):
            self.publish(self._tmp, "custom.jsonl", processed_store=store)
        self.assertEqual(open(store.path, "rb").read(), before)

    def test_an_ordinary_report_still_publishes(self):
        with patch.object(CFG, "DATA_DIR", self._tmp):
            self.publish(self._tmp, "alpha_learning_report.json")
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp, "alpha_learning_report.json")))


# ════════════════════════════════════════════════════════════════════════
# M07P — a COMPLETE record whose quotes are declared derived
# ════════════════════════════════════════════════════════════════════════
class M07P_CompleteRecordWithDerivedQuotes(AlphaCase):
    """M07 asked what happens when the quote-observation check is DELETED.
    M07P asks the harder question: with the check intact, what happens to a
    record that is complete, correctly checksummed, correctly attributed --
    and simply says, truthfully, that its quotes were derived?

    That is not a hypothetical. `MarketValidator.normalize_book` derives a
    missing NO side for the order path on purpose, and it is right to: the
    order path needs a complete book to price against. The record describing
    that book is well-formed in every respect. It is just not evidence.

    The assertion is by EFFECT, not by counter. A counter proves a branch
    ran; what matters is that NOTHING was produced -- no snapshot minted, no
    prediction row, no commit receipt, no processed acknowledgement. A
    derived quote that reaches the calibration ledger is indistinguishable
    from an observed one forever after, and every number computed from it is
    unfalsifiable.
    """

    def derived_record(self, count=4):
        """A complete, checksum-correct record declaring `count` quotes
        derived. Everything else about it is impeccable."""
        record = dict(valid_record())
        fields = list(contract.QUOTE_FIELDS)[:count]
        record["quote_observation"] = {
            f: (contract.QUOTE_DERIVED if f in fields
                else contract.QUOTE_OBSERVED)
            for f in contract.QUOTE_FIELDS}
        record["record_sha256"] = contract.compute_checksum(record)
        return record

    def consume(self, record):
        from alpha_consumer import ProcessedStore, SpoolConsumer

        class _Source:
            directory = None

            def records(self):
                return [record]

        consumer = SpoolConsumer(
            source=_Source(),
            store=ProcessedStore(path=os.path.join(self._tmp, "p.jsonl")))
        return consumer, consumer.pending()

    def test_the_record_is_otherwise_impeccable(self):
        """Anti-vacuity: it must fail for the RIGHT reason, and for only
        that reason."""
        errors = validate_record(self.derived_record())
        self.assertTrue(errors)
        for error in errors:
            self.assertIn("not directly observed", error, errors)

    def test_zero_snapshots_are_minted(self):
        consumer, pending = self.consume(self.derived_record())
        self.assertEqual(pending, [])
        self.assertEqual(consumer.stats["minted"], 0)
        self.assertEqual(consumer.stats["derived_quotes"], 1)

    def test_one_derived_quote_is_enough(self):
        for count in (1, 2, 3, 4):
            with self.subTest(derived=count):
                consumer, pending = self.consume(self.derived_record(count))
                self.assertEqual(pending, [])
                self.assertEqual(consumer.stats["minted"], 0)

    def test_zero_durable_predictions_are_written(self):
        from alpha_ledger import AlphaLedger
        ledger = AlphaLedger(path=os.path.join(self._tmp, "ledger.jsonl"),
                             cost_path=os.path.join(self._tmp, "cost.jsonl"))
        _consumer, pending = self.consume(self.derived_record())
        self.assertEqual(pending, [])
        self.assertEqual(ledger.predictions(), [])
        self.assertEqual(ledger.commits(), {})
        self.assertEqual([r for r in ledger.rows()], [])

    def test_the_producer_will_not_emit_it_either(self):
        """Closed at both ends: the producer refuses to build it from a raw
        market whose quotes only the execution book carries."""
        raw = raw_market(yes_bid=DROP, yes_ask=DROP, no_bid=DROP, no_ask=DROP)
        candidate = valid_candidate(raw, EXECUTION_BOOK)
        self.assertEqual(
            set(candidate["quote_observation"].values()),
            {contract.QUOTE_DERIVED})
        feed = research_feed.ResearchFeed(start_writer=False)
        self.addCleanup(feed.writer.stop)
        self.assertIsNone(feed._build(candidate))
        self.assertEqual(feed.refused_derived, 1)

    def test_the_readiness_gate_refuses_it_too(self):
        import alpha_feed_readiness as readiness
        verdict = readiness.assess_record(self.derived_record())
        self.assertIs(verdict["ready"], False)

    def test_a_fully_observed_record_still_mints(self):
        """Anti-vacuity: the rule must not refuse everything."""
        consumer, pending = self.consume(valid_record())
        self.assertEqual(len(pending), 1)
        self.assertEqual(consumer.stats["minted"], 1)
