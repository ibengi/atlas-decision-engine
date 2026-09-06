# -*- coding: utf-8 -*-
"""PRODUCTION READ_ONLY may OBSERVE through a capital risk guard.

THE DEFECT
    The live account holds about $0.04 against roughly -$0.48 of persisted
    historical PnL, so `rolling_drawdown_pct()` reads over 1200%. The equity
    drawdown guard fired before the scan and the cycle returned zero. In
    CAPITAL that is exactly right. In READ_ONLY it cut the only thing
    read-only exists for -- watching the real market and recording what the
    engine WOULD have decided -- to protect an exposure that cannot be taken.

THE RULE
    A capital risk guard is still EVALUATED and still REPORTED in READ_ONLY.
    It simply stops deciding whether the engine may LOOK. Integrity guards
    are untouched and still stop the cycle in every mode.

WHAT THESE TESTS REFUSE TO ACCEPT
    Every claim here is made by running the real code. The predicate is
    exercised across the full guard vocabulary rather than the one guard that
    prompted the change, the cycle path is the real `_cycle_sequential`, and
    the zero-write claim is measured at three depths against a real
    `OrderManager` and a real `KalshiClient` -- with a CAPITAL control that
    drives those same recorders above zero, so a broken harness cannot pass
    for a safe one.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import config
import execution_engine
from execution_engine import CAPITAL_RISK_GUARDS


#: Guards that must KEEP stopping the cycle in every mode. Integrity, not
#: risk pricing. Listed explicitly so that widening CAPITAL_RISK_GUARDS by
#: accident fails a test instead of quietly permitting more.
INTEGRITY_GUARDS = (
    "persistence_failure",
    "contract_cap_invalid",
    "reconciliation_mismatch",
    "reconciliation_unknown",
    "reconciliation_broker_unavailable",
    "kill_switch",
    "balance_gate",
    "risk_can_trade_unclassified",
    "mode_drift",
)

_MODE_VARS = ("PROD_ACCESS_MODE", "DEMO_TRADING")


class _ModeEnv(unittest.TestCase):
    """Restores every mode variable this file touches."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_VARS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:                                        # pragma: no cover
                os.environ[k] = v

    def _mode(self, value):
        if value is None:
            os.environ.pop("PROD_ACCESS_MODE", None)
        else:
            os.environ["PROD_ACCESS_MODE"] = value

    def _predicate(self, env, guard):
        """The REAL predicate, reached the way the cycle reaches it.

        The mode is captured by the shipping `_capture_access_mode` rather
        than constructed here, so these cases still exercise the whole path
        from the environment variable to the answer.
        """
        stub = type("S", (), {"client": type("C", (), {"env": env})()})()
        mode = execution_engine.ExecutionEngine._capture_access_mode(stub)
        return execution_engine.ExecutionEngine._guard_is_observation_only(
            stub, guard, mode)


class ThePredicateNamesExactlyOneSituation(_ModeEnv):

    def test_capital_risk_guards_become_observational_in_prod_read_only(self):
        self._mode(config.PROD_READ_ONLY)
        for guard in sorted(CAPITAL_RISK_GUARDS):
            self.assertTrue(
                self._predicate("prod", guard),
                f"{guard} still stops observation in PRODUCTION READ_ONLY")

    def test_the_same_guards_still_block_in_capital(self):
        """THE load-bearing case: money mode is completely unchanged."""
        self._mode(config.PROD_CAPITAL)
        for guard in sorted(CAPITAL_RISK_GUARDS):
            self.assertFalse(
                self._predicate("prod", guard),
                f"{guard} stopped blocking in CAPITAL -- real money would be "
                f"exposed through a guard that used to refuse it")

    def test_demo_is_untouched_even_with_the_mode_variable_set(self):
        """No DEMO regression, including the hand-exported-variable case.

        `--demo` and a production access mode are mutually exclusive at the
        entrypoint, but nothing stops someone exporting PROD_ACCESS_MODE
        beside a demo run. The environment test is what keeps demo honest.
        """
        for mode in (config.PROD_READ_ONLY, config.PROD_CAPITAL, None):
            self._mode(mode)
            for guard in sorted(CAPITAL_RISK_GUARDS):
                self.assertFalse(
                    self._predicate("demo", guard),
                    f"DEMO behaviour changed for {guard} with "
                    f"PROD_ACCESS_MODE={mode!r}")

    def test_integrity_guards_never_become_observational(self):
        self._mode(config.PROD_READ_ONLY)
        for guard in INTEGRITY_GUARDS:
            self.assertFalse(
                self._predicate("prod", guard),
                f"{guard} is an integrity guard and must stop the cycle in "
                f"every mode; relaxing it would let the engine reason from a "
                f"state it knows to be wrong")

    def test_an_unknown_guard_name_is_refused(self):
        """Fail closed on a guard nobody has classified yet."""
        self._mode(config.PROD_READ_ONLY)
        for guard in ("", None, "brand_new_guard", "EQUITY_DRAWDOWN"):
            self.assertFalse(self._predicate("prod", guard),
                             f"unclassified guard {guard!r} was relaxed")

    def test_the_two_guard_sets_do_not_overlap(self):
        self.assertEqual(CAPITAL_RISK_GUARDS & set(INTEGRITY_GUARDS), set())


