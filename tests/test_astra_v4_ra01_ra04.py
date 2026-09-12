# -*- coding: utf-8 -*-
"""Astra v4 COUNTER-audit V4-RA-01..V4-RA-04, reproduced before they closed.

SHADOW ONLY. Nothing here places an order, touches CAPITAL, or imports a
broker module. The two cases that drive `ExecutionEngine._shadow_observer` and
`MarketOpportunityPipeline` do so through the same read-only harness
`tests/test_btc_daily_evidence.py` already uses: no client, no risk engine, no
order manager, and a decision that is already frozen before the observer sees
it.

WHY THESE TESTS LOOK LIKE THIS
    Each case below was written from the counter-audit's own counterexample
    and run against the PINNED BASE (`57d4975`) first. The reproductions are
    kept rather than replaced by tests of the fix, because a test that only
    describes the fix cannot tell you whether the fix addressed the defect.

    The assertions are on what ends up in the record, the digest, the spool
    and the engine's wall-clock -- never on a counter or a log line, which is
    the class of assertion that let two safety mutations survive 1,670 tests
    before AA-17.

    The four findings overlap on purpose and the tests do not pretend
    otherwise: V4-RA-01 is about what a source container is ALLOWED to be,
    and V4-RA-02 is about what survives once it is allowed. A single
    malformed member is therefore asserted twice -- once as a refusal, once
    as an identity that cannot collide.
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402  (repo root onto sys.path)

import research_feed                                          # noqa: E402
import research_spool                                         # noqa: E402
from _alpha import AlphaCase                                  # noqa: E402
from _candidate import DROP, raw_market, valid_candidate      # noqa: E402
from config import CFG                                        # noqa: E402
from research_feed import (MalformedSettlementSource,          # noqa: E402
                           ResearchFeed, candidate_from_market,
                           render_settlement_source,
                           settlement_source_comparator,
                           settlement_source_identity)


def record_for(market):
    """The record the producer would SPOOL for `market`, or None.

    Goes through the real producer, so a refusal here is a refusal before
    spool and prediction publication -- which is what V4-RA-01 asks be
    proved. `start_writer=False` keeps the writer thread out of it; the cases
    that care about the spool bytes start a real one.
    """
    feed = ResearchFeed(start_writer=False)
    return feed._build(candidate_from_market(market, {}, raw_book=market,
                                             cycle_id="v4"))


class _StalledHandler(logging.Handler):
    """A log handler that behaves like a file handler on a stalled volume.

    `hold` is small on purpose: the property under test is "the engine thread
    does not wait on the handler AT ALL", and 40 emits against a 0.25s
    handler is 10s measured against a 2s budget -- decisive either way, and
    not so slow that the mutation probe takes twenty minutes per mutation.

    Named `let_go` rather than `release`, because `release` SHADOWS
    `logging.Handler.release` -- the method `Handler.handle` calls to drop the
    handler lock after `emit`. RA-15 (v4) is the finding for that; the same
    trap is avoided here rather than rediscovered.
    """

    def __init__(self, hold: float = 0.25):
        super().__init__()
        self.hold = hold
        self.let_go = threading.Event()
        self.records = []

    def emit(self, record):
        self.records.append(record)
        self.let_go.wait(self.hold)


class _HostileRepr:
    """A source value whose RENDERING is not safe to perform.

    The counter-audit's witness for V4-RA-03: `__repr__` raising is how a
    malformed alias made research normalization raise on the engine's thread.
    `__eq__` returns False so the value reaches the contradiction path rather
    than comparing equal to its sibling alias and being resolved quietly.
    """

    def __repr__(self):
        raise RuntimeError("this value cannot be rendered")

    def __eq__(self, other):
        return False

    __hash__ = None


class _HugeRepr:
    """A source value whose rendering is not BOUNDED."""

    def __init__(self, size=40_000_000):
        self.size = size

    def __repr__(self):
        return "x" * self.size

    def __eq__(self, other):
        return False

    __hash__ = None


# ════════════════════════════════════════════════════════════════════════
# V4-RA-01 — the source container was normalized before it was validated
# ════════════════════════════════════════════════════════════════════════
class V4RA01_TheSourceObjectHadNoSchema(AlphaCase):
    """`settlement_source_identity` validated only the keys it UNDERSTOOD.

    RA-01 closed the early return: with `name` present, `url` is examined
    too. What it did not close is everything that is NEITHER. The loop was

        for key in SOURCE_IDENTITY_KEYS:
            if key not in value: continue
            ...

    so a key the producer has no definition for was skipped in silence, a
    nested list was FLATTENED into its parent by `members.extend(...)`, and an
    empty nested member contributed nothing and therefore disappeared. All
    three end the same way: a container that was only partly accounted for
    became fully-fledged canonical evidence.

    Against the pinned base:

        {"name": "CF Benchmarks RTI", "source_id": 7}
            -> the authority "CF Benchmarks RTI", record PUBLISHED
        [[{"name": "A"}], {"name": "B"}]
            -> "A | B", record PUBLISHED, grouping gone
        [{"name": "A"}, []]
            -> "A", record PUBLISHED, the empty member gone
    """

    def test_an_unsupported_companion_key_makes_the_member_malformed(self):
        for member in ({"name": "CF Benchmarks RTI", "source_id": 7},
                       {"name": "CF Benchmarks RTI", "settlement_method": "x"},
                       {"name": "CF Benchmarks RTI", "url": "https://x",
                        "ticker": "T"},
                       {"url": "https://x", "unknown": None},
                       {"name": "A", "": "blank key"}):
            with self.subTest(member=member):
                market = raw_market(settlement_sources=[member])
                self.assertIsNone(
                    valid_candidate(market)["resolution_source"],
                    f"{member!r} was normalized into a settlement authority "
                    f"with the key this producer cannot read discarded")
                self.assertIsNone(
                    record_for(market),
                    "a record was published from a container the producer "
                    "only partly validated")

    def test_the_unsupported_key_is_named_in_the_refusal(self):
        """An operator has to be able to see WHICH key was not understood."""
        with self.assertRaises(MalformedSettlementSource) as caught:
            settlement_source_identity({"name": "CF", "source_id": 7})
        self.assertIn("source_id", str(caught.exception))

    def test_a_nested_collection_member_is_refused_not_flattened(self):
        for hostile in ([[{"name": "A"}], {"name": "B"}],
                        [[[{"name": "A"}, {"name": "B"}]]],
                        [{"name": "A"}, ({"name": "B"},)],
                        [[{"name": "A"}]]):
            with self.subTest(container=hostile):
                market = raw_market(settlement_sources=hostile)
                self.assertIsNone(
                    valid_candidate(market)["resolution_source"],
                    f"{hostile!r} was flattened into a flat authority list, "
                    f"so the grouping the exchange published stopped "
                    f"existing")
                self.assertIsNone(record_for(market))

    def test_an_empty_nested_member_is_refused_not_dropped(self):
        for hostile in ([{"name": "A"}, []],
                        [[], {"name": "A"}],
                        [{"name": "A"}, ()],
                        [{"name": "A"}, [[]]]):
            with self.subTest(container=hostile):
                market = raw_market(settlement_sources=hostile)
                self.assertIsNone(
                    valid_candidate(market)["resolution_source"],
                    f"the empty member of {hostile!r} disappeared and the "
                    f"rest was published as a complete reading")
                self.assertIsNone(record_for(market))

    def test_a_member_with_no_identity_at_all_is_still_refused(self):
        """The AA-02 control, restated: `{}` is not an empty source."""
        for hostile in ([{}], [{"name": None}], [{"name": None, "url": None}]):
            with self.subTest(container=hostile):
                self.assertIsNone(
                    valid_candidate(raw_market(
                        settlement_sources=hostile))["resolution_source"])

    def test_an_empty_top_level_collection_is_absent_not_a_source(self):
        """The ONE input that is validly empty, stated so the rule is falsifiable.

        An empty top-level list is how a feed says "this market has no
        settlement sources". That reads as ABSENT rather than malformed -- and
        `resolution_source` is REQUIRED, so the record is still refused. What
        must never happen is the third outcome: a source appearing out of it.
        """
        for empty in ([], ()):
            with self.subTest(container=empty):
                market = raw_market(settlement_sources=empty)
                candidate = valid_candidate(market)
                self.assertIsNone(candidate["resolution_source"])
                self.assertIn("resolution_source",
                              candidate["unavailable_fields"])
                self.assertIsNone(record_for(market))

    def test_the_previously_accepted_witnesses_never_reach_the_spool(self):
        """V4-RA-01's required proof, against a REAL spool directory.

        Not `_build() is None`: the finding asks that these be refused before
        spool publication, so this drives the whole producer -- admission,
        the writer thread, the contract walk and the spool -- and then counts
        the bytes on disk.
        """
        with patch.object(CFG, "RESEARCH_FEED_ENABLED", True):
            directory = os.path.join(self._tmp, "spool")
            spool = research_spool.BoundedSpool(
                directory, max_records=100, max_bytes=10 ** 7,
                max_record_bytes=10 ** 6, max_age_s=3600)
            writer = research_spool.ResearchWriter(spool, start=False)
            feed = ResearchFeed(writer=writer)
            self.addCleanup(writer.stop)

            witnesses = (
                [{"name": "CF Benchmarks RTI", "source_id": 7}],
                [{"name": "CF Benchmarks RTI", "url": 8080}],
                [[{"name": "A"}], {"name": "B"}],
                [{"name": "A"}, []],
                [{"name": "A"}, {"ref": "B"}],
            )
            for index, hostile in enumerate(witnesses):
                feed.emit_candidate(candidate_from_market(
                    raw_market(ticker=f"KX-V4RA01-{index}",
                               settlement_sources=hostile),
                    {}, raw_book=raw_market(
                        ticker=f"KX-V4RA01-{index}",
                        settlement_sources=hostile)))
            writer.start()
            self.assertTrue(writer.drain(timeout=10))
            spooled = [n for n in os.listdir(directory) if n.endswith(".json")] \
                if os.path.isdir(directory) else []
            self.assertEqual(
                spooled, [],
                f"{len(spooled)} malformed settlement source(s) reached the "
                f"spool as canonical evidence")

    def test_a_valid_source_still_survives_all_of_this(self):
        """The control. A schema that refuses everything is not a schema."""
        for good, expected in (
                ([{"name": "CF Benchmarks RTI"}], "CF Benchmarks RTI"),
                ({"name": "CF Benchmarks RTI"}, "CF Benchmarks RTI"),
                ("CF Benchmarks RTI", "CF Benchmarks RTI"),
                ([{"name": "A"}, {"name": "B"}], "A | B"),
                ([{"name": "A", "url": "https://a"}],
                 "A <https://a>"),
                ([{"url": "https://a"}], "<https://a>"),
                ([{"name": "A", "url": None}], "A"),
        ):
            with self.subTest(source=good):
                market = raw_market(settlement_sources=good)
                self.assertEqual(
                    valid_candidate(market)["resolution_source"], expected)
                self.assertIsNotNone(
                    record_for(market),
                    "the schema refused a source the exchange really does "
                    "publish")


# ════════════════════════════════════════════════════════════════════════
# V4-RA-02 — information loss, not a hash collision
# ════════════════════════════════════════════════════════════════════════
class V4RA02_DistinctSourcesProducedOneRecord(AlphaCase):
    """Different raw structures produced byte-identical records.

    Measured on the pinned base, with everything but the settlement source
    held equal and the observation clock pinned:

        [{"name": "CF", "source_id": 7}]  and  [{"name": "CF", "source_id": 9}]
            -> record_sha256 86fbec9b33852b73...  BOTH
        [[{"name": "A"}], {"name": "B"}]  and  [{"name": "A"}, {"name": "B"}]
            -> record_sha256 8db6bd8bf5f8c205...  BOTH
        [{"name": "A"}, []]               and  [{"name": "A"}]
            -> record_sha256 97496b63062ce4bd...  BOTH

    That is INFORMATION LOSS upstream of the digest, not a property of
    SHA256: the two inputs were made equal before they were hashed, so no
    hash function could have told them apart. The comparator had the same
    hole, and it is the worse half -- `resolve_alias` asked it whether two
    alias keys agreed, it said yes, and a real contradiction was resolved
    silently in favour of whichever key is listed first in a tuple.
    """

    #: Structurally DISTINCT settlement sources, every one of them valid.
    #: The claim is that no two of these can ever produce one record.
    DISTINCT = (
        [{"name": "A"}],
        [{"name": "B"}],
        [{"name": "A, B"}],
        [{"name": "A"}, {"name": "B"}],
        [{"name": "B"}, {"name": "A"}],
        [{"name": "A | B"}],
        [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        [{"url": "https://a"}],
        [{"name": "A", "url": "https://a"}],
        [{"name": "A", "url": "https://b"}],
        [{"name": "A <https://a>"}],
        [{"name": "A"}, {"url": "https://a"}],
        [{"name": "A\\"}],
        [{"name": "A\\|B"}],
    )

    def test_no_two_distinct_sources_share_a_record_sha256(self):
        seen = {}
        for source in self.DISTINCT:
            market = raw_market(settlement_sources=source)
            record = record_for(market)
            self.assertIsNotNone(record, f"{source!r} was refused; this "
                                         f"corpus is meant to be valid")
            digest = record["record_sha256"]
            self.assertNotIn(
                digest, seen,
                f"{source!r} and {seen.get(digest)!r} are different source "
                f"structures and produced ONE record; the distinction was "
                f"lost before the digest, not by it")
            seen[digest] = source
        self.assertEqual(len(seen), len(self.DISTINCT))

    def test_no_two_distinct_sources_share_a_retained_field(self):
        """The digest is downstream. The FIELD must be injective too."""
        seen = {}
        for source in self.DISTINCT:
            rendered = valid_candidate(
                raw_market(settlement_sources=source))["resolution_source"]
            self.assertNotIn(rendered, seen,
                             f"{source!r} and {seen.get(rendered)!r} render "
                             f"to the same `resolution_source`")
            seen[rendered] = source

    def test_an_unknown_identity_field_can_no_longer_be_dropped(self):
        """The pair that hashed identically, asserted as a pair."""
        for left, right in (
                ([{"name": "CF", "source_id": 7}],
                 [{"name": "CF", "source_id": 9}]),
                ([{"name": "CF", "source_id": 7}], [{"name": "CF"}]),
                ([[{"name": "A"}], {"name": "B"}],
                 [{"name": "A"}, {"name": "B"}]),
                ([{"name": "A"}, []], [{"name": "A"}]),
        ):
            with self.subTest(left=left, right=right):
                one = record_for(raw_market(settlement_sources=left))
                two = record_for(raw_market(settlement_sources=right))
                if one is not None and two is not None:
                    self.assertNotEqual(
                        one["record_sha256"], two["record_sha256"],
                        f"{left!r} and {right!r} still produce one record")
                else:
                    self.assertIsNone(
                        one,
                        f"{left!r} is the malformed side and was published "
                        f"anyway")

    def test_the_compared_representation_is_the_retained_one(self):
        """V4-RA-02's actual requirement, stated as an equality.

        The comparator used to return the STRUCTURED identity while the
        record retained a text rendering of it. Two representations of one
        fact is two chances to disagree. This asserts there is now one.
        """
        for source in self.DISTINCT:
            with self.subTest(source=source):
                retained = valid_candidate(raw_market(
                    settlement_sources=source))["resolution_source"]
                self.assertEqual(
                    settlement_source_comparator(source), retained,
                    "what `resolve_alias` compares is not what the record "
                    "keeps and the digest covers")

    def test_the_retained_rendering_is_invertible(self):
        """Injectivity, as a decidable property rather than an argument.

        `render_settlement_source` claimed to be injective in its docstring.
        A claim in a docstring is not a guarantee, so there is an inverse
        now, and this runs it over the identities most likely to break it --
        the ones containing the separator and delimiter characters.
        """
        hostile = (
            "A", "A | B", "A|B", "A <x>", "<x>", "A\\", "\\|", "\\<\\>",
            " A ", "A  B", "|", "<", ">", "\\", "A\\ <B",
        )
        for name in hostile:
            for url in (None,) + hostile:
                identity = ((("name", name),) if url is None
                            else tuple(sorted((("name", name), ("url", url)))))
                with self.subTest(name=name, url=url):
                    self.assertTrue(
                        research_feed.round_trips((identity,)),
                        f"render/parse lost {identity!r}")
        # And whole collections, where the member separator is in play.
        self.assertTrue(research_feed.round_trips(
            ((("name", "A | B"),), (("name", "A"),), (("name", "B"),))))

    def test_a_nested_grouping_no_longer_agrees_with_a_flat_one(self):
        """The comparator half: a disagreement must be SEEN as one."""
        market = raw_market(
            settlement_sources=[[{"name": "A"}], {"name": "B"}],
            settlement_source=[{"name": "A"}, {"name": "B"}])
        candidate = valid_candidate(market)
        self.assertIn(
            "resolution_source", candidate["contradictory_fields"],
            "a nested grouping and a flat one compared EQUAL, so the "
            "disagreement between the two alias keys was never seen")
        self.assertIsNone(record_for(market))

    def test_unreadable_and_none_published_do_not_compare_equal(self):
        """"We cannot read this" and "there are none" are different facts."""
        self.assertNotEqual(settlement_source_comparator([]),
                            settlement_source_comparator([{"name": 12345}]))
        market = raw_market(settlement_sources=[],
                            settlement_source=[{"name": 12345}])
        self.assertIn("resolution_source",
                      valid_candidate(market)["contradictory_fields"],
                      "an empty source list and an unreadable one compared "
                      "equal, so one alias key contradicting the other was "
                      "filed as agreement")

    def test_every_malformed_container_still_compares_equal(self):
        """The guard RA-02 established, which this must not weaken.

        "We cannot read this" is ONE fact however it is misspelled, and a
        field both aliases agree is unreadable is ABSENT -- which the
        contract refuses anyway.
        """
        malformed = ([{"name": 12345}], {"name": 12345}, 123, True,
                     [{"name": "A"}, 7], [[{"name": "A"}]],
                     [{"name": "A", "source_id": 1}])
        answers = {id(settlement_source_comparator(m)) for m in malformed}
        self.assertEqual(len(answers), 1,
                         "two unreadable containers compared unequal, which "
                         "manufactures a contradiction out of one fact")

    def test_the_malformed_verdict_cannot_be_forged_by_a_url(self):
        """A marker STRING would have re-opened V4-RA-02 inside its own fix.

        A sentinel spelled `"<malformed settlement source>"` is publishable:
        a member carrying that text as its URL renders to exactly those
        characters, and a real identity would then have compared equal to an
        unreadable one.
        """
        for text in ("malformed settlement source",
                     "no settlement source published"):
            with self.subTest(text=text):
                self.assertNotEqual(
                    settlement_source_comparator([{"url": text}]),
                    settlement_source_comparator([{"name": 12345}]))
                self.assertNotEqual(
                    settlement_source_comparator([{"name": text}]),
                    settlement_source_comparator([]))

    def test_agreeing_aliases_in_different_shapes_still_agree(self):
        """RA-02's control, restated. The comparator must not invent conflicts."""
        market = raw_market(
            settlement_sources=[{"name": "CF Benchmarks RTI"}],
            settlement_source={"name": "CF Benchmarks RTI"})
        candidate = valid_candidate(market)
        self.assertEqual(candidate["contradictory_fields"], {})
        self.assertEqual(candidate["resolution_source"], "CF Benchmarks RTI")
        self.assertIsNotNone(record_for(market))


