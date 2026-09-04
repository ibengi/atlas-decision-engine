"""B1 + B2: strict fail-closed order gates, and the daily (KXBTCD) quarantine.

Two defects are under test here, both found by adversarial review of a design
that had not yet been written as code:

B1  `_env_b` reads anything it does not recognise as TRUE. An empty string, an
    "off", a typo'd "fasle" all opened the gate, and DELETING the variable
    opened it too (default=True). That parser guarded ALLOW_ORDER_SUBMISSION,
    so the entire DEMO hold rested on the literal string "false" being
    present. `_env_gate` never enables on a value it cannot read.

B2  The daily strategy reached ORDER_SUBMIT_ATTEMPT while its settlement label
    source was discredited. A market_type equality test would have been a
    DENYLIST, and `Decision.market_type` defaults to None — so a malformed
    Decision would have walked straight through it. The engine gate is an
    allowlist, and a second guard on the money path itself is keyed on the
    ticker so it holds for callers that never set a market_type at all.
"""

import ast
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import _bootstrap  # noqa: F401  — dummy DEMO creds BEFORE config is imported

import config
from config import (GATE_FALSE_WORDS, GATE_TRUE_WORDS, _env_gate,
                    canonical_ticker, daily_oracle_approved,
                    daily_quarantine_blocks, is_daily_ticker,
                    ticker_is_wellformed)


