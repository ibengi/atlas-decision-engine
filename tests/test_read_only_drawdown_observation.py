# -*- coding: utf-8 -*-
"""PROD READ_ONLY may observe through `equity_drawdown`; nothing else changes.

The behaviour is REPO-OWNED: it lives in `execution_engine.ExecutionEngine`
and is selected by the access mode alone. Nothing is installed or patched
for these tests. They drive the REAL `_post_balance_gates`,
`_cycle_sequential`, `_cycle_parallel`, `_finish_cycle`,
`_record_cycle_evidence` and `_execute_decision`, and read every claim back
from the durable sink, the dashboard file, the cycle report, or a recorder at
the write boundary. Only the underlying guard VERDICT
(`_evaluate_global_guards`) is injected; the drawdown arithmetic behind it
is pinned in test_p0_observability.

Written as unittest.TestCase so that `python run_tests.py` -- the runner the
Docker build and the LIVE gate rely on -- collects them.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import config                                                     # noqa: E402
import execution_engine                                           # noqa: E402
import test_shadow_write_layer_isolation as shadow_iso            # noqa: E402
from config import CFG, _p                                        # noqa: E402
from execution_engine import ExecutionEngine, OBSERVATION_ONLY_GUARD  # noqa: E402
from persistence import JsonStore                                 # noqa: E402

GUARD = OBSERVATION_ONLY_GUARD
FLAG = "_capital_blocking_guard"


class _Observed(shadow_iso._IsolatedState, unittest.TestCase):
    """Own DATA_DIR; mode variables saved and restored; the underlying guard
    verdict injectable per test. Nothing installed."""

    def setUp(self):
        shadow_iso._IsolatedState.setUp(self)
        self._gate_patch = None

    def tearDown(self):
        if self._gate_patch is not None:
            self._gate_patch.stop()
        shadow_iso._IsolatedState.tearDown(self)

    def _mode(self, value):
        if value is None:
            os.environ.pop("PROD_ACCESS_MODE", None)
        else:
            os.environ["PROD_ACCESS_MODE"] = value

    def _gate_returns(self, ok, guard):
        """Inject the verdict of the REAL guard evaluator for this cycle."""
        if self._gate_patch is not None:
            self._gate_patch.stop()
        self._gate_patch = patch.object(ExecutionEngine,
                                        "_evaluate_global_guards",
                                        lambda self: (ok, guard))
        self._gate_patch.start()


class _Sink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        # A durable writer serializes; an unserializable row must fail here.
        self.rows.append(json.loads(json.dumps(row)))


class _Pipeline:
    def __init__(self, raises=None):
        self.calls = 0
        self.raises = raises

    def run_cycle(self, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return {"report": {"cycle_id": "cyc-ro", "scanned_raw": 9,
                           "scanned": 9, "ranker_eligible": 1, "accepted": 0,
                           "rejections": {}},
                "accepted": []}


def _engine(env="prod"):
    """A real ExecutionEngine instance with only its collaborators stubbed."""
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.client = type("C", (), {"env": env})()
    eng.cycles_jsonl = _Sink()
    eng.stats = type("S", (), {"log_summary": lambda self: None})()
    eng.posmgr = type("P", (), {"tickers_open": lambda self: set()})()
    eng.tlog = type("T", (), {"has_open_on": lambda self, t: False})()
    eng.orders = type("O", (), {"exchange_pause_until": 0.0})()
    eng.scanner = type("Sc", (), {"shadow_population": lambda self: []})()
    eng.btc_daily_shadow = type("B", (), {"run": lambda self, pop, cid: {}})()
    eng.capital = eng.configured_capital = eng.last_balance = 0.04
    eng.pipeline = _Pipeline()
    eng._balance_gate = lambda *a: (True, "solde=0.04$")
    eng.executed = []
    eng._execute_decision = lambda dec, report: eng.executed.append(dec) or 0
    return eng


def _parallel(eng, n=1):
    eng._executor = type("E", (), {"submit": lambda self, fn: type(
        "F", (), {"result": lambda self: (0.04, None)})()})()
    eng._background_balance_health = lambda: (0.04, None)
    return eng._cycle_parallel(n)


# --------------------------------------------------------------------------
# The gate verdict.
# --------------------------------------------------------------------------

class TheGateRelaxesExactlyOneGuardInExactlyOneMode(_Observed):

    def test_prod_read_only_equity_drawdown_continues_observation(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        self.assertEqual(eng._post_balance_gates(), (True, None))
        self.assertEqual(getattr(eng, FLAG), GUARD)

    def test_capital_equity_drawdown_remains_blocking(self):
        self._mode(config.PROD_CAPITAL)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        self.assertEqual(eng._post_balance_gates(), (False, GUARD))
        self.assertIsNone(getattr(eng, FLAG))

    def test_demo_equity_drawdown_remains_blocking(self):
        for mode in (config.PROD_READ_ONLY, config.PROD_CAPITAL, None):
            with self.subTest(mode=mode):
                self._mode(mode)
                self._gate_returns(False, GUARD)
                eng = _engine("demo")
                self.assertEqual(eng._post_balance_gates(), (False, GUARD))
                self.assertIsNone(getattr(eng, FLAG))

    def test_an_unreadable_mode_is_read_only_and_a_typo_is_not_capital(self):
        """Fail-closed direction for OBSERVATION: unreadable -> read-only ->
        observe; a typo must never mean CAPITAL."""
        for mode in (None, "", "READ0NLY", "capital-ish"):
            with self.subTest(mode=mode):
                self._mode(mode)
                self._gate_returns(False, GUARD)
                eng = _engine("prod")
                self.assertEqual(eng._post_balance_gates(), (True, None))

    def test_other_fail_closed_guards_remain_blocking_in_read_only(self):
        self._mode(config.PROD_READ_ONLY)
        for guard in ("persistence_failure", "contract_cap_invalid",
                      "reconciliation_mismatch", "reconciliation_unknown",
                      "reconciliation_broker_unavailable", "daily_loss_stop",
                      "consecutive_loss_breaker", "max_open_positions",
                      "max_trades_cycle", "open_risk_budget",
                      "risk_can_trade_unclassified", "kill_switch",
                      "balance_gate", "", None, "EQUITY_DRAWDOWN"):
            with self.subTest(guard=guard):
                self._gate_returns(False, guard)
                eng = _engine("prod")
                self.assertEqual(eng._post_balance_gates(), (False, guard))
                self.assertIsNone(getattr(eng, FLAG))

    def test_a_clean_verdict_passes_through_untouched(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(True, None)
        eng = _engine("prod")
        self.assertEqual(eng._post_balance_gates(), (True, None))
        self.assertIsNone(getattr(eng, FLAG))

    def test_the_real_evaluator_is_what_the_gate_consults(self):
        """Anti-vacuity: with nothing injected, the shipping evaluator runs
        and its first fail-closed gate (persistence) is honoured in
        READ_ONLY too."""
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        with patch.object(execution_engine.PersistenceSentinel, "healthy",
                          classmethod(lambda cls: False)), \
                patch.object(execution_engine.PersistenceSentinel, "failure",
                             classmethod(lambda cls: {"path": "x",
                                                      "reason": "y"})):
            self.assertEqual(eng._post_balance_gates(),
                             (False, "persistence_failure"))
        self.assertIsNone(getattr(eng, FLAG))


# --------------------------------------------------------------------------
# No state leaks between cycles.
# --------------------------------------------------------------------------

class TheFlagCannotSurviveIntoTheNextCycle(_Observed):

    def test_the_gate_resets_a_stale_flag_before_deciding(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, GUARD)                       # stale, from before
        self._gate_returns(True, None)                  # this cycle is clean
        eng._post_balance_gates()
        self.assertIsNone(getattr(eng, FLAG))

    def test_finalization_clears_the_flag_even_when_it_raises(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, GUARD)

        def boom(self, n, res, execution_path="sequential"):
            raise RuntimeError("finalization failed")

        with patch.object(ExecutionEngine, "_finalize_cycle", boom):
            with self.assertRaises(RuntimeError):
                eng._finish_cycle(1, {"report": {}, "accepted": []},
                                  "sequential")
        self.assertIsNone(getattr(eng, FLAG))

    def test_a_stale_flag_never_reaches_a_clean_cycles_evidence(self):
        """Cycle 1 observes through drawdown, cycle 2 is clean. Cycle 2's
        durable row must NOT carry cycle 1's guard."""
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        self._gate_returns(False, GUARD)
        eng._cycle_sequential(1)
        self._gate_returns(True, None)
        eng._cycle_sequential(2)
        self.assertEqual([r["would_block_capital"] for r in
                          eng.cycles_jsonl.rows], [GUARD, None])

    def test_an_exception_between_gate_and_finalization_cannot_leak(self):
        """The gate records the guard, then the scan raises before
        finalization ever runs. The next cycle -- whether it stops at the
        kill switch before the gate, or runs clean -- must not inherit it,
        on either cycle path."""
        self._mode(config.PROD_READ_ONLY)
        for path in ("sequential", "parallel"):
            with self.subTest(path=path):
                eng = _engine("prod")
                self._gate_returns(False, GUARD)
                eng.pipeline = _Pipeline(raises=RuntimeError("scan died"))
                run = (lambda n: eng._cycle_sequential(n)) \
                    if path == "sequential" else (lambda n: _parallel(eng, n))
                with self.assertRaises(RuntimeError):
                    run(1)
                eng.pipeline = _Pipeline()
                with patch.object(CFG, "KILL_SWITCH", True):
                    run(2)                       # stops before the gate
                self._gate_returns(True, None)
                run(3)                           # clean cycle
                rows = eng.cycles_jsonl.rows
                self.assertEqual([r["blocking_global_guard"] for r in rows],
                                 ["kill_switch", None])
                self.assertEqual([r["would_block_capital"] for r in rows],
                                 [None, None])
                self.assertIsNone(getattr(eng, FLAG))