# ════════════════════════════════════════════════════════════════════════
# V4-RA-03 — the observer's own error path blocked the decision cycle
# ════════════════════════════════════════════════════════════════════════
class V4RA03_TheObserverErrorPathLoggedSynchronously(AlphaCase):
    """AA-10 moved the data path off the cycle. The ERROR path stayed on it.

        try:
            self.research_feed.emit_candidate(candidate_from_market(...))
        except Exception as e:
            log.debug(f"research feed: {e}")

    Both halves ran on the decision cycle's thread. `candidate_from_market`
    is research NORMALIZATION and it COULD raise -- not hypothetically:

      * `resolve_alias` built its refusal message with `{value!r}`, so a
        source value whose `__repr__` raises propagated out of the producer
        entirely; and
      * `strict_number` built ITS refusal with `f"{value}"`, which on
        CPython 3.11+ raises `ValueError` past 4300 digits. A plain JSON
        integer quote of `10 ** 5000` -- no hostile object required --
        therefore escaped `observed_cents`'s `except ContractError`.

    and `log.debug` then took the handler's lock and emitted SYNCHRONOUSLY.
    Measured on the pinned base against a 0.25s handler:

        20 observer calls -> 10.01s of engine cycle, 40 log records emitted
        on the engine's own thread

    A research subsystem whose error path can stall the money path has become
    part of the money path, which is the whole of AA-10.
    """

    #: Every market that used to make the producer raise on the engine thread.
    HOSTILE = (
        ("__repr__ raises, via an alias contradiction",
         lambda: raw_market(event_ticker="EV-1", event_id=_HostileRepr())),
        ("__repr__ returns 40MB",
         lambda: raw_market(event_ticker="EV-1", event_id=_HugeRepr())),
        ("a plain JSON integer quote of 10**5000",
         lambda: raw_market(yes_bid=10 ** 5000)),
        ("a plain JSON integer volume of 10**5000",
         lambda: raw_market(volume=10 ** 5000)),
        ("an unrenderable settlement-source member",
         lambda: raw_market(
             settlement_sources=[{"name": "A", "url": _HostileRepr()}],
             settlement_source=[{"name": "B"}])),
        ("an unrenderable close time",
         lambda: raw_market(close_time=_HostileRepr())),
    )

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
        return ResearchFeed(writer=writer)

    def stall(self, hold=0.25):
        """A stalled handler on BOTH loggers the observer path can reach."""
        handler = _StalledHandler(hold)
        names = ("BOT", "RESEARCH_FEED")
        for name in names:
            logger = logging.getLogger(name)
            previous = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            self.addCleanup(logger.setLevel, previous)
            self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(handler.let_go.set)
        return handler

    # ── the producer itself ─────────────────────────────────────────────
    def test_research_normalization_is_total(self):
        for label, build in self.HOSTILE:
            with self.subTest(witness=label):
                market = build()
                try:
                    candidate = candidate_from_market(market, {},
                                                      raw_book=market)
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"{label} raised {type(exc).__name__} out of "
                              f"research normalization, on the engine's own "
                              f"thread")
                self.assertIsInstance(candidate, dict)

    def test_research_normalization_is_bounded(self):
        """A 40MB rendering must not become 40MB of decision-cycle work."""
        market = raw_market(event_ticker="EV-1",
                            event_id=_HugeRepr(80_000_000))
        started = time.time()
        candidate = candidate_from_market(market, {}, raw_book=market)
        elapsed = time.time() - started
        self.assertLess(elapsed, 0.5,
                        f"the observer thread spent {elapsed:.2f}s rendering "
                        f"a value of which at most 200 characters are kept")
        kept = candidate["contradictory_fields"]["event_id"]
        self.assertTrue(all(len(v) <= 200 for v in kept.values()))

    #: Adjacent to the supplied witnesses: a container that misbehaves in a
    #: way that is NEITHER a canonical identity nor
    #: `MalformedSettlementSource`. Each of these escaped
    #: `_settlement_source_name` and `settlement_source_comparator` -- both
    #: of which catch only `MalformedSettlementSource` -- and reached the
    #: engine, even after the supplied witnesses were closed.
    HOSTILE_CONTAINERS = ("len", "iter", "getitem", "eq")

    @staticmethod
    def hostile_container(kind):
        if kind == "len":
            class _Bad(list):
                def __len__(self):
                    raise RuntimeError("len")
            return [{"name": "A"}, _Bad()]
        if kind == "iter":
            class _Bad(dict):
                def __iter__(self):
                    raise RuntimeError("iter")
            return [_Bad(name="A")]
        if kind == "getitem":
            class _Bad(dict):
                def __getitem__(self, key):
                    raise RuntimeError("getitem")
            return [_Bad(name="A")]
        class _Bad:                                           # kind == "eq"
            def __eq__(self, other):
                raise RuntimeError("eq")
            __hash__ = None
        return [{"name": "A", "url": _Bad()}]

    def test_a_container_we_cannot_even_traverse_is_malformed(self):
        for kind in self.HOSTILE_CONTAINERS:
            with self.subTest(kind=kind):
                container = self.hostile_container(kind)
                try:
                    candidate = candidate_from_market(
                        raw_market(settlement_sources=container), {},
                        raw_book=raw_market())
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"a container whose __{kind}__ raises let "
                              f"{type(exc).__name__} reach the engine")
                self.assertIsNone(candidate["resolution_source"])
                # And the comparator, which `resolve_alias` calls on the same
                # thread, must answer rather than raise.
                self.assertEqual(settlement_source_comparator(container),
                                 settlement_source_comparator(
                                     [{"name": 12345}]),
                                 "an untraversable container is not the same "
                                 "verdict as an unreadable one")

    def test_a_readable_mapping_is_still_read(self):
        """The control for the case above, and it is not a nit.

        A `dict` subclass whose `keys()` raises is a perfectly READABLE
        mapping: this producer traverses with `for key in value` and
        `value[key]`, so `keys()` is a method nobody calls. It is read, and
        that is right -- "fails closed on anything unusual" would refuse
        ordinary data, which is a different defect from the one being fixed.
        """
        class _KeysRaise(dict):
            def keys(self):
                raise RuntimeError("keys")

        self.assertEqual(
            candidate_from_market(
                raw_market(settlement_sources=[_KeysRaise(name="A")]),
                {}, raw_book=raw_market())["resolution_source"], "A")

    def test_an_oversized_identity_value_is_refused_not_rendered(self):
        """A 40MB name cost the decision cycle 3.74s on the pinned base.

        `render_settlement_source` escapes character by character, on the
        observer's thread, to build a field the spool's `max_record_bytes`
        then refuses on a different one. The bound belongs on the INPUT, and
        the value is refused rather than shortened -- a truncated authority
        name is a different authority (V4-RA-02).
        """
        for size in (research_feed.MAX_IDENTITY_CHARS + 1, 10_000_000):
            with self.subTest(size=size):
                market = raw_market(
                    settlement_sources=[{"name": "a" * size}])
                started = time.time()
                candidate = candidate_from_market(market, {},
                                                  raw_book=market)
                elapsed = time.time() - started
                self.assertIsNone(candidate["resolution_source"])
                self.assertLess(elapsed, 0.2,
                                f"the decision cycle spent {elapsed:.2f}s on "
                                f"a {size}-character identity value")
        # The control: the bound is generous, and real sources are nowhere
        # near it.
        inside = "a" * research_feed.MAX_IDENTITY_CHARS
        self.assertEqual(
            candidate_from_market(
                raw_market(settlement_sources=[{"name": inside}]), {},
                raw_book=raw_market())["resolution_source"], inside)

    def test_an_oversized_collection_is_refused(self):
        many = [{"name": f"a{i}"}
                for i in range(research_feed.MAX_SOURCE_MEMBERS + 1)]
        self.assertIsNone(candidate_from_market(
            raw_market(settlement_sources=many), {},
            raw_book=raw_market())["resolution_source"])
        ok = many[:research_feed.MAX_SOURCE_MEMBERS]
        self.assertIsNotNone(candidate_from_market(
            raw_market(settlement_sources=ok), {},
            raw_book=raw_market())["resolution_source"])

    def test_the_schema_check_itself_is_bounded(self):
        """The fix must not become the next instance of the defect.

        A first version of the unsupported-key check was
        `sorted(str(k) for k in value ...)`, which walks a mapping whose size
        the EXCHANGE chooses and calls `str` on every key -- on the observer's
        thread. Only two keys can ever be supported, so the check stops after
        a bounded number of iterations however large the object is.
        """
        class _BadKey:
            def __repr__(self):
                raise RuntimeError("key repr")

            def __hash__(self):
                return 7

            def __eq__(self, other):
                return self is other

        for label, member in (
                ("a million unsupported keys",
                 {**{f"k{i}": i for i in range(1_000_000)}, "name": "A"}),
                ("a key whose __repr__ raises",
                 {"name": "A", _BadKey(): 1}),
        ):
            with self.subTest(member=label):
                started = time.time()
                candidate = candidate_from_market(
                    raw_market(settlement_sources=[member]), {},
                    raw_book=raw_market())
                elapsed = time.time() - started
                self.assertIsNone(candidate["resolution_source"])
                self.assertLess(elapsed, 0.2,
                                f"the schema check spent {elapsed:.2f}s on "
                                f"{label}")

    def test_a_hostile_market_mapping_does_not_reach_the_engine(self):
        """The market itself can be the hostile container."""
        class _Bad(dict):
            def __getitem__(self, key):
                raise RuntimeError("getitem")

        market = _Bad(raw_market())
        try:
            candidate = candidate_from_market(market, {}, raw_book=market)
        except Exception as exc:                              # noqa: BLE001
            self.fail(f"{type(exc).__name__} reached the engine from a "
                      f"market mapping whose __getitem__ raises")
        self.assertIsNone(record_for(market),
                          "a market we could not read produced a record")

    def test_an_unreadable_field_is_refused_not_filed_as_absent(self):
        """Total must not mean lenient: the record still has to be REFUSED.

        And the refusal must not be the ABSENCE refusal. Filing "we could not
        read what the source sent" as "the source sent nothing" is the exact
        downgrade AA-03 and RA-01 both exist to prevent.
        """
        market = raw_market(event_ticker="EV-1", event_id=_HostileRepr())
        candidate = candidate_from_market(market, {}, raw_book=market)
        self.assertIn("event_id", candidate["contradictory_fields"])
        self.assertNotIn("event_id", candidate["unavailable_fields"])
        self.assertNotIn("event_id", candidate["field_provenance"])
        self.assertIsNone(record_for(market))

    # ── the engine's observer ───────────────────────────────────────────
    class _FakeShadow:
        def __init__(self):
            self.calls = []

        def record(self, **kw):
            self.calls.append(kw)

    def observer(self, feed):
        """The REAL `ExecutionEngine._shadow_observer`, bound to a stand-in.

        The idiom `tests/test_btc_daily_evidence.py::TestObserverRouting`
        already uses: the method under test is the production one, and the
        stand-in carries only what it touches. Building an `ExecutionEngine`
        would require a broker client, and a test that needs one to prove a
        timing property about research has proved something else.

        Both of the OTHER stores are real (or faithfully faked). That matters
        for the assertion, not just for tidiness: a `None` store raises, the
        hook's second `try` logs that on its own legitimate refusal path, and
        the test would then be measuring a log line this finding is not about.
        """
        from btc_daily_evidence import BtcDailyEvidenceStore
        from execution_engine import ExecutionEngine

        class _Self:
            pass

        stand_in = _Self()
        stand_in.research_feed = feed
        stand_in.shadow_store = self._FakeShadow()
        stand_in.btc_daily_evidence = BtcDailyEvidenceStore(
            os.path.join(self._tmp, "daily"))
        return stand_in, ExecutionEngine._shadow_observer.__get__(
            stand_in, ExecutionEngine)

    def test_the_observer_never_waits_on_a_log_handler(self):
        """The witness, at the observer. 0.25s x 40 against a 2s budget."""
        handler = self.stall()
        feed = self.feed()
        _stand_in, observe = self.observer(feed)

        class _Dec:
            decision_id = "cyc1-abcd1234"
            strategy = "neither_store"       # routes past both shadow stores

        class _Snap:
            def __init__(self, market):
                self.raw_market = market
                self.minutes_remaining = 10
                self.quality = None

        for label, build in self.HOSTILE:
            with self.subTest(witness=label):
                snapshot = _Snap(build())
                started = time.time()
                for _ in range(40):
                    observe(snapshot, {}, _Dec())
                elapsed = time.time() - started
                self.assertLess(
                    elapsed, 2.0,
                    f"{label}: the engine cycle waited {elapsed:.2f}s on a "
                    f"logging handler held by another thread")
        self.assertEqual(
            handler.records, [],
            f"the engine thread emitted {len(handler.records)} log record(s) "
            f"itself; research diagnostics belong on the writer's thread")

    def test_the_refusal_is_still_reported_by_the_writer(self):
        """Non-blocking must not mean silent (AA-10's own second half)."""
        handler = self.stall()
        handler.let_go.set()                 # let it run freely
        feed = self.feed()
        _stand_in, observe = self.observer(feed)

        class _Dec:
            decision_id = "cyc1-abcd1234"
            strategy = "neither_store"

        class _Snap:
            raw_market = raw_market(event_ticker="EV-1",
                                    event_id=_HostileRepr())
            minutes_remaining = 10
            quality = None

        observe(_Snap(), {}, _Dec())
        feed.writer.start()
        self.addCleanup(feed.writer.stop)
        self.assertTrue(feed.writer.drain(timeout=10))
        deadline = time.time() + 2
        while time.time() < deadline and not handler.records:
            time.sleep(0.01)
        self.assertTrue(handler.records,
                        "the refusal was dropped without ever being said")

    # ── the real pipeline ───────────────────────────────────────────────
    def test_a_real_pipeline_cycle_does_not_stall_on_research(self):
        """The pipeline-level witness V4-RA-03 asks for.

        `MarketOpportunityPipeline.run_cycle` is the real caller: it builds
        the snapshot, freezes the decision and then invokes the observer
        inside its OWN `try/except ... log.debug`. So this measures the thing
        that actually matters -- a decision cycle, end to end, against a
        stalled handler, with a market whose settlement source cannot be
        rendered.
        """
        from test_btc_daily_evidence import _Client, _Router, mkt
        from opportunity_pipeline import MarketOpportunityPipeline
        from strategy_router import GateConfig

        handler = self.stall()
        feed = self.feed()
        _stand_in, observe = self.observer(feed)

        # The hostile value sits on `event_id`, which the EXECUTION path never
        # reads and the research path resolves as an alias of `event_ticker`.
        # A hostile quote would be measuring the order path's tolerance for
        # nonsense, which is a different question from this one.
        #
        # Several markets, not one: with a single market the stall is one
        # handler hold and the WALL CLOCK cannot tell the two versions apart,
        # so the case would rest entirely on the log-record assertion. Eight
        # markets is 2s of stall on the pinned base against the same budget.
        markets = []
        for index in range(8):
            market = dict(mkt(ticker=f"KXBTCD-26AUG20-T7000{index}"))
            market["event_ticker"] = "EV-1"
            market["event_id"] = _HostileRepr()
            markets.append(market)

        seen = []

        def observer(snapshot, book, dec):
            seen.append(dec.ticker)
            observe(snapshot, book, dec)

        pipeline = MarketOpportunityPipeline(
            _Client(markets), _Router(), gates=GateConfig(),
            observer=observer, data_dir=self._tmp)
        started = time.time()
        pipeline.run_cycle(max_accepted=len(markets))
        elapsed = time.time() - started

        self.assertGreaterEqual(
            len(seen), 4,
            f"only {len(seen)} market(s) reached the observer, so this cycle "
            f"does not measure what it claims to")
        self.assertLess(elapsed, 1.0,
                        f"a decision cycle over {len(seen)} markets took "
                        f"{elapsed:.2f}s because research could not render a "
                        f"value")
        self.assertEqual(handler.records, [],
                         "the decision cycle emitted a log record itself")

    # ── static, because timing only proves the calls that ran ───────────
    def test_no_logging_call_is_reachable_from_the_engines_research_hook(self):
        import ast
        with open("execution_engine.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        hook = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_shadow_observer"), None)
        self.assertIsNotNone(hook, "this test names a function that no "
                                   "longer exists, so it proves nothing")
        # The research block is everything up to the SECOND `try` -- the
        # btc15m/btc_daily stores that follow are not this finding's subject
        # and legitimately log on their own refusal paths.
        tries = [n for n in hook.body if isinstance(n, ast.Try)]
        self.assertGreaterEqual(len(tries), 2,
                                "the hook's shape changed; re-read it")
        offences = []
        for node in ast.walk(tries[0]):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id.startswith("log"):
                offences.append(f"line {node.lineno}: "
                                f"{node.func.value.id}.{node.func.attr}()")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_producer_still_does_no_hashing_on_the_observer_thread(self):
        """RA-03's guard, re-pointed at the new entry point.

        `observe_market` is now what the engine calls, so it joins the set of
        functions that may not serialize, hash or validate. Strictly stronger
        than the RA-03 version, which could not name a function that did not
        yet exist.
        """
        import ast
        with open("research_feed.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        emit_path = {"observe_market", "note_observer_failure",
                     "emit_candidate", "_admit", "_size", "_note"}
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}
        self.assertEqual(emit_path - defined, set(),
                         "this test names functions that do not exist")
        forbidden = {"compute_checksum", "validate_record", "canonical_json",
                     "canonical_content"}
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) \
                    or node.name not in emit_path:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                name = getattr(inner.func, "id", None) or \
                    getattr(inner.func, "attr", None)
                if name in forbidden:
                    offences.append(f"{node.name}:{inner.lineno} {name}()")
                if isinstance(inner.func, ast.Attribute) \
                        and isinstance(inner.func.value, ast.Name) \
                        and inner.func.value.id == "log":
                    offences.append(f"{node.name}:{inner.lineno} "
                                    f"log.{inner.func.attr}()")
        self.assertEqual(offences, [], "\n".join(offences))

    def test_an_off_feed_costs_the_cycle_nothing_at_all(self):
        """The strict gate is read BEFORE normalizing, not after."""
        with patch.object(CFG, "RESEARCH_FEED_ENABLED", False):
            feed = self.feed()
            calls = []
            with patch.object(research_feed, "candidate_from_market",
                              side_effect=lambda *a, **kw: calls.append(1)):
                feed.observe_market(raw_market(), {})
            self.assertEqual(calls, [],
                             "research normalized a market for a feed that "
                             "is switched off")