# --------------------------------------------------------------------------
# The real cycle path.
# --------------------------------------------------------------------------

class _Pipeline:
    def __init__(self):
        self.calls = 0

    def run_cycle(self, **kw):
        self.calls += 1
        return {"report": {"cycle_id": "cyc-1", "scanned_raw": 9},
                "accepted": []}


class _Stats:
    def __init__(self):
        self.summaries = 0

    def log_summary(self):
        self.summaries += 1


class _CycleEngine:
    """The REAL `_cycle_sequential`, with only its collaborators stubbed.

    `_guard_is_observation_only`, `_report_would_block_capital` and the
    branch under test are the shipping implementations, reached exactly as
    production reaches them.
    """

    def __init__(self, env, guard="equity_drawdown"):
        self.client = type("C", (), {"env": env})()
        self.pipeline = _Pipeline()
        self.stats = _Stats()
        self.posmgr = type("P", (), {"tickers_open": lambda self: set()})()
        self.tlog = type("T", (), {"has_open_on": lambda self, t: False})()
        self.evidence = []
        self.finished = []
        self.modes = []
        self._guard = guard

    def _balance_gate(self, *a):
        return True, "solde=0.04$"

    def _post_balance_gates(self):
        return False, self._guard

    def _record_cycle_evidence(self, n, path, guard=None, detail="",
                               pipeline=None, would_block_capital=None):
        self.evidence.append({"guard": guard, "scan_executed": pipeline is not None,
                              "would_block_capital": would_block_capital})
        return {}

    def _finish_cycle(self, n, res, path, would_block_capital, mode):
        self.finished.append(would_block_capital)
        self.modes.append(mode)
        return 0

    def _capture_access_mode(self):
        return execution_engine.ExecutionEngine._capture_access_mode(self)

    def _guard_is_observation_only(self, guard, mode):
        return execution_engine.ExecutionEngine._guard_is_observation_only(
            self, guard, mode)

    def _report_would_block_capital(self, guard):
        return execution_engine.ExecutionEngine._report_would_block_capital(
            self, guard)

    def run(self, n=1):
        return execution_engine.ExecutionEngine._cycle_sequential(self, n)