# --------------------------------------------------------------------------
# Durable evidence, both cycle paths.
# --------------------------------------------------------------------------

class TheDurableRecordKeepsTheCapitalBlockFact(_Observed):

    def _assert_observed_row(self, eng):
        self.assertEqual(len(eng.cycles_jsonl.rows), 1, eng.cycles_jsonl.rows)
        row = eng.cycles_jsonl.rows[0]
        self.assertTrue(row["scan_executed"], "the scanner never ran")
        self.assertIsNone(row["blocking_global_guard"],
                          "the cycle stopped at the guard after all")
        self.assertEqual(row["would_block_capital"], GUARD,
                         "the durable row lost the fact that CAPITAL would "
                         "have refused this cycle")
        self.assertEqual(eng.pipeline.calls, 1)
        return row

    def test_sequential_read_only_observes_and_records_the_guard(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        eng._cycle_sequential(1)
        self._assert_observed_row(eng)
        state = JsonStore.load(_p("dashboard_state.json"), {})
        self.assertEqual(state.get("capital_blocking_guard"), GUARD)
        self.assertIs(state.get("capital_eligible"), False)
        self.assertIs(state.get("read_only"), True)
        report = JsonStore.load(_p("cycle_report.json"), {})
        self.assertEqual(report.get("capital_blocking_guard"), GUARD)
        self.assertIs(report.get("capital_eligible"), False)

    def test_parallel_read_only_observes_and_records_the_guard(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        _parallel(eng)
        self._assert_observed_row(eng)
        state = JsonStore.load(_p("dashboard_state.json"), {})
        self.assertEqual(state.get("capital_blocking_guard"), GUARD)
        self.assertIs(state.get("capital_eligible"), False)
        self.assertEqual(JsonStore.load(_p("cycle_report.json"), {})
                         .get("capital_blocking_guard"), GUARD)

    def test_CONTROL_capital_still_stops_before_the_scan(self):
        """Anti-vacuity: the same cycle in CAPITAL never reaches the scan and
        its row says so."""
        self._mode(config.PROD_CAPITAL)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        eng._cycle_sequential(1)
        self.assertEqual(eng.pipeline.calls, 0, "CAPITAL scanned through a cut")
        row = eng.cycles_jsonl.rows[0]
        self.assertEqual(row["blocking_global_guard"], GUARD)
        self.assertFalse(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])
        self.assertIsNone(JsonStore.load(_p("dashboard_state.json"), {})
                          .get("capital_blocking_guard"))

    def test_CONTROL_capital_parallel_still_stops(self):
        self._mode(config.PROD_CAPITAL)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        _parallel(eng)
        row = eng.cycles_jsonl.rows[0]
        self.assertEqual(row["blocking_global_guard"], GUARD)
        self.assertFalse(row["scan_executed"] and eng.executed)
        self.assertIsNone(row["would_block_capital"])
        self.assertEqual(eng.executed, [])

    def test_CONTROL_demo_still_stops(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(False, GUARD)
        eng = _engine("demo")
        eng._cycle_sequential(1)
        self.assertEqual(eng.pipeline.calls, 0)
        row = eng.cycles_jsonl.rows[0]
        self.assertEqual(row["blocking_global_guard"], GUARD)
        self.assertIsNone(row["would_block_capital"])

    def test_a_clean_cycle_records_null(self):
        """Anti-vacuity: the field is not simply always populated."""
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(True, None)
        eng = _engine("prod")
        eng._cycle_sequential(1)
        row = eng.cycles_jsonl.rows[0]
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])
        state = JsonStore.load(_p("dashboard_state.json"), {})
        self.assertIsNone(state.get("capital_blocking_guard"))
        self.assertNotIn("capital_eligible", state)
        self.assertNotIn("capital_blocking_guard",
                         JsonStore.load(_p("cycle_report.json"), {}))

    def test_an_explicit_caller_value_wins_over_the_flag(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, GUARD)
        row = eng._record_cycle_evidence(
            1, "sequential", None, pipeline={"report": {}},
            would_block_capital="max_open_positions")
        self.assertEqual(row["would_block_capital"], "max_open_positions")

    def test_the_observation_is_logged_with_the_guard_name(self):
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        with self.assertLogs("RISK", level="WARNING") as cm:
            eng._post_balance_gates()
        self.assertTrue(any("[READ_ONLY_OBSERVATION]" in m and GUARD in m
                            and "broker_writes=false" in m
                            for m in cm.output), cm.output)