# ════════════════════════════════════════════════════════════════════════
# V4-RA-04 — uncertain metadata read as zero occupancy
# ════════════════════════════════════════════════════════════════════════
class V4RA04_TransientEnoentWasReadAsAnEmptySpool(AlphaCase):
    """AA-11 propagated every `OSError` and exempted `ENOENT` from its own rule.

    `_scan` had two `FileNotFoundError` handlers and both answered "nothing is
    there":

        except FileNotFoundError:     # os.listdir
            return [], []
        ...
        except FileNotFoundError:     # os.stat
            continue                  # "pruned under us; not an error"

    Right for a spool that has never been created, and wrong for one the
    writer is holding a capacity RESERVATION on -- where the advisory lock
    means nobody else is pruning, so a listed entry that vanishes is metadata
    we cannot account for rather than a race.

    Measured on the pinned base with `max_records=1` and one record already
    written:

        a synthetic transient ENOENT from `os.stat`   -> write ACCEPTED, 2 records
        a synthetic transient ENOENT from `os.listdir`-> write ACCEPTED, 3 records
        `capacity()` while `listdir` raises ENOENT    -> {"occupied": 0}

    A bound that reports zero occupancy exactly when the filesystem is
    misbehaving is not a bound.
    """

    def spool(self, max_records=1):
        return research_spool.BoundedSpool(
            os.path.join(self._tmp, "spool"), max_records=max_records,
            max_bytes=10 ** 7, max_record_bytes=10 ** 6, max_age_s=3600)

    @staticmethod
    def record(index):
        return {"emitted_at_utc": f"2026-09-12T12:00:0{index}+00:00",
                "record_sha256": f"{index}" * 64,
                "contract_id": f"KX-{index}"}

    def spooled(self, spool):
        if not os.path.isdir(spool.directory):
            return []
        return sorted(n for n in os.listdir(spool.directory)
                      if n.endswith(research_spool.RECORD_SUFFIX))

    def vanishing_stat(self, spool):
        real = os.stat

        def hostile(path, *a, **kw):
            if isinstance(path, str) \
                    and path.endswith(research_spool.RECORD_SUFFIX) \
                    and spool.directory in path:
                raise FileNotFoundError(2, "No such file or directory", path)
            return real(path, *a, **kw)

        return patch("os.stat", side_effect=hostile)

    def vanishing_listdir(self, spool):
        real = os.listdir

        def hostile(path, *a, **kw):
            if isinstance(path, str) and os.path.abspath(path) == \
                    os.path.abspath(spool.directory):
                raise FileNotFoundError(2, "No such file or directory", path)
            return real(path, *a, **kw)

        return patch("os.listdir", side_effect=hostile)

    # ── the bound ───────────────────────────────────────────────────────
    def test_a_transient_stat_enoent_cannot_admit_a_second_record(self):
        spool = self.spool(max_records=1)
        self.assertTrue(spool.write(self.record(1)))
        self.assertEqual(len(self.spooled(spool)), 1)
        with self.vanishing_stat(spool):
            accepted = spool.write(self.record(2))
        self.assertFalse(accepted,
                         "a write was admitted while the occupancy of the "
                         "spool could not be established")
        self.assertEqual(
            len(self.spooled(spool)), 1,
            "max_records=1 holds two records: a transient ENOENT during the "
            "reservation read as an empty spool")

    def test_a_transient_listdir_enoent_cannot_admit_a_second_record(self):
        spool = self.spool(max_records=1)
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_listdir(spool):
            accepted = spool.write(self.record(2))
        self.assertFalse(accepted)
        self.assertEqual(len(self.spooled(spool)), 1)

    def test_the_bound_holds_under_a_run_of_transient_enoent(self):
        """Not one witness: the bound must hold however often it happens."""
        spool = self.spool(max_records=2)
        self.assertTrue(spool.write(self.record(1)))
        self.assertTrue(spool.write(self.record(2)))
        with self.vanishing_stat(spool):
            for index in range(3, 12):
                self.assertFalse(spool.write(self.record(index)))
        self.assertEqual(len(self.spooled(spool)), 2)

    # ── the classification ──────────────────────────────────────────────
    def test_a_vanished_directory_is_not_an_empty_spool(self):
        spool = self.spool()
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_listdir(spool):
            with self.assertRaises(research_spool.SpoolCapacityUnknown):
                spool.capacity()

    def test_a_first_time_absent_directory_is_genuinely_zero(self):
        """The control, and the distinction the finding asks for.

        A spool that has never been created really does hold nothing, and a
        first write must be allowed. Failing closed here would mean research
        could never start.
        """
        spool = self.spool()
        self.assertFalse(os.path.isdir(spool.directory))
        self.assertEqual(spool.capacity()["occupied"], 0)
        self.assertTrue(spool.write(self.record(1)))
        self.assertEqual(len(self.spooled(spool)), 1)

    def test_an_uncertain_stat_outside_a_reservation_is_still_a_prune_race(self):
        """The other control: do NOT fail closed everywhere.

        Outside the reservation lock, an entry that is gone by the time we
        stat it is an ordinary race with our own pruning and recovery.
        Failing closed there would turn every prune into a refusal and break
        the asynchronous recovery the finding says to preserve.
        """
        spool = self.spool(max_records=5)
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_stat(spool):
            capacity = spool.capacity()
        self.assertEqual(capacity["occupied"], 0,
                         "a scan outside any reservation refused to skip an "
                         "entry that had genuinely been pruned")

    def test_an_unreadable_directory_still_fails_closed(self):
        """AA-11's own rule, unchanged: a non-ENOENT OSError is unknown."""
        spool = self.spool()
        os.makedirs(spool.directory, exist_ok=True)
        with patch("os.listdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(research_spool.SpoolCapacityUnknown):
                spool.capacity()

    # ── recovery ────────────────────────────────────────────────────────
    def test_recovery_is_asynchronous_and_needs_no_reset(self):
        """Fail closed must not mean fail forever.

        Once the transient condition clears, the next write goes through --
        and the bound is still the bound. The old behaviour is not the
        baseline here: it "recovered" by writing past the limit.
        """
        spool = self.spool(max_records=2)
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_stat(spool):
            self.assertFalse(spool.write(self.record(2)))
        self.assertTrue(spool.write(self.record(2)),
                        "the spool stayed closed after the uncertainty had "
                        "passed")
        self.assertFalse(spool.write(self.record(3)))
        self.assertEqual(len(self.spooled(spool)), 2)

    def test_a_deleted_spool_directory_is_recreated_by_the_next_write(self):
        """The operator case: `rm -rf` on the spool must not be terminal."""
        import shutil
        spool = self.spool(max_records=3)
        self.assertTrue(spool.write(self.record(1)))
        shutil.rmtree(spool.directory)
        self.assertTrue(spool.write(self.record(2)),
                        "a spool whose directory was removed by hand could "
                        "never be written to again")
        self.assertEqual(len(self.spooled(spool)), 1)

    def test_a_removal_during_the_reservation_is_observed_not_uncertain(self):
        """The distinction, at its sharpest, stated so it cannot drift.

        `durable_append.exclusive_lock` re-creates the parent of its sidecar,
        so a directory removed between `makedirs` and the lock is back --
        EMPTY -- by the time the scan runs, and `listdir` SUCCEEDS. That is
        not uncertainty: the records were genuinely deleted, occupancy really
        is zero, and admitting the write is right.

        `_established` therefore does not mean "always fail closed from now
        on". It means "an ENOENT FROM `listdir` is now a disappearance". A
        successful `listdir` is an observation whatever it returns, and this
        case is what keeps the fix from turning a real deletion into a
        permanent refusal.
        """
        import shutil
        spool = self.spool(max_records=2)
        self.assertTrue(spool.write(self.record(1)))
        self.assertTrue(spool.write(self.record(2)))
        self.assertFalse(spool.write(self.record(3)))    # the bound holds

        real_lock = research_spool.exclusive_lock

        def remove_then_lock(path, **kw):
            shutil.rmtree(spool.directory, ignore_errors=True)
            return real_lock(path, **kw)

        with patch.object(research_spool, "exclusive_lock",
                          side_effect=remove_then_lock):
            self.assertTrue(
                spool.write(self.record(4)),
                "a spool whose records were genuinely deleted refused to "
                "accept anything, which is not fail-closed, it is stuck")
        self.assertEqual(len(self.spooled(spool)), 1)

    def test_capacity_from_another_thread_is_not_inside_the_reservation(self):
        """The reservation depth is per-THREAD, and that is load-bearing.

        A plain attribute would make every caller of `capacity()` -- the
        telemetry path included -- fail closed for as long as the writer
        happens to be inside a reservation, which is a different behaviour
        from the one the finding asks for.
        """
        spool = self.spool(max_records=3)
        self.assertTrue(spool.write(self.record(1)))
        observed = {}
        real_scan = research_spool.BoundedSpool._scan

        def scan_then_look(inner_self):
            result = real_scan(inner_self)
            if inner_self._reservation.depth > 0 and "answer" not in observed:
                def outside():
                    try:
                        observed["answer"] = inner_self.capacity()["occupied"]
                    except Exception as exc:                  # noqa: BLE001
                        observed["answer"] = type(exc).__name__
                thread = threading.Thread(target=outside)
                thread.start()
                thread.join(timeout=10)
            return result

        with patch.object(research_spool.BoundedSpool, "_scan",
                          scan_then_look):
            spool.write(self.record(2))
        self.assertEqual(observed.get("answer"), 1,
                         "a thread outside the reservation was given the "
                         "inside answer")

    def test_the_refusal_is_counted_as_unknown_capacity_not_as_full(self):
        """"We do not know" and "it is full" are different operator problems."""
        spool = self.spool(max_records=1)
        self.assertTrue(spool.write(self.record(1)))
        before = dict(spool.stats)
        with self.vanishing_stat(spool):
            self.assertFalse(spool.write(self.record(2)))
        self.assertEqual(spool.stats["capacity_unknown"],
                         before["capacity_unknown"] + 1)
        self.assertEqual(spool.stats["dropped_full"], before["dropped_full"])

    def test_the_records_that_are_there_are_still_readable_afterwards(self):
        """A refusal must not have damaged what the spool already held."""
        spool = self.spool(max_records=1)
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_stat(spool):
            spool.write(self.record(2))
        names = self.spooled(spool)
        self.assertEqual(len(names), 1)
        with open(os.path.join(spool.directory, names[0]),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["contract_id"], "KX-1")

    def test_no_partial_is_left_behind_by_a_refused_write(self):
        spool = self.spool(max_records=1)
        self.assertTrue(spool.write(self.record(1)))
        with self.vanishing_stat(spool):
            spool.write(self.record(2))
        partials = [n for n in os.listdir(spool.directory)
                    if n.endswith(research_spool.TEMP_SUFFIX)]
        self.assertEqual(partials, [])


# ════════════════════════════════════════════════════════════════════════
# The SHADOW_ONLY boundary, re-asserted across all four remediations
# ════════════════════════════════════════════════════════════════════════
class V4_TheShadowOnlyBoundaryIsIntact(unittest.TestCase):
    """None of the four fixes gave research a way to reach the money path."""

    def test_the_producer_imports_nothing_new(self):
        """Delegates to the pinned allow-list rather than restating it."""
        from test_research_feed_boundary import \
            TheProducerKnowsNothingAboutResearch as Pinned
        suite = unittest.TestLoader().loadTestsFromTestCase(Pinned)
        result = unittest.TextTestRunner(
            stream=open(os.devnull, "w"), verbosity=0).run(suite)
        self.assertEqual((len(result.failures), len(result.errors)), (0, 0),
                         f"{result.failures + result.errors}")

    def test_the_research_hook_still_returns_nothing_to_the_engine(self):
        """`observe_market` may not hand the engine anything to act on.

        It returns a bool for telemetry and tests, and the engine's call site
        must discard it -- a hook whose result changed a decision would make
        research part of the money path by the front door.
        """
        import ast
        with open("execution_engine.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        hook = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_shadow_observer")
        for node in ast.walk(hook):
            if isinstance(node, ast.Call) \
                    and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("observe_market",
                                           "emit_candidate"):
                parent_is_expr = any(
                    isinstance(outer, ast.Expr) and outer.value is node
                    for outer in ast.walk(hook))
                self.assertTrue(
                    parent_is_expr,
                    "the engine consumed the research hook's return value")

    def test_the_feed_is_still_off_by_default(self):
        """The DECLARED default, read from `config.py`.

        Deliberately not `bool(CFG.RESEARCH_FEED_ENABLED)`: many cases in
        this suite patch that attribute to True for their own duration, so a
        live read passes or fails depending on what else ran, which is a test
        of the harness rather than of the product.
        """
        with open("config.py", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn(
            '_env_gate("RESEARCH_FEED_ENABLED", default=False)', source,
            "the research feed is no longer opt-IN by declared default")

    def test_no_execution_symbol_reached_the_producer(self):
        """Over the CODE, not the text.

        A substring scan fails on `research_feed`'s own docstring, which says
        the producer "never touches `PersistenceSentinel`" -- a sentence whose
        presence is evidence FOR the boundary. So this walks the AST and looks
        at names that are actually referenced.
        """
        import ast
        with open("research_feed.py", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        forbidden = {"create_order", "place_and_track", "set_capital",
                     "PersistenceSentinel", "OrderManager", "RiskManager",
                     "capital_eligible", "submit_order", "amend_order"}
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
        self.assertEqual(sorted(used & forbidden), [],
                         "the producer references an execution symbol")


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
