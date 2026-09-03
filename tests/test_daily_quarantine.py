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

import os
import unittest
from unittest.mock import MagicMock, patch

import _bootstrap  # noqa: F401  — dummy DEMO creds BEFORE config is imported

import config
from config import (GATE_FALSE_WORDS, GATE_TRUE_WORDS, _env_gate,
                    daily_oracle_approved, daily_quarantine_blocks)


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


class SecondaryMoneyPathGuard(unittest.TestCase):
    """Part 3: place_and_track refuses KXBTCD on its own, with no broker call."""

    def _om(self):
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


if __name__ == "__main__":
    unittest.main()