# --------------------------------------------------------------------------
# The write boundary, with the guard observed through and the drawdown blown.
# --------------------------------------------------------------------------

class NoBrokerWriteWhileObservingThroughTheGuard(_Observed):
    """Real `_execute_decision`, real OrderManager, real KalshiClient with the
    transport replaced by a recorder. Three depths, every trading flag armed.
    """

    def _run(self, mode, *, allow_submission, write_auth):
        self._mode(mode)
        holder = type("H", (), {})()
        holder.boundary = shadow_iso._Boundary()
        client = shadow_iso.ReadOnlyShadowNeverEntersTheWriteLayer._client(holder)
        eng = shadow_iso.ReadOnlyShadowNeverEntersTheWriteLayer._engine(
            holder, client)
        eng.risk.rolling_drawdown = lambda: -0.48
        eng.risk.rolling_drawdown_pct = lambda: 1222.0
        # The gate has run and let observation continue:
        setattr(eng, FLAG, GUARD)
        report = {"rejections": {}}
        with patch.object(CFG, "SHADOW_MODE", False), \
                patch.object(CFG, "ALLOW_ORDER_SUBMISSION", allow_submission), \
                patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", write_auth), \
                patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                patch.object(CFG, "KILL_SWITCH", False):
            placed = eng._execute_decision(shadow_iso._Decision(), report)
        return placed, report, holder.boundary

    def test_read_only_reaches_a_complete_decision_and_writes_nothing(self):
        placed, report, b = self._run(config.PROD_READ_ONLY,
                                      allow_submission=True, write_auth=True)
        self.assertEqual(report.get("risk_passed"), 1,
                         "sizing never ran; the zeros below prove nothing")
        self.assertEqual(report.get("would_submit"), 1,
                         "no WOULD_SUBMIT: the shadow stream is empty")
        self.assertEqual(placed, 0)
        self.assertEqual(b.place_and_track, [], b.summary())
        self.assertEqual(b.create_order, [], b.summary())
        self.assertEqual(b.cancel_order, [], b.summary())
        self.assertEqual(b.mutating_http, [], str(b.http))

    def test_an_unreadable_mode_writes_nothing_either(self):
        placed, report, b = self._run("READ0NLY", allow_submission=True,
                                      write_auth=True)
        self.assertEqual(placed, 0)
        self.assertEqual(b.mutating_http, [], str(b.http))

    def test_CONTROL_capital_does_reach_the_write_layer(self):
        """Without this, a harness recording nothing would satisfy every zero."""
        placed, report, b = self._run(config.PROD_CAPITAL,
                                      allow_submission=True, write_auth=True)
        self.assertGreater(len(b.place_and_track) + len(b.create_order), 0,
                           "the recorders never move")
        self.assertGreater(len(b.mutating_http), 0,
                           "the transport recorder is not wired")


if __name__ == "__main__":
    unittest.main()