class TheCycleObservesInsteadOfStopping(_ModeEnv):

    def test_1_read_only_scans_and_reports_that_capital_would_be_blocked(self):
        """Operator test 1, by execution."""
        self._mode(config.PROD_READ_ONLY)
        eng = _CycleEngine("prod")
        with self.assertLogs("RISK", level="WARNING") as logs:
            eng.run()
        self.assertEqual(eng.pipeline.calls, 1,
                         "scan_executed would be False: the scanner never ran")
        self.assertEqual(eng.finished, ["equity_drawdown"],
                         "the cycle did not reach execution carrying the "
                         "reported guard")
        self.assertEqual(eng.evidence, [],
                         "an early-return evidence row was written, so the "
                         "cycle stopped after all")
        joined = " ".join(logs.output)
        self.assertIn("WOULD_BLOCK_CAPITAL", joined)
        self.assertIn("equity_drawdown", joined)

    def test_2_capital_mode_blocks_exactly_as_before(self):
        """Operator test 2. The regression that would matter most."""
        self._mode(config.PROD_CAPITAL)
        eng = _CycleEngine("prod")
        eng.run()
        self.assertEqual(eng.pipeline.calls, 0,
                         "CAPITAL scanned through a drawdown cut")
        self.assertEqual(eng.finished, [], "CAPITAL reached execution")
        self.assertEqual(len(eng.evidence), 1)
        self.assertEqual(eng.evidence[0]["guard"], "equity_drawdown")
        self.assertFalse(eng.evidence[0]["scan_executed"])
        self.assertEqual(eng.stats.summaries, 1)

    def test_5_demo_blocks_exactly_as_before(self):
        """Operator test 5: no DEMO regression."""
        self._mode(config.PROD_READ_ONLY)     # even so: demo must not change
        eng = _CycleEngine("demo")
        eng.run()
        self.assertEqual(eng.pipeline.calls, 0)
        self.assertEqual(eng.finished, [])
        self.assertEqual(eng.evidence[0]["guard"], "equity_drawdown")
        self.assertFalse(eng.evidence[0]["scan_executed"])

    def test_an_integrity_guard_still_stops_read_only(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _CycleEngine("prod", guard="reconciliation_mismatch")
        eng.run()
        self.assertEqual(eng.pipeline.calls, 0,
                         "read-only scanned through a reconciliation halt, so "
                         "every decision would be reasoned from a position "
                         "the engine knows is wrong")
        self.assertEqual(eng.evidence[0]["guard"], "reconciliation_mismatch")

    # -- the SAME branch exists on the parallel path -----------------------

    def _parallel(self, env, guard="equity_drawdown"):
        """Drive the real `_cycle_parallel`.

        The branch under test is duplicated on both cycle paths. Testing only
        the sequential one would leave the path P8 actually selects in
        production unproven.
        """
        eng = _CycleEngine(env, guard)

        class _Future:
            def result(self):
                return (0.04, None)

        eng._executor = type("E", (), {"submit": lambda self, fn: _Future()})()
        eng._background_balance_health = lambda: (0.04, None)
        return eng, execution_engine.ExecutionEngine._cycle_parallel(eng, 1)

    def test_1p_read_only_scans_on_the_parallel_path_too(self):
        self._mode(config.PROD_READ_ONLY)
        eng, _ = self._parallel("prod")
        self.assertEqual(eng.pipeline.calls, 1)
        self.assertEqual(eng.finished, ["equity_drawdown"])
        self.assertEqual(eng.evidence, [])

    def test_2p_capital_still_blocks_on_the_parallel_path(self):
        self._mode(config.PROD_CAPITAL)
        eng, _ = self._parallel("prod")
        self.assertEqual(eng.finished, [], "CAPITAL reached execution")
        self.assertEqual(eng.evidence[0]["guard"], "equity_drawdown")
        # The parallel path scans BEFORE the gates by design, so the scan
        # having run is not evidence of a relaxation here; the early return is.
        self.assertTrue(eng.evidence[0]["scan_executed"])
        self.assertEqual(eng.stats.summaries, 1)

    def test_5p_demo_still_blocks_on_the_parallel_path(self):
        self._mode(config.PROD_READ_ONLY)
        eng, _ = self._parallel("demo")
        self.assertEqual(eng.finished, [])
        self.assertEqual(eng.evidence[0]["guard"], "equity_drawdown")

    def test_a_clean_cycle_reports_no_would_block(self):
        """Anti-vacuity: the field is not simply always populated."""
        self._mode(config.PROD_READ_ONLY)
        eng = _CycleEngine("prod")
        eng._post_balance_gates = lambda: (True, None)
        eng.run()
        self.assertEqual(eng.finished, [None])
        self.assertEqual(eng.pipeline.calls, 1)


class TheEvidenceRowCarriesTheReportedGuard(unittest.TestCase):
    """The real `_record_cycle_evidence`, so the row is the shipping row."""

    def _row(self, **kw):
        class _Sink:
            def __init__(self): self.rows = []
            def write(self, row): self.rows.append(row)

        stub = type("S", (), {})()
        stub.cycles_jsonl = _Sink()
        stub._FUNNEL_KEYS = execution_engine.ExecutionEngine._FUNNEL_KEYS
        row = execution_engine.ExecutionEngine._record_cycle_evidence(
            stub, 1, "sequential", **kw)
        self.assertEqual(stub.cycles_jsonl.rows, [row], "row was not durable")
        return row

    def test_a_shadow_cycle_records_both_facts(self):
        row = self._row(pipeline={"report": {"cycle_id": "c"}},
                        would_block_capital="equity_drawdown")
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["blocking_global_guard"])
        self.assertEqual(row["would_block_capital"], "equity_drawdown")

    def test_an_ordinary_cycle_records_neither(self):
        row = self._row(pipeline={"report": {"cycle_id": "c"}})
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])

    def test_a_blocked_cycle_is_still_distinguishable(self):
        row = self._row(blocking_global_guard="persistence_failure")
        self.assertFalse(row["scan_executed"])
        self.assertEqual(row["blocking_global_guard"], "persistence_failure")
        self.assertIsNone(row["would_block_capital"])