class StrictGateParser(unittest.TestCase):
    """B1. Every value that is not explicitly true must block."""

    def setUp(self):
        os.environ.pop("ATLAS_TEST_GATE", None)
        config.GATE_PARSE_WARNINGS.clear()

    tearDown = setUp

    def test_absent_is_false(self):
        self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=False))

    def test_absent_honours_default_only_when_caller_asks(self):
        # The parser still supports default=True for non-gate callers; the
        # ORDER gates all pass default=False, asserted separately below.
        self.assertTrue(_env_gate("ATLAS_TEST_GATE", default=True))

    def test_true_vocabulary(self):
        for v in ("true", "TRUE", "True", "1", "yes", "YES", "y", "on", "ON",
                  " true ", "\ttrue\n"):
            os.environ["ATLAS_TEST_GATE"] = v
            self.assertTrue(_env_gate("ATLAS_TEST_GATE", default=False),
                            f"{v!r} should be TRUE")

    def test_false_vocabulary(self):
        for v in ("false", "FALSE", "False", "0", "no", "NO", "n", "off",
                  "OFF", "non", " false ", "\tno\n"):
            os.environ["ATLAS_TEST_GATE"] = v
            self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=False),
                             f"{v!r} should be FALSE")

    def test_empty_and_whitespace_are_false(self):
        # The exact defect: os.getenv returns "" for a variable that EXISTS
        # but is blank, so the old parser never saw its default at all.
        for v in ("", " ", "   ", "\t", "\n"):
            os.environ["ATLAS_TEST_GATE"] = v
            self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=False),
                             f"{v!r} should be FALSE")
            self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=True),
                             f"{v!r} must be FALSE even with default=True")

    def test_malformed_values_are_false_and_warn(self):
        for v in ("disabled", "fasle", "null", "none", "random text", "0.0",
                  "F", "T", "enable", "-1", "2"):
            config.GATE_PARSE_WARNINGS.clear()
            os.environ["ATLAS_TEST_GATE"] = v
            self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=False),
                             f"{v!r} must not enable anything")
            self.assertFalse(_env_gate("ATLAS_TEST_GATE", default=True),
                             f"{v!r} must be FALSE even with default=True")
            self.assertTrue(config.GATE_PARSE_WARNINGS,
                            f"{v!r} must be reported loudly")

    def test_recognised_values_do_not_warn(self):
        for v in GATE_TRUE_WORDS + GATE_FALSE_WORDS:
            config.GATE_PARSE_WARNINGS.clear()
            os.environ["ATLAS_TEST_GATE"] = v
            _env_gate("ATLAS_TEST_GATE", default=False)
            self.assertEqual(config.GATE_PARSE_WARNINGS, [],
                             f"{v!r} is a known word and must not warn")

    def test_order_gates_are_parsed_strictly_and_default_false(self):
        """The two order gates must not be readable by the permissive parser.

        Asserted against the source because the parsed value alone cannot
        distinguish "strict parser, default False" from "permissive parser
        that happened to see a valid word".
        """
        path = os.path.join(os.path.dirname(__file__), "..", "config.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # Statements can wrap; join continuation lines before matching.
        flat = " ".join(src.split())
        for name in ("ALLOW_ORDER_SUBMISSION", "DAILY_RESEARCH_ORACLE_APPROVED"):
            i = flat.index(f"{name} = _env_gate(")
            stmt = flat[i:flat.index(")", i) + 1]
            self.assertIn("_env_gate(", stmt, f"{name} must be strict")
            self.assertIn("default=False", stmt,
                          f"{name} must not default to an enabling value")
        # And the permissive parser must not be used for either of them.
        self.assertNotIn("ALLOW_ORDER_SUBMISSION = _env_b(", flat)
        self.assertNotIn("DAILY_RESEARCH_ORACLE_APPROVED = _env_b(", flat)


class DailyApprovalState(unittest.TestCase):
    """Part 4: daily approval resolves FALSE until a derived artifact exists."""

    def test_resolves_false_by_default(self):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            self.assertFalse(daily_oracle_approved())

    def test_both_guards_read_one_resolver(self):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", True):
            self.assertTrue(daily_oracle_approved())
            self.assertFalse(daily_quarantine_blocks("KXBTCD-26SEP0306-T1"))
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            self.assertTrue(daily_quarantine_blocks("KXBTCD-26SEP0306-T1"))

    def test_quarantine_is_keyed_on_ticker_not_market_type(self):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            for t in ("KXBTCD-26SEP0306-T67099.99", "kxbtcd-lower-case",
                      "KXBTCD"):
                self.assertTrue(daily_quarantine_blocks(t), t)
            for t in ("KXBTC15M-26SEP030900-00", "KXTEST-CANARY-T1",
                      "KXNFL-GAME", "", None):
                self.assertFalse(daily_quarantine_blocks(t), repr(t))


class _Dec:
    """Minimal stand-in for Decision. `market_type=None` mirrors the dataclass
    default — the exact shape a denylist would have let through."""

    def __init__(self, ticker="KXBTC15M-26SEP030900-00",
                 market_type="btc_15m_above_strike", strategy="s"):
        self.ticker = ticker
        self.market_type = market_type
        self.strategy = strategy
        self.side = "yes"
        self.confidence = 8
        self.taille = "0.5%"
        self.model_probability = 0.6
        self.market_probability = 0.5
        self.net_edge = 0.1
        self.net_ev = 0.1
        self.category = "Crypto"


def _engine():
    """An ExecutionEngine with only the fields _execute_decision touches, so
    the test exercises the real method rather than a re-implementation."""
    import execution_engine as ee
    eng = ee.ExecutionEngine.__new__(ee.ExecutionEngine)
    # An EMPTY book, so a decision that clears both gates stops one line later
    # at `if not book: return 0`. Passing the gates is what these tests assert;
    # running the whole sizing path would only prove MagicMock arithmetic.
    eng.fresh_book = MagicMock(return_value=({}, None))
    eng.risk = MagicMock()
    eng.risk.portfolio_check.return_value = (True, "")
    eng.risk.rolling_drawdown.return_value = 0.0
    eng.risk.drawdown_size_factor.return_value = 1.0
    eng.risk.claim_half_open_attempt.return_value = (True, "")
    eng.posmgr = MagicMock()
    eng.posmgr.open_risk_by_category.return_value = {}
    eng.posmgr.open_risk_on.return_value = 0.0
    eng.posmgr.open_risk.return_value = 0.0
    eng.orders = MagicMock()
    eng.capital = 100.0
    return eng


class DailyExecutionQuarantine(unittest.TestCase):
    """B2, engine side: money path untouched for a blocked daily decision."""

    def setUp(self):
        self.report = {"rejections": {}}

    def _run(self, dec, approved=False):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                          approved):
            eng = _engine()
            placed = eng._execute_decision(dec, self.report)
            return eng, placed

    def test_daily_decision_is_refused_and_spends_nothing(self):
        eng, placed = self._run(_Dec(ticker="KXBTCD-26SEP0306-T67099.99",
                                     market_type="btc_above_strike_daily"))
        self.assertEqual(placed, 0)
        self.assertEqual(self.report["rejections"]["daily_oracle_unapproved"], 1)
        # Part 6 money-path proofs, each asserted on the real object.
        eng.fresh_book.assert_not_called()
        eng.risk.portfolio_check.assert_not_called()
        eng.risk.claim_half_open_attempt.assert_not_called()
        eng.orders.place_and_track.assert_not_called()
        self.assertNotIn("risk_passed", self.report)
        self.assertNotIn("orders_submitted", self.report)

    def test_daily_ticker_refused_even_when_market_type_is_wrong(self):
        """A hand-built Decision that never set market_type must still fail
        closed — this is the case a denylist would have executed."""
        for mt in (None, "unknown", "btc_daily_above_strike_v2", ""):
            self.report = {"rejections": {}}
            eng, placed = self._run(_Dec(ticker="KXBTCD-26SEP0306-T1",
                                         market_type=mt))
            self.assertEqual(placed, 0, f"market_type={mt!r}")
            self.assertEqual(
                self.report["rejections"].get("daily_oracle_unapproved"), 1,
                f"market_type={mt!r} must hit the daily guard")
            eng.fresh_book.assert_not_called()

    def test_unknown_market_type_fails_closed_via_allowlist(self):
        for mt in (None, "unknown", "sports_newthing", ""):
            self.report = {"rejections": {}}
            eng, placed = self._run(_Dec(ticker="KXNFL-GAME", market_type=mt))
            self.assertEqual(placed, 0, f"market_type={mt!r}")
            self.assertEqual(
                self.report["rejections"].get("market_type_not_executable"), 1,
                f"market_type={mt!r} must hit the allowlist")
            eng.fresh_book.assert_not_called()

    def test_15m_is_unaffected_and_proceeds_past_both_gates(self):
        eng, _ = self._run(_Dec())          # btc_15m_above_strike
        eng.fresh_book.assert_called_once()
        self.assertNotIn("daily_oracle_unapproved", self.report["rejections"])
        self.assertNotIn("market_type_not_executable",
                         self.report["rejections"])

    def test_15m_unaffected_by_daily_approval_state(self):
        for approved in (True, False):
            self.report = {"rejections": {}}
            eng, _ = self._run(_Dec(), approved=approved)
            eng.fresh_book.assert_called_once()

    def test_daily_proceeds_once_approved(self):
        eng, _ = self._run(_Dec(ticker="KXBTCD-26SEP0306-T1",
                                market_type="btc_above_strike_daily"),
                           approved=True)
        eng.fresh_book.assert_called_once()

    def test_allowlist_excludes_daily_market_type(self):
        import execution_engine as ee
        self.assertNotIn(ee.DAILY_MARKET_TYPE, ee.EXECUTABLE_MARKET_TYPES)
        self.assertIn("btc_15m_above_strike", ee.EXECUTABLE_MARKET_TYPES)

    def test_effective_allowlist_has_one_switch_only(self):
        """Approving the oracle is necessary AND sufficient for the daily type
        to become executable — there is no second list to edit, and editing a
        list without approving the oracle changes nothing."""
        import execution_engine as ee
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            self.assertNotIn(ee.DAILY_MARKET_TYPE, ee.executable_market_types())
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", True):
            self.assertIn(ee.DAILY_MARKET_TYPE, ee.executable_market_types())
        # 15m is in the list either way.
        for approved in (True, False):
            with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                              approved):
                self.assertIn("btc_15m_above_strike",
                              ee.executable_market_types())


