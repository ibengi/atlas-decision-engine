# -*- coding: utf-8 -*-
"""PROD READ_ONLY may observe through `equity_drawdown`; nothing else changes.

The bootstrap wrapper patches three ExecutionEngine methods at process start.
These tests install it the way `main()` does and then drive the REAL
`_cycle_sequential`, `_cycle_parallel`, `_finish_cycle`,
`_record_cycle_evidence` and `_execute_decision`, reading every claim back
from the durable sink, the dashboard file, or a recorder at the write
boundary. Only the gate VERDICT is injected (the drawdown arithmetic behind
it is pinned in test_p0_observability); everything downstream of the verdict
is the shipping code.

Written as unittest.TestCase so that `python run_tests.py` -- the runner the
Docker build and the LIVE gate rely on -- collects them. Fixture-taking
module-level functions are refused by that collector by design.
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
import read_only_dashboard_bootstrap as bootstrap                 # noqa: E402
import test_shadow_write_layer_isolation as shadow_iso            # noqa: E402
from config import CFG, _p                                        # noqa: E402
from execution_engine import ExecutionEngine                      # noqa: E402
from persistence import JsonStore                                 # noqa: E402

GUARD = bootstrap.OBSERVATION_ONLY_GUARD
FLAG = bootstrap._CAPITAL_GUARD_ATTR


class _Installed(shadow_iso._IsolatedState, unittest.TestCase):
    """Wrapper installed for the test, removed after; own DATA_DIR; mode
    variables saved and restored."""

    def setUp(self):
        shadow_iso._IsolatedState.setUp(self)
        bootstrap.install()
        self._saved_gate = bootstrap._original_post_balance_gates

    def tearDown(self):
        bootstrap._original_post_balance_gates = self._saved_gate
        bootstrap.uninstall()
        shadow_iso._IsolatedState.tearDown(self)

    def _mode(self, value):
        if value is None:
            os.environ.pop("PROD_ACCESS_MODE", None)
        else:
            os.environ["PROD_ACCESS_MODE"] = value

    def _gate_returns(self, ok, guard):
        """Inject the ORIGINAL gate's verdict for this cycle."""
        bootstrap._original_post_balance_gates = lambda self: (ok, guard)


class _Sink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        # A durable writer serializes; an unserializable row must fail here.
        self.rows.append(json.loads(json.dumps(row)))


class _Pipeline:
    def __init__(self):
        self.calls = 0

    def run_cycle(self, **kw):
        self.calls += 1
        return {"report": {"cycle_id": "cyc-ro", "scanned_raw": 9,
                           "scanned": 9, "ranker_eligible": 1, "accepted": 0,
                           "rejections": {}},
                "accepted": []}


def _engine(env="prod"):
    """A real ExecutionEngine instance with only its collaborators stubbed.

    Being a real instance matters: the wrapper patches the CLASS, so the
    instance must resolve `_post_balance_gates`, `_record_cycle_evidence` and
    `_finish_cycle` through it, exactly as production does.
    """
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


# --------------------------------------------------------------------------
# The gate verdict.
# --------------------------------------------------------------------------