# --------------------------------------------------------------------------
# The new exposure: `_execute_decision` is now REACHED on a cycle whose
# capital guard fired. Before this change it never was.
# --------------------------------------------------------------------------

import test_shadow_write_layer_isolation as shadow_iso        # noqa: E402
from config import CFG                                        # noqa: E402
from unittest.mock import patch                               # noqa: E402


class NoBrokerWriteWhenObservationContinuesPastTheGuard(
        shadow_iso._IsolatedState, _ModeEnv):
    """Three depths, real `OrderManager`, real `KalshiClient`.

    The existing shadow-isolation suite already proves read-only writes
    nothing on an ORDINARY cycle. It cannot speak to this one: until now a
    blown drawdown meant `_execute_decision` was never called at all. These
    tests put the engine in exactly the production condition -- read-only,
    drawdown far past the limit -- and measure what crosses the boundary.
    """

    def setUp(self):
        # Order matters and is explicit. _IsolatedState gives this test its
        # own DATA_DIR; without it the order manager's durable session state
        # leaks in and the CAPITAL control is refused by a stale pending
        # intent rather than by the code under test -- a control that stops
        # working while still passing.
        shadow_iso._IsolatedState.setUp(self)
        _ModeEnv.setUp(self)

    def tearDown(self):
        _ModeEnv.tearDown(self)
        shadow_iso._IsolatedState.tearDown(self)

    def _harness(self):
        holder = type("H", (), {})()
        holder.boundary = shadow_iso._Boundary()
        client = shadow_iso.ReadOnlyShadowNeverEntersTheWriteLayer._client(holder)
        eng = shadow_iso.ReadOnlyShadowNeverEntersTheWriteLayer._engine(
            holder, client)
        # The real production condition: drawdown far past the limit, while
        # the per-decision gates still pass. That is precisely the state the
        # global guard used to intercept before the decision could run.
        eng.risk.rolling_drawdown = lambda: -0.48
        eng.risk.rolling_drawdown_pct = lambda: 1222.0
        return holder.boundary, eng

    def _run(self, mode, *, allow_submission, write_auth):
        self._mode(mode)
        boundary, eng = self._harness()
        report = {"rejections": {}}
        with patch.object(CFG, "SHADOW_MODE", False), \
                patch.object(CFG, "ALLOW_ORDER_SUBMISSION", allow_submission), \
                patch.object(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", write_auth), \
                patch.object(CFG, "MAX_CONTRACTS_PER_ORDER", "1"), \
                patch.object(CFG, "KILL_SWITCH", False):
            placed = eng._execute_decision(shadow_iso._Decision(), report)
        return placed, report, boundary

    def test_3_read_only_with_submission_allowed_still_writes_nothing(self):
        """Operator test 3, in the drawdown-blown state.

        `ALLOW_ORDER_SUBMISSION=true` and `LIVE_BROKER_WRITES_AUTHORIZED=true`
        are set deliberately: read-only must dominate them both. If isolation
        depended on those flags rather than on the mode, this is where it
        would show.
        """
        placed, report, boundary = self._run(
            config.PROD_READ_ONLY, allow_submission=True, write_auth=True)
        self.assertEqual(
            report.get("risk_passed"), 1,
            "the decision never reached sizing, so the zeros below would "
            "prove nothing")
        self.assertEqual(report.get("would_submit"), 1,
                         "no WOULD_SUBMIT telemetry: the shadow stream this "
                         "whole change exists to restore is still empty")
        self.assertEqual(placed, 0)
        self.assertEqual(boundary.place_and_track, [], boundary.summary())
        self.assertEqual(boundary.create_order, [], boundary.summary())
        self.assertEqual(boundary.cancel_order, [], boundary.summary())
        self.assertEqual(boundary.mutating_http, [], str(boundary.http))

    def test_CONTROL_capital_mode_does_reach_the_write_layer(self):
        """Anti-vacuity. Without this, a harness that records nothing at all
        would satisfy every zero above."""
        placed, report, boundary = self._run(
            config.PROD_CAPITAL, allow_submission=True, write_auth=True)
        self.assertGreater(
            len(boundary.place_and_track) + len(boundary.create_order), 0,
            "the recorders never move, so they cannot witness a write")
        self.assertGreater(len(boundary.mutating_http), 0,
                           "no mutating HTTP even in CAPITAL: the transport "
                           "recorder is not wired to anything")

    def test_an_unreadable_mode_writes_nothing_either(self):
        """Fail-closed: a typo must not be read as CAPITAL."""
        placed, report, boundary = self._run(
            "READ0NLY", allow_submission=True, write_auth=True)
        self.assertEqual(placed, 0)
        self.assertEqual(boundary.place_and_track, [], boundary.summary())
        self.assertEqual(boundary.mutating_http, [], str(boundary.http))


class TheStartupRefusalIsUnchanged(unittest.TestCase):
    """Operator test 4: READ_ONLY + an already-armed write authorization.

    This change touches the read-only path, so the refusal that guards it is
    re-proven here rather than assumed. A `READ_ONLY` process that starts with
    `LIVE_BROKER_WRITES_AUTHORIZED` already armed must still stop: nothing
    could mutate today, but a later `CAPITAL` start from that same environment
    would inherit an authorization nobody granted for that mode.

    The child has no credentials and cannot open a socket, so it can only
    ever reach a decision, never an account.
    """

    def test_read_only_still_refuses_an_armed_write_authorization(self):
        import json
        import subprocess
        import tempfile
        import _netblock

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tmp = tempfile.mkdtemp(prefix="atlas-startup-")
        log = os.path.join(tmp, "net")
        base = {k: v for k, v in os.environ.items()
                if k not in _netblock.STRIPPED_ENV}
        base["DATA_DIR"] = tempfile.mkdtemp(prefix="atlas-startup-data-")
        base["PROBE_PROVIDERS_ON_START"] = "0"
        env = _netblock.install(tmp, base, log)
        # AFTER install: it strips exactly these, which is the point of it.
        env["KALSHI_ENV_CONFIRM"] = "LIVE"
        env["LIVE_BROKER_WRITES_AUTHORIZED"] = "1"

        proc = subprocess.run(
            [sys.executable, "kalshi_alpha_bot.py", "--loop",
             "--live-read-only"],
            cwd=root, env=env, capture_output=True, text=True, timeout=180)

        self.assertNotEqual(proc.returncode, 0,
                            "startup accepted an armed write authorization")
        messages = []
        for line in (proc.stdout + proc.stderr).splitlines():
            try:
                messages.append(json.loads(line).get("message", ""))
            except Exception:
                messages.append(line)
        joined = " ".join(messages)
        self.assertIn(
            "LIVE_BROKER_WRITES_AUTHORIZED", joined,
            "the process exited non-zero for some OTHER reason; a production "
            "start without credentials also exits non-zero, so the exit code "
            "alone proves nothing")
        self.assertNotIn(
            "identifiants invalides", joined,
            "it stopped at the credential gate instead of the authorization "
            "refusal, so this test would pass with the refusal deleted")
        self.assertEqual(_netblock.attempts(log), [],
                         "the child attempted an outbound connection")


# --------------------------------------------------------------------------
# The SEAM. `_finish_cycle` is what carries the reported guard from the cycle
# into the durable row, and nothing above tests it: the evidence tests call
# `_record_cycle_evidence` directly, and the cycle tests stub `_finish_cycle`.
# Deleting `would_block_capital=` from that one call therefore left the whole
# suite green while the durable record silently lost the only machine-readable
# statement that CAPITAL would have refused the cycle.
#
# These tests run the REAL `_cycle_sequential` / `_cycle_parallel`, the REAL
# `_finish_cycle` and the REAL `_record_cycle_evidence`, and read the row back
# out of the sink it was written to.
# --------------------------------------------------------------------------

class _DurableSink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        # A durable writer serializes. Anything unserializable would be lost
        # on the real path, so it must fail here too.
        self.rows.append(json.loads(json.dumps(row)))


class _FullCycleEngine:
    """Enough collaborators for the REAL finalization path to run.

    Nothing on the evidence path is stubbed: `_finish_cycle`,
    `_record_cycle_evidence` and `_FUNNEL_KEYS` are the shipping articles, and
    the row is read back from the sink rather than from a return value.
    """

    _FUNNEL_KEYS = execution_engine.ExecutionEngine._FUNNEL_KEYS

    def __init__(self, env="prod", guard="equity_drawdown", gates_ok=False):
        self.client = type("C", (), {"env": env})()
        self.cycles_jsonl = _DurableSink()
        self.stats = _Stats()
        self.posmgr = type("P", (), {"tickers_open": lambda self: set()})()
        self.tlog = type("T", (), {"has_open_on": lambda self, t: False})()
        self.orders = type("O", (), {"exchange_pause_until": 0.0})()
        self.scanner = type("S", (), {"shadow_population": lambda self: []})()
        self.btc_daily_shadow = type(
            "B", (), {"run": lambda self, pop, cid: {}})()
        self.capital = 0.04
        self.configured_capital = 0.04
        self.last_balance = 0.04
        self.pipeline = _FullPipeline()
        self._guard = guard
        self._gates_ok = gates_ok
        self.executed = []
        self.drift_to = None

    # -- collaborators the cycle needs before the gates -------------------
    def _balance_gate(self, *a):
        return True, "solde=0.04$"

    def _post_balance_gates(self):
        return (True, None) if self._gates_ok else (False, self._guard)

    def _execute_decision(self, dec, report):
        self.executed.append(dec)
        return 0

    # -- everything below is the shipping implementation ------------------
    def _capture_access_mode(self):
        mode = execution_engine.ExecutionEngine._capture_access_mode(self)
        # A test hook, used ONLY by the drift cases: the second capture --
        # the one `_finish_cycle` takes to compare -- sees a changed
        # environment, exactly as it would if something rewrote the variable
        # mid-cycle.
        if self.drift_to is not None:
            os.environ["PROD_ACCESS_MODE"] = self.drift_to
            self.drift_to = None
        return mode

    def _guard_is_observation_only(self, guard, mode):
        return execution_engine.ExecutionEngine._guard_is_observation_only(
            self, guard, mode)

    def _report_would_block_capital(self, guard):
        return execution_engine.ExecutionEngine._report_would_block_capital(
            self, guard)

    def _record_cycle_evidence(self, *a, **kw):
        return execution_engine.ExecutionEngine._record_cycle_evidence(
            self, *a, **kw)

    def _finish_cycle(self, *a, **kw):
        return execution_engine.ExecutionEngine._finish_cycle(self, *a, **kw)

    def sequential(self, n=1):
        return execution_engine.ExecutionEngine._cycle_sequential(self, n)

    def parallel(self, n=1):
        self._executor = type(
            "E", (), {"submit": lambda self, fn: type(
                "F", (), {"result": lambda self: (0.04, None)})()})()
        self._background_balance_health = lambda: (0.04, None)
        return execution_engine.ExecutionEngine._cycle_parallel(self, n)


class _FullPipeline:
    def __init__(self):
        self.calls = 0

    def run_cycle(self, **kw):
        self.calls += 1
        return {"report": {"cycle_id": "cyc-seam", "scanned_raw": 9,
                           "scanned": 9, "ranker_eligible": 3,
                           "accepted": 0, "rejections": {}},
                "accepted": []}


class TheDurableRowKeepsTheCapitalBlockFact(
        shadow_iso._IsolatedState, _ModeEnv):
    """A READ_ONLY cycle that observed past a capital guard must SAY SO in
    the durable record, not only in a log line.

    Mutation this pins: deleting `would_block_capital=` from the
    `_finish_cycle` -> `_record_cycle_evidence` call.
    """

    def setUp(self):
        shadow_iso._IsolatedState.setUp(self)
        _ModeEnv.setUp(self)

    def tearDown(self):
        _ModeEnv.tearDown(self)
        shadow_iso._IsolatedState.tearDown(self)

    def _only_row(self, eng):
        self.assertEqual(len(eng.cycles_jsonl.rows), 1,
                         f"expected exactly one durable row, got "
                         f"{eng.cycles_jsonl.rows}")
        return eng.cycles_jsonl.rows[0]

    def test_sequential_row_names_the_guard_capital_would_have_obeyed(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine()
        eng.sequential()
        row = self._only_row(eng)
        self.assertTrue(row["scan_executed"],
                        "the scan did not run, so this is not the cycle "
                        "under test")
        self.assertIsNone(row["blocking_global_guard"],
                          "the cycle stopped at the guard after all")
        self.assertEqual(
            row["would_block_capital"], "equity_drawdown",
            "the durable row lost the fact that CAPITAL would have refused "
            "this cycle -- the log line alone is not the record")

    def test_parallel_row_names_it_too(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine()
        eng.parallel()
        row = self._only_row(eng)
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["blocking_global_guard"])
        self.assertEqual(row["would_block_capital"], "equity_drawdown")

    def test_every_capital_guard_survives_the_seam(self):
        """Not just the one guard that prompted the change."""
        for guard in sorted(CAPITAL_RISK_GUARDS):
            with self.subTest(guard=guard):
                self._mode(config.PROD_READ_ONLY)
                eng = _FullCycleEngine(guard=guard)
                eng.sequential()
                self.assertEqual(self._only_row(eng)["would_block_capital"],
                                 guard)

    def test_ANTIVACUITY_a_clean_cycle_writes_null(self):
        """The field is not simply always populated: a cycle no capital guard
        objected to must record `None`, or the assertions above would pass
        against a constant."""
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine(gates_ok=True)
        eng.sequential()
        row = self._only_row(eng)
        self.assertTrue(row["scan_executed"])
        self.assertIsNone(row["would_block_capital"])

    def test_finalization_refuses_to_run_without_the_facts_it_carries(self):
        """`would_block_capital` and `mode` are facts only the caller has.

        `_finish_cycle` cannot re-derive either honestly -- re-reading the
        mode is the very drift this change exists to stop, and the reported
        guard exists nowhere else by then. So both are REQUIRED parameters,
        and a caller that omits one fails loudly here rather than finalizing
        a cycle it cannot describe. Asserted by calling, not by inspecting
        the signature: the contract is what the code does when you break it.
        """
        eng = _FullCycleEngine()
        res = _FullPipeline().run_cycle()
        finish = execution_engine.ExecutionEngine._finish_cycle
        with self.assertRaises(TypeError):
            finish(eng, 1, res, "sequential")
        with self.assertRaises(TypeError):
            finish(eng, 1, res, "sequential", "equity_drawdown")
        self.assertEqual(eng.cycles_jsonl.rows, [],
                         "a cycle was finalized despite the refusal")

    def test_the_row_is_serializable_so_it_can_actually_be_durable(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine()
        eng.sequential()
        json.dumps(self._only_row(eng))


class TheModeCannotDriftBetweenObservationAndAction(
        shadow_iso._IsolatedState, _ModeEnv):
    """The cycle is AUTHORIZED under one reading of PROD_ACCESS_MODE and then
    ACTS under another. Both directions are refused before any decision runs.

    `PROD_ACCESS_MODE` is an environment variable, so it is mutable for the
    life of the process. Reading it twice in one cycle reads two facts that
    merely share a name.
    """

    def setUp(self):
        shadow_iso._IsolatedState.setUp(self)
        _ModeEnv.setUp(self)

    def tearDown(self):
        _ModeEnv.tearDown(self)
        shadow_iso._IsolatedState.tearDown(self)

    def _drifted(self, start, finish, gates_ok=False, env="prod"):
        self._mode(start)
        eng = _FullCycleEngine(env=env, gates_ok=gates_ok)
        eng.drift_to = finish
        eng.sequential()
        return eng

    def test_a_read_only_cycle_cannot_finalize_as_capital(self):
        """The dangerous direction: relaxed past a capital guard, then acting
        under an authorization that would have refused the relaxation."""
        eng = self._drifted(config.PROD_READ_ONLY, config.PROD_CAPITAL)
        self.assertEqual(eng.executed, [],
                         "a decision was executed on a cycle whose mode "
                         "changed under it")
        row = eng.cycles_jsonl.rows[-1]
        self.assertEqual(row["blocking_global_guard"], "mode_drift")
        self.assertEqual(
            row["would_block_capital"], "equity_drawdown",
            "the drift stop threw away the capital-guard fact it was "
            "supposed to preserve")
        self.assertTrue(row["scan_executed"],
                        "the observation that DID happen was lost")

    def test_a_capital_cycle_cannot_finalize_as_read_only(self):
        eng = self._drifted(config.PROD_CAPITAL, config.PROD_READ_ONLY,
                            gates_ok=True)
        self.assertEqual(eng.executed, [])
        self.assertEqual(eng.cycles_jsonl.rows[-1]["blocking_global_guard"],
                         "mode_drift")

    def test_capital_drifting_to_an_unreadable_value_fails_closed(self):
        """An unreadable mode reads as read-only, so this is a real change of
        authorization and must stop the cycle."""
        eng = self._drifted(config.PROD_CAPITAL, "READ0NLY", gates_ok=True)
        self.assertEqual(eng.executed, [])
        self.assertEqual(eng.cycles_jsonl.rows[-1]["blocking_global_guard"],
                         "mode_drift")

    def test_two_spellings_of_the_same_authorization_are_not_drift(self):
        """Anti-vacuity in the other direction: the check must not stop a
        cycle whose EFFECTIVE authorization never moved, or it would halt
        production on a cosmetic difference."""
        eng = self._drifted(config.PROD_READ_ONLY, "READ0NLY")
        row = eng.cycles_jsonl.rows[-1]
        self.assertIsNone(row["blocking_global_guard"],
                          "an unchanged authorization was reported as drift")
        self.assertEqual(row["would_block_capital"], "equity_drawdown")

    def test_no_drift_is_the_ordinary_case(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine()
        eng.sequential()
        self.assertIsNone(
            eng.cycles_jsonl.rows[-1]["blocking_global_guard"])

    def test_DEMO_is_not_subject_to_the_drift_check(self):
        """Demo's mode does not come from PROD_ACCESS_MODE, so changing that
        variable beside a demo run must not stop a demo cycle."""
        eng = self._drifted(config.PROD_READ_ONLY, config.PROD_CAPITAL,
                            gates_ok=True, env="demo")
        row = eng.cycles_jsonl.rows[-1]
        self.assertIsNone(row["blocking_global_guard"],
                          "DEMO behaviour changed")
        self.assertTrue(row["scan_executed"])

    def test_the_parallel_path_refuses_drift_too(self):
        self._mode(config.PROD_READ_ONLY)
        eng = _FullCycleEngine()
        eng.drift_to = config.PROD_CAPITAL
        eng.parallel()
        self.assertEqual(eng.executed, [])
        self.assertEqual(eng.cycles_jsonl.rows[-1]["blocking_global_guard"],
                         "mode_drift")

    def test_the_captured_mode_is_immutable(self):
        """A carried value that can be edited in flight is not a snapshot."""
        self._mode(config.PROD_READ_ONLY)
        stub = type("S", (), {"client": type("C", (), {"env": "prod"})()})()
        mode = execution_engine.ExecutionEngine._capture_access_mode(stub)
        with self.assertRaises(AttributeError):
            mode.read_only = False

    def test_mode_drift_is_never_observational(self):
        self._mode(config.PROD_READ_ONLY)
        stub = type("S", (), {"client": type("C", (), {"env": "prod"})()})()
        mode = execution_engine.ExecutionEngine._capture_access_mode(stub)
        self.assertFalse(
            execution_engine.ExecutionEngine._guard_is_observation_only(
                stub, execution_engine.MODE_DRIFT_GUARD, mode))
        self.assertNotIn(execution_engine.MODE_DRIFT_GUARD,
                         CAPITAL_RISK_GUARDS)

    def test_an_unrecognised_environment_is_treated_as_PRODUCTION(self):
        """"Not demo", never "is prod".

        The write boundary reads the environment this way on purpose: only
        the string `demo` earns demo's exemption, and anything else -- a
        future environment name, an unset value, a typo -- is production.
        Rewriting the test as `== "prod"` would be safe HERE (an unknown
        environment would simply stop being relaxed) but it would break the
        shared idiom, and the same rewrite on the write boundary is not safe
        at all. Pinning it here keeps the two spellings from diverging.
        """
        self._mode(config.PROD_READ_ONLY)
        for env in ("prod", "staging", "", None, "PROD"):
            with self.subTest(environment=env):
                stub = type("S", (), {
                    "client": type("C", (), {"env": env})()})()
                mode = execution_engine.ExecutionEngine._capture_access_mode(
                    stub)
                self.assertTrue(
                    mode.observation_only_allowed,
                    f"environment {env!r} is not demo, so it must be treated "
                    f"as production")
        stub = type("S", (), {"client": type("C", (), {"env": "demo"})()})()
        mode = execution_engine.ExecutionEngine._capture_access_mode(stub)
        self.assertFalse(mode.observation_only_allowed,
                         "only 'demo' earns demo's exemption")