def _order_manager():
    """An OrderManager with only the fields place_and_track touches, so the
    test exercises the real money path rather than a re-implementation."""
    import order_manager as om_mod
    om = om_mod.OrderManager.__new__(om_mod.OrderManager)
    om.client = MagicMock()
    om.client.env = "demo"
    om.client.create_order = MagicMock()
    om.resolution_halt = None
    om.pending_intents = {}
    om.session_submitted = {}
    om.exchange_pause_until = 0.0
    return om


class SecondaryMoneyPathGuard(unittest.TestCase):
    """Part 3: place_and_track refuses KXBTCD on its own, with no broker call."""

    _om = staticmethod(_order_manager)

    def test_direct_invocation_with_kxbtcd_is_refused_without_broker_call(self):
        om = self._om()
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", True), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False), \
             patch.object(config.CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
             patch("order_manager.PersistenceSentinel") as sentinel, \
             patch("order_manager.assert_real_demo_integrity"):
            sentinel.healthy.return_value = True
            res = om.place_and_track("KXBTCD-26SEP0306-T67099.99", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:daily_oracle_unapproved")
        self.assertEqual(res.state, "rejected")
        self.assertEqual(om.client.create_order.call_count, 0)

    def test_non_daily_ticker_is_not_refused_by_this_guard(self):
        """A non-daily ticker must pass THROUGH the guard. Proven by letting
        it stop at the next gate below it (the session dedup lock), which is
        reachable only if the daily guard did not fire."""
        import time as _t
        om = self._om()
        om.session_submitted = {"KXTEST-CANARY-T1": _t.time()}
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", True), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False), \
             patch.object(config.CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
             patch.object(config.CFG, "SUBMIT_DEDUP_TTL_S", 3600.0), \
             patch("order_manager.PersistenceSentinel") as sentinel, \
             patch("order_manager.assert_real_demo_integrity"):
            sentinel.healthy.return_value = True
            res = om.place_and_track("KXTEST-CANARY-T1", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:duplicate_submission_guard")
        self.assertEqual(om.client.create_order.call_count, 0)

    def test_submission_disabled_takes_precedence_for_a_daily_ticker(self):
        """Guard ORDER: the global submission gate is reported first, so the
        existing state5 restore assertion (blocked:submission_disabled on a
        KXBTCD ticker) keeps holding. Either way zero broker writes."""
        om = self._om()
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", False), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False), \
             patch.object(config.CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
             patch("order_manager.PersistenceSentinel") as sentinel, \
             patch("order_manager.assert_real_demo_integrity"):
            sentinel.healthy.return_value = True
            res = om.place_and_track("KXBTCD-26AUG2817-T84999.99", "yes", 1, 40)
        self.assertEqual(res.status, "blocked:submission_disabled")
        self.assertEqual(om.client.create_order.call_count, 0)

    def test_restart_harness_ticker_is_not_a_daily_ticker(self):
        """Part 5: the harness submits KXTEST-CANARY-T1, so it is unaffected —
        but a KXBTCD ticker through that same call would be refused."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                                "restart_harness.py"), encoding="utf-8").read()
        line = next(ln for ln in src.splitlines()
                    if ln.startswith("TICKER"))
        self.assertNotIn("KXBTCD", line.upper())
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            self.assertFalse(daily_quarantine_blocks("KXTEST-CANARY-T1"))
            self.assertTrue(daily_quarantine_blocks("KXBTCD-ANY"))


class GuardIndependence(unittest.TestCase):
    """Part 7: a misconfiguration of one gate must not disable the other."""

    def test_daily_guard_holds_when_submission_is_enabled(self):
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", True), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            self.assertTrue(daily_quarantine_blocks("KXBTCD-X"))

    def test_submission_guard_holds_when_daily_is_approved(self):
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", False), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", True):
            self.assertFalse(config.CFG.ALLOW_ORDER_SUBMISSION)


#: Every spelling of one daily ticker that a caller could plausibly produce —
#: a stray space from a CSV column, a tab from a copy/paste, a newline from a
#: file read, a lowercase series from a hand-written tool. Before the fix,
#: EVERY entry but the first two walked through both guards and reached
#: create_order, because the classifier was a bare `.upper().startswith()`.
DAILY_TICKER_VARIANTS = (
    "KXBTCD-26SEP0306-T77599.99",
    "kxbtcd-26sep0306-t77599.99",
    " KXBTCD-26SEP0306-T77599.99",
    "\tKXBTCD-26SEP0306-T77599.99",
    "\nKXBTCD-26SEP0306-T77599.99",
    "\rKXBTCD-26SEP0306-T77599.99",
    "KXBTCD-26SEP0306-T77599.99 ",
    "  kxbtcd-26sep0306-t77599.99  ",
    "\r\n kxbtcd-26SEP0306-T77599.99 \t\n",
    "\x0bKXBTCD-26SEP0306-T77599.99",
    "\x0cKXBTCD-26SEP0306-T77599.99",
    "\xa0KXBTCD-26SEP0306-T77599.99",      # NBSP: str.strip() removes it
)

#: Values that cannot be classified at all. A guard keyed on a prefix has
#: nothing to say about them, so the money path must refuse them outright
#: rather than hand them to the broker and hope.
UNCLASSIFIABLE_TICKERS = (
    None, "", " ", "   ", "\t", "\n", "\r\n \t",
    b"KXBTCD-26SEP0306-T1",                 # bytes are not a Kalshi ticker
    bytearray(b"KXBTC15M-X"), 123, 12.5, True, ["KXBTCD-X"],
    {"ticker": "KXBTCD-X"}, object(),
    "\u200bKXBTCD-26SEP0306-T1",            # zero width space: strip() keeps it
    "\u2060KXBTCD-26SEP0306-T1",            # word joiner
    "KX BTCD-26SEP0306-T1",                 # inner blank: NOT a daily ticker
    "-KXBTCD-X",                            # cannot start with a separator
    "KXBTCD/26SEP0306",                     # '/' is not a Kalshi ticker char
)


class TickerCanonicalisation(unittest.TestCase):
    """A: one canonical form, shared by both guards.

    The bypass this closes: `" KXBTCD-…"` — a single leading space — used to
    classify as NOT daily, so both the engine quarantine and the money-path
    guard passed it straight through to create_order.
    """

    def test_border_whitespace_and_case_are_normalised(self):
        for t in DAILY_TICKER_VARIANTS:
            self.assertEqual(canonical_ticker(t), "KXBTCD-26SEP0306-T77599.99",
                             repr(t))

    def test_inner_whitespace_is_never_removed(self):
        """Stripping INSIDE a ticker would manufacture a valid ticker out of an
        invalid string — the transformation an attacker would want, and one
        the Kalshi ticker format does not authorise."""
        self.assertEqual(canonical_ticker(" KX BTCD-X "), "KX BTCD-X")
        self.assertNotEqual(canonical_ticker("KX BTCD-X"), "KXBTCD-X")
        self.assertFalse(is_daily_ticker("KX BTCD-X"))
        self.assertFalse(ticker_is_wellformed("KX BTCD-X"))

    def test_every_variant_classifies_as_daily(self):
        for t in DAILY_TICKER_VARIANTS:
            self.assertTrue(is_daily_ticker(t), repr(t))

    def test_every_variant_is_quarantined_when_unapproved(self):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            for t in DAILY_TICKER_VARIANTS:
                self.assertTrue(daily_quarantine_blocks(t), repr(t))

    def test_non_daily_tickers_stay_unblocked_through_the_same_path(self):
        with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED", False):
            for t in (" KXBTC15M-26SEP030900-00", "\tKXTEST-CANARY-T1",
                      "kxnfl-game", "KXETHD-26SEP0306-T1", "KXBTC-26SEP03"):
                self.assertFalse(daily_quarantine_blocks(t), repr(t))
                self.assertTrue(ticker_is_wellformed(t), repr(t))

    def test_unclassifiable_values_are_not_wellformed(self):
        for t in UNCLASSIFIABLE_TICKERS:
            self.assertFalse(ticker_is_wellformed(t), repr(t))

    def test_bytes_are_still_classified_defensively_as_daily(self):
        """Belt and braces: bytes are refused as malformed on the money path,
        but the CLASSIFIER still reads them as daily, so the quarantine holds
        even if a future caller learns to accept bytes."""
        for t in (b"KXBTCD-26SEP0306-T1", bytearray(b" kxbtcd-x"),
                  b"\tKXBTCD-X"):
            self.assertTrue(is_daily_ticker(t), repr(t))
            with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                              False):
                self.assertTrue(daily_quarantine_blocks(t), repr(t))
            self.assertFalse(ticker_is_wellformed(t), repr(t))

    def test_canonicalisation_never_raises(self):
        for t in UNCLASSIFIABLE_TICKERS + DAILY_TICKER_VARIANTS:
            canonical_ticker(t)
            is_daily_ticker(t)
            ticker_is_wellformed(t)
            daily_quarantine_blocks(t)

    def test_both_guards_share_the_single_classifier(self):
        """Neither path may grow its own normalisation. Asserted on the source
        of both call sites: each defers to daily_quarantine_blocks, whose one
        canonical form is the subject of every test above."""
        root = os.path.join(os.path.dirname(__file__), "..")
        for mod, marker in (("execution_engine.py", "_execute_decision"),
                            ("order_manager.py", "place_and_track")):
            src = open(os.path.join(root, mod), encoding="utf-8").read()
            self.assertIn("daily_quarantine_blocks(ticker)", src, mod)
            self.assertIn("ticker_is_wellformed(ticker)", src, mod)
            self.assertIn(marker, src, mod)
            # No second, local spelling of the prefix test anywhere.
            self.assertNotIn('.upper().startswith("KXBTCD")', src, mod)
            self.assertNotIn('startswith("KXBTCD")', src, mod)


class MalformedTickerFailsClosed(unittest.TestCase):
    """A: a ticker that cannot be classified never becomes a broker write."""

    _om = staticmethod(_order_manager)

    def test_place_and_track_refuses_every_unclassifiable_value(self):
        for t in UNCLASSIFIABLE_TICKERS:
            om = self._om()
            with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", True), \
                 patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                              True), \
                 patch.object(config.CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                 patch("order_manager.PersistenceSentinel") as sentinel, \
                 patch("order_manager.assert_real_demo_integrity"):
                sentinel.healthy.return_value = True
                res = om.place_and_track(t, "yes", 1, 40)
            self.assertEqual(res.status, "blocked:ticker_malformed", repr(t))
            self.assertEqual(res.state, "rejected", repr(t))
            self.assertEqual(om.client.create_order.call_count, 0, repr(t))

    def test_engine_refuses_unclassifiable_before_reading_a_book(self):
        for t in UNCLASSIFIABLE_TICKERS:
            report = {"rejections": {}}
            with patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                              True):
                eng = _engine()
                placed = eng._execute_decision(
                    _Dec(ticker=t, market_type="btc_15m_above_strike"), report)
            self.assertEqual(placed, 0, repr(t))
            self.assertEqual(report["rejections"].get("ticker_malformed"), 1,
                             repr(t))
            eng.fresh_book.assert_not_called()
            eng.orders.place_and_track.assert_not_called()


class DefenceInDepthMatrix(unittest.TestCase):
    """B: the two gates are independent, and neither can override the other."""

    def _om(self, dedup_ticker=None):
        om = _order_manager()
        if dedup_ticker is not None:
            import time as _t
            om.session_submitted = {dedup_ticker: _t.time()}
        return om

    def _call(self, ticker, *, allow, approved, dedup_ticker=None):
        om = self._om(dedup_ticker)
        with patch.object(config.CFG, "ALLOW_ORDER_SUBMISSION", allow), \
             patch.object(config.CFG, "DAILY_RESEARCH_ORACLE_APPROVED",
                          approved), \
             patch.object(config.CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
             patch.object(config.CFG, "SUBMIT_DEDUP_TTL_S", 3600.0), \
             patch("order_manager.PersistenceSentinel") as sentinel, \
             patch("order_manager.assert_real_demo_integrity"):
            sentinel.healthy.return_value = True
            res = om.place_and_track(ticker, "yes", 1, 40)
        self.assertEqual(om.client.create_order.call_count, 0, repr(ticker))
        return res

    def test_every_variant_is_blocked_with_submission_enabled(self):
        """The exact adversarial case: the global hold is OPEN, so only the
        daily guard stands between these tickers and the broker."""
        for t in DAILY_TICKER_VARIANTS:
            res = self._call(t, allow=True, approved=False)
            self.assertEqual(res.status, "blocked:daily_oracle_unapproved",
                             repr(t))

    def test_global_false_daily_false_blocks_globally(self):
        res = self._call("KXBTCD-26SEP0306-T1", allow=False, approved=False)
        self.assertEqual(res.status, "blocked:submission_disabled")

    def test_global_false_daily_true_blocks_globally(self):
        res = self._call("KXBTCD-26SEP0306-T1", allow=False, approved=True)
        self.assertEqual(res.status, "blocked:submission_disabled")

    def test_global_true_daily_false_blocks_on_the_daily_guard(self):
        res = self._call("KXBTCD-26SEP0306-T1", allow=True, approved=False)
        self.assertEqual(res.status, "blocked:daily_oracle_unapproved")

    def test_global_true_daily_true_reaches_only_the_normal_gates(self):
        """Both policy gates open: the call must proceed PAST them and stop at
        an ordinary operational gate. Proven with the session dedup lock,
        which is unreachable unless both policy guards passed."""
        res = self._call("KXBTCD-26SEP0306-T1", allow=True, approved=True,
                         dedup_ticker="KXBTCD-26SEP0306-T1")
        self.assertEqual(res.status, "blocked:duplicate_submission_guard")

    def test_fifteen_minute_path_is_unchanged_by_the_daily_state(self):
        for approved in (True, False):
            res = self._call("KXBTC15M-26SEP030900-00", allow=True,
                             approved=approved,
                             dedup_ticker="KXBTC15M-26SEP030900-00")
            self.assertEqual(res.status, "blocked:duplicate_submission_guard",
                             f"approved={approved}")


# Environment variables that decide whether an order may leave this process.
# Imported from the module that SETS them for the suite, so the scrub below
# can never drift from the thing it is scrubbing: the subprocess tests observe
# the SHIPPED defaults rather than whatever the test runners set for their own
# convenience. Two spellings because `tests/` is on sys.path as a package under
# pytest and as the top level under `unittest discover("tests")`.
try:
    from tests._gates import GATE_VARS as _GATE_VARS
except ImportError:            # pragma: no cover - depends on the runner
    from _gates import GATE_VARS as _GATE_VARS

_PROBE = (
    "import json, config;"
    "print(json.dumps({"
    "'allow': bool(config.CFG.ALLOW_ORDER_SUBMISSION),"
    "'daily': bool(config.CFG.DAILY_RESEARCH_ORACLE_APPROVED),"
    "'resolver': bool(config.daily_oracle_approved()),"
    "'warned': sorted({n for n, _ in config.GATE_PARSE_WARNINGS}),"
    "}))"
)


class CleanEnvironmentDefaults(unittest.TestCase):
    """C: what the SHIPPED code does in a clean environment, observed.

    Every other test in this file runs inside a process whose conftest sets
    both gates to "true" so that ~130 unrelated order-plumbing tests keep
    working. That conftest is exactly the thing that could hide a regression
    in the defaults, so these assertions are made in a SEPARATE interpreter
    with the variables removed — behaviour, not a source string.
    """

    def _probe(self, **overrides):
        env = {k: v for k, v in os.environ.items() if k not in _GATE_VARS}
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        out = subprocess.run([sys.executable, "-c", _PROBE], env=env, cwd=root,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_the_probe_sees_a_scrubbed_environment(self):
        """Guards the guard: if the scrub silently stopped working, every
        assertion below would pass for the wrong reason."""
        self.assertTrue(all(v in os.environ for v in _GATE_VARS),
                        "conftest is expected to set both gates in-process")
        got = self._probe()
        self.assertIn("allow", got)

    def test_absent_variables_ship_closed(self):
        got = self._probe()
        self.assertFalse(got["allow"], "ALLOW_ORDER_SUBMISSION must ship FALSE")
        self.assertFalse(got["daily"], "daily approval must ship FALSE")
        self.assertFalse(got["resolver"], "the resolver must agree")
        self.assertEqual(got["warned"], [], "an absent gate is not a warning")

    def test_blank_and_whitespace_are_false(self):
        for v in ("", " ", "   ", "\t", "\n", "\t \n"):
            got = self._probe(ALLOW_ORDER_SUBMISSION=v,
                              DAILY_RESEARCH_ORACLE_APPROVED=v)
            self.assertFalse(got["allow"], repr(v))
            self.assertFalse(got["daily"], repr(v))
            self.assertFalse(got["resolver"], repr(v))

    def test_malformed_values_are_false_and_reported(self):
        for v in ("maybe", "fasle", "TRUE!", "enabled", "2", "-1", "null",
                  "none", "T", "F", "oui"):
            got = self._probe(ALLOW_ORDER_SUBMISSION=v,
                              DAILY_RESEARCH_ORACLE_APPROVED=v)
            self.assertFalse(got["allow"], repr(v))
            self.assertFalse(got["daily"], repr(v))
            self.assertFalse(got["resolver"], repr(v))
            self.assertEqual(got["warned"], sorted(_GATE_VARS),
                             f"{v!r} must be reported loudly at start-up")

    def test_explicit_false_words_are_false(self):
        for v in GATE_FALSE_WORDS + ("FALSE", "Off", " no "):
            got = self._probe(ALLOW_ORDER_SUBMISSION=v,
                              DAILY_RESEARCH_ORACLE_APPROVED=v)
            self.assertFalse(got["allow"], repr(v))
            self.assertFalse(got["daily"], repr(v))

    def test_only_the_true_vocabulary_opens_a_gate(self):
        for v in GATE_TRUE_WORDS + ("TRUE", "Yes", " on "):
            got = self._probe(ALLOW_ORDER_SUBMISSION=v,
                              DAILY_RESEARCH_ORACLE_APPROVED=v)
            self.assertTrue(got["allow"], repr(v))
            self.assertTrue(got["daily"], repr(v))
            self.assertTrue(got["resolver"], repr(v))
            self.assertEqual(got["warned"], [], repr(v))

    def test_the_two_gates_are_read_independently(self):
        got = self._probe(ALLOW_ORDER_SUBMISSION="true",
                          DAILY_RESEARCH_ORACLE_APPROVED=None)
        self.assertTrue(got["allow"])
        self.assertFalse(got["daily"], "daily must not follow the global gate")
        got = self._probe(ALLOW_ORDER_SUBMISSION=None,
                          DAILY_RESEARCH_ORACLE_APPROVED="true")
        self.assertFalse(got["allow"], "global must not follow the daily gate")
        self.assertTrue(got["daily"])


class ImportOrderIsIrrelevant(unittest.TestCase):
    """D: CFG safety state must not depend on which module imported first."""

    def test_config_state_is_identical_whatever_is_imported_first(self):
        env = {k: v for k, v in os.environ.items() if k not in _GATE_VARS}
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        first = None
        for lead in ("", "import execution_engine;", "import order_manager;",
                     "import strategy_router;", "import market_scanner;"):
            out = subprocess.run([sys.executable, "-c", lead + _PROBE],
                                 env=env, cwd=root, capture_output=True,
                                 text=True, timeout=120)
            self.assertEqual(out.returncode, 0, f"{lead}: {out.stderr}")
            got = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertFalse(got["allow"], lead)
            self.assertFalse(got["daily"], lead)
            if first is None:
                first = got
            self.assertEqual(got, first, lead)


class BothRunnersShareTestGates(unittest.TestCase):
    """A: pytest and unittest discovery must see the same test assumptions.

    `tests/conftest.py` is a PYTEST mechanism. The repository's build gate is
    `run_tests.py` (unittest discovery -> test_report.json -> the Dockerfile's
    test stage -> model_gatekeeper). Gate defaults that lived only in conftest
    made the suite green under pytest and red under the runner that actually
    stops the build, so no image could be produced. These tests pin the fix.
    """

    def _import_run_tests(self, scrub=True):
        """Import run_tests in a child and report what it did to the env.

        Importing is enough: `main()` is behind `if __name__ == "__main__"`,
        so nothing is discovered or executed — only the module-level import
        of the shared gate defaults runs.
        """
        env = dict(os.environ)
        if scrub:
            for v in _GATE_VARS:
                env.pop(v, None)
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        probe = (
            "import json, os, run_tests;"
            "print(json.dumps({v: os.environ.get(v) for v in "
            f"{list(_GATE_VARS)!r}}}))"
        )
        out = subprocess.run([sys.executable, "-c", probe], env=env, cwd=root,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_importing_run_tests_applies_the_shared_gate_defaults(self):
        got = self._import_run_tests()
        for v in _GATE_VARS:
            self.assertEqual(got[v], "true",
                             f"{v} must be set for the unittest runner too")

    def test_run_tests_does_not_override_an_explicit_setting(self):
        """setdefault, not assignment: a deliberately CLOSED run stays closed."""
        env = dict(os.environ)
        env["ALLOW_ORDER_SUBMISSION"] = "false"
        env["DAILY_RESEARCH_ORACLE_APPROVED"] = "false"
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        probe = ("import json, os, run_tests;"
                 "print(json.dumps({v: os.environ.get(v) for v in "
                 f"{list(_GATE_VARS)!r}}}))")
        out = subprocess.run([sys.executable, "-c", probe], env=env, cwd=root,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        for v in _GATE_VARS:
            self.assertEqual(got[v], "false", f"{v} must not be overridden")

    def test_the_gate_defaults_are_loaded_before_discovery(self):
        """Order matters: `config` freezes its class attributes at import, so
        the defaults must be in place before any test module is discovered."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "run_tests.py"), encoding="utf-8").read()
        self.assertIn("_gates", src, "run_tests.py must load the shared gates")
        self.assertLess(src.index("_gates"), src.index("loader.discover"),
                        "gate defaults must be imported BEFORE discovery")

    def test_production_defaults_are_untouched_by_the_test_plumbing(self):
        """The plumbing may only add ENV defaults — never edit the shipped
        parser or its default. Asserted behaviourally in the scrubbed
        subprocess above; asserted here as source, so a future edit that
        reopens the production default is caught in two independent ways."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        raw = open(os.path.join(root, "tests", "_gates.py"),
                   encoding="utf-8").read()
        # Assert on the EXECUTABLE source only: the docstring and comments
        # discuss CFG and config by name, and a prose mention is not a call.
        tree = ast.parse(raw)
        doc = ast.get_docstring(tree) or ""
        body = raw.replace(doc, "", 1) if doc else raw
        code = "\n".join(ln for ln in body.splitlines()
                          if not ln.lstrip().startswith("#"))
        self.assertNotIn("import config", code,
                         "the test plumbing must not import production config")
        self.assertNotIn("CFG.", code,
                         "the test plumbing must not write production CFG")
        for v in _GATE_VARS:
            self.assertIn(f'os.environ.setdefault("{v}"', code,
                          f"{v} must be a setdefault, never an assignment")
        self.assertNotIn("os.environ[", code,
                         "no unconditional env assignment in the test plumbing")


if __name__ == "__main__":
    unittest.main()