class TheGateRelaxesExactlyOneGuardInExactlyOneMode(_Installed):

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
        observe; but the wrapper must still never let a typo mean CAPITAL."""
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


# --------------------------------------------------------------------------
# No state leaks between cycles.
# --------------------------------------------------------------------------

class TheFlagCannotSurviveIntoTheNextCycle(_Installed):

    def test_the_gate_resets_a_stale_flag_before_deciding(self):
        """A flag left by an earlier cycle must not count for this one."""
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, "equity_drawdown")          # stale, from before
        self._gate_returns(True, None)                  # this cycle is clean
        eng._post_balance_gates()
        self.assertIsNone(getattr(eng, FLAG))

    def test_finalization_clears_the_flag_even_when_it_raises(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, "equity_drawdown")

        def boom(self, n, res, *a, **k):
            raise RuntimeError("finalization failed")

        with patch.object(bootstrap, "_original_finish_cycle", boom):
            with self.assertRaises(RuntimeError):
                eng._finish_cycle(1, {"report": {}, "accepted": []},
                                  "sequential")
        self.assertIsNone(getattr(eng, FLAG))

    def test_a_stale_flag_never_reaches_a_clean_cycles_evidence(self):
        """End to end: cycle 1 observes through drawdown, cycle 2 is clean.
        Cycle 2's durable row must NOT carry cycle 1's guard."""
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        self._gate_returns(False, GUARD)
        eng._cycle_sequential(1)
        self._gate_returns(True, None)
        eng._cycle_sequential(2)
        rows = eng.cycles_jsonl.rows
        self.assertEqual([r["would_block_capital"] for r in rows],
                         [GUARD, None])


# --------------------------------------------------------------------------
# Durable evidence, both cycle paths.
# --------------------------------------------------------------------------

class TheDurableRecordKeepsTheCapitalBlockFact(_Installed):

    def _parallel(self, eng, n=1):
        eng._executor = type("E", (), {"submit": lambda self, fn: type(
            "F", (), {"result": lambda self: (0.04, None)})()})()
        eng._background_balance_health = lambda: (0.04, None)
        return eng._cycle_parallel(n)

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
        self._parallel(eng)
        self._assert_observed_row(eng)
        self.assertEqual(
            JsonStore.load(_p("dashboard_state.json"), {})
            .get("capital_blocking_guard"), GUARD)

    def test_CONTROL_capital_still_stops_before_the_scan(self):
        """Anti-vacuity: the same cycle in CAPITAL never reaches the scan and
        its row says so. Without this the assertions above could pass
        against a harness that never blocks anything."""
        self._mode(config.PROD_CAPITAL)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        eng._cycle_sequential(1)
        self.assertEqual(eng.pipeline.calls, 0, "CAPITAL scanned through a cut")
        row = eng.cycles_jsonl.rows[0]
        self.assertEqual(row["blocking_global_guard"], GUARD)
        self.assertFalse(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])
        self.assertIsNone(
            JsonStore.load(_p("dashboard_state.json"), {})
            .get("capital_blocking_guard"))

    def test_CONTROL_capital_parallel_still_stops(self):
        self._mode(config.PROD_CAPITAL)
        self._gate_returns(False, GUARD)
        eng = _engine("prod")
        self._parallel(eng)
        row = eng.cycles_jsonl.rows[0]
        self.assertEqual(row["blocking_global_guard"], GUARD)
        self.assertIsNone(row["would_block_capital"])
        self.assertEqual(eng.executed, [])

    def test_a_clean_cycle_records_null(self):
        """Anti-vacuity: the field is not simply always populated."""
        self._mode(config.PROD_READ_ONLY)
        self._gate_returns(True, None)
        eng = _engine("prod")
        eng._cycle_sequential(1)
        row = eng.cycles_jsonl.rows[0]
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])

    def test_an_explicit_engine_value_wins_over_the_flag(self):
        """If the engine itself carries the fact one day, the wrapper defers."""
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        setattr(eng, FLAG, GUARD)
        row = eng._record_cycle_evidence(
            1, "sequential", None, pipeline={"report": {}},
            would_block_capital="max_open_positions")
        self.assertEqual(row["would_block_capital"], "max_open_positions")


# --------------------------------------------------------------------------
# Interface: the finalization wrapper is signature-agnostic.
# --------------------------------------------------------------------------

class TheWrapperDoesNotDependOnTheEnginesArity(_Installed):

    def test_extra_positional_and_keyword_arguments_pass_through(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        seen = {}

        def five_arg_finish(self, n, res, execution_path, would_block_capital,
                            mode):
            seen.update(n=n, path=execution_path, wbc=would_block_capital,
                        mode=mode)
            return 7

        with patch.object(bootstrap, "_original_finish_cycle",
                          five_arg_finish):
            out = eng._finish_cycle(42, {"report": {}, "accepted": []},
                                    "parallel", "equity_drawdown", "MODE")
        self.assertEqual(out, 7)
        self.assertEqual(seen, {"n": 42, "path": "parallel",
                                "wbc": "equity_drawdown", "mode": "MODE"})

    def test_main_style_three_argument_call_still_works(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _engine("prod")
        self._gate_returns(True, None)
        out = eng._finish_cycle(3, _Pipeline().run_cycle(), "sequential")
        self.assertEqual(out, 0)
        self.assertEqual(len(eng.cycles_jsonl.rows), 1)


# --------------------------------------------------------------------------
# The write boundary, with the wrapper installed and the drawdown blown.
# --------------------------------------------------------------------------

class NoBrokerWriteWhileObservingThroughTheGuard(_Installed):
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
        # The wrapper's gate has run and let observation continue:
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


class InstallIsReversible(unittest.TestCase):

    def test_uninstall_restores_the_originals(self):
        bootstrap.install()
        self.assertIs(ExecutionEngine._post_balance_gates,
                      bootstrap._post_balance_gates_observation_aware)
        bootstrap.uninstall()
        self.assertIs(ExecutionEngine._post_balance_gates,
                      bootstrap._original_post_balance_gates)
        self.assertIs(ExecutionEngine._finish_cycle,
                      bootstrap._original_finish_cycle)
        self.assertIs(ExecutionEngine._record_cycle_evidence,
                      bootstrap._original_record_cycle_evidence)


if __name__ == "__main__":
    unittest.main()
