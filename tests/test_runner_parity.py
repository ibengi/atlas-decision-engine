# -*- coding: utf-8 -*-
"""The build's suite must be the WHOLE suite (F1).

`run_tests.py` writes `test_report.json`; `model_gatekeeper.check_live_allowed()`
reads it before permitting LIVE. If the build's runner collects fewer tests than
the suite actually contains, that artifact under-reports the evidence the last
lock before real money consults — and it does so silently, because a smaller
green report looks exactly like a complete green report.

These tests pin the parity itself, not a number. Nothing here asserts 799: the
totals are compared against each other, so the suite can grow without editing
this file, and cannot silently split again.
"""

import os
import subprocess
import sys
import unittest

import _bootstrap  # noqa: F401

try:
    from tests import _collect
except ImportError:                     # pragma: no cover - depends on runner
    import _collect

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pytest_count():
    """Tests the REAL pytest collects, or None when pytest is unavailable.

    Deliberately shells out to pytest rather than re-deriving its rules: a
    hand-rolled model of pytest's collection would share _collect.py's blind
    spots and agree with it precisely where both were wrong.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=_ROOT, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):       # pragma: no cover
        return None
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if "test" in line and "collected" in line:
            for token in line.split():
                if token.isdigit():
                    return int(token)
    return None                                        # pragma: no cover


class BuildSuiteIsTheWholeSuite(unittest.TestCase):

    def test_canonical_collection_matches_real_pytest(self):
        """COUNTS_MATCH: what the build runs == what pytest sees."""
        pc = _pytest_count()
        if pc is None:
            self.skipTest("pytest unavailable; parity unverifiable here")
        _, diag = _collect.collect("tests")
        self.assertEqual(
            diag["total"], pc,
            f"build collects {diag['total']}, pytest collects {pc}. The "
            f"artifact the LIVE gate reads would describe a different suite "
            f"than the one developers run.")

    def test_plain_unittest_discovery_alone_is_not_enough(self):
        """Anti-vacuity: the collector must actually ADD something.

        If this ever reads zero, the parity above would be satisfied by
        unittest already seeing everything — in which case this whole module
        is dead weight and should be removed deliberately, not left passing
        for a reason that no longer holds.
        """
        _, diag = _collect.collect("tests")
        self.assertGreater(
            diag["module_level_added"], 0,
            "no module-level tests were added; either the suite changed style "
            "or the collector stopped working — both need a human decision")
        self.assertEqual(
            diag["total"], diag["discovered_by_unittest"] + diag["module_level_added"])

    def test_the_calibration_tests_are_in_the_built_suite(self):
        """The five that were missing are the ones the LIVE rule depends on."""
        suite, _ = _collect.collect("tests")
        ids = set()

        def walk(s):
            for t in s:
                walk(t) if isinstance(t, unittest.TestSuite) else ids.add(t.id())

        walk(suite)
        for name in ("test_brier_and_ece_known_values",
                     "test_calibration_curve_has_empty_bins_and_includes_one",
                     "test_collect_pairs_accepts_shadow_records_and_skips_unresolved",
                     "test_analyze_is_json_friendly_and_aliases_work",
                     "test_invalid_bin_count_rejected"):
            self.assertTrue(
                any(i.endswith(name) for i in ids),
                f"{name} absent from the suite the build reports on")

    def test_nothing_is_collectable_only_by_pytest(self):
        """A pytest-only test would silently re-open the gap."""
        _, diag = _collect.collect("tests")
        self.assertEqual(
            diag["uncollectable"], [],
            "these run under pytest but not under the build's runner, so "
            "test_report.json would under-report the suite")


class CollectorRefusesWhatItCannotRun(unittest.TestCase):
    """The collector must report, not skip, what it cannot execute."""

    def _module(self, source):
        import textwrap
        import types
        mod = types.ModuleType("tests.test_synthetic_probe")
        mod.__file__ = os.path.join(_ROOT, "tests", "test_synthetic_probe.py")
        exec(compile(textwrap.dedent(source), mod.__file__, "exec"), mod.__dict__)
        return mod

    def test_a_parameterised_module_level_test_is_reported(self):
        mod = self._module("def test_needs_a_fixture(tmp_path):\n    pass\n")
        found = [(n, f) for n, f in _collect._module_level_tests(mod)]
        self.assertEqual([n for n, _ in found], ["test_needs_a_fixture"])
        import inspect
        self.assertTrue(inspect.signature(found[0][1]).parameters,
                        "probe must actually declare a parameter")

    def test_wrapped_functions_really_execute(self):
        """Anti-vacuity: a wrapped failing test must FAIL, not be skipped."""
        mod = self._module(
            "def test_passes():\n    assert True\n"
            "def test_fails():\n    raise AssertionError('boom')\n")
        funcs = list(_collect._module_level_tests(mod))
        case = _collect._case_for(mod, funcs)
        res = unittest.TextTestRunner(
            stream=open(os.devnull, "w"), verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(case))
        self.assertEqual(res.testsRun, 2)
        self.assertEqual(len(res.failures), 1)
        self.assertIn("boom", res.failures[0][1])

    def test_an_imported_helper_is_not_double_counted(self):
        """A `test_*` imported from elsewhere belongs to its defining module."""
        mod = self._module(
            "from tests.test_calibration import test_brier_and_ece_known_values\n"
            "def test_local():\n    pass\n")
        names = [n for n, _ in _collect._module_level_tests(mod)]
        self.assertEqual(names, ["test_local"])


class UnrunnableTestKindsCannotReportGreen(unittest.TestCase):
    """RC-1 M1: a test whose body never ran must never be counted as passing.

    Before this closed, a module-level `async def test_*` was wrapped, counted
    and "passed" without its body executing: calling it merely built a
    coroutine. Reproduced on e377df6 with a deliberately failing async test —
    pytest said `1 failed`, `run_tests.py` said `ran:884 failures:0` and exited
    0, writing a green `test_report.json` for a test that never ran. That
    artifact is what `model_gatekeeper.check_live_allowed()` reads.

    These tests are behavioural: each one runs the collector (or the real
    build runner) against a probe that FAILS if its body executes, and asserts
    the outcome is red or refused — never green.
    """

    def _module(self, source):
        import textwrap
        import types
        mod = types.ModuleType("tests.test_synthetic_probe")
        mod.__file__ = os.path.join(_ROOT, "tests", "test_synthetic_probe.py")
        exec(compile(textwrap.dedent(source), mod.__file__, "exec"), mod.__dict__)
        return mod

    # ---- control -------------------------------------------------------

    def test_a_plain_sync_module_level_test_is_still_collected(self):
        """ANTI-VACUITY CONTROL. Without this, refusing everything would pass.

        The refusals below are only meaningful if the ordinary case still
        works: a normal zero-argument sync `test_*` must still be collected
        AND still actually execute its body.
        """
        mod = self._module(
            "def test_ordinary():\n    pass\n"
            "def test_ordinary_failing():\n    raise AssertionError('ran')\n")
        names = [n for n, f in _collect._module_level_tests(mod)
                 if _collect._unsupported_kind(f) is None]
        self.assertEqual(names, ["test_ordinary", "test_ordinary_failing"],
                         "ordinary sync tests must remain collectable")
        case = _collect._case_for(mod, list(_collect._module_level_tests(mod)))
        res = unittest.TextTestRunner(
            stream=open(os.devnull, "w"), verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(case))
        self.assertEqual(res.testsRun, 2)
        self.assertEqual(len(res.failures), 1,
                         "the sync body must really execute, not be skipped")
        self.assertIn("ran", res.failures[0][1])

    # ---- the refusals --------------------------------------------------

    def test_an_async_module_level_test_is_refused_not_counted(self):
        """ASYNC_SILENT_PASS_CLOSED, at the collector boundary."""
        mod = self._module(
            "async def test_async_probe():\n"
            "    raise AssertionError('body ran')\n")
        func = dict(_collect._module_level_tests(mod))["test_async_probe"]
        self.assertIsNotNone(
            _collect._unsupported_kind(func),
            "an async def test must be refused; wrapping it would count a "
            "test whose body never runs")

    def test_a_generator_module_level_test_is_refused_not_counted(self):
        """GENERATOR_SILENT_PASS_CLOSED, at the collector boundary."""
        mod = self._module(
            "def test_generator_probe():\n"
            "    yield\n"
            "    raise AssertionError('body ran')\n")
        func = dict(_collect._module_level_tests(mod))["test_generator_probe"]
        self.assertIsNotNone(_collect._unsupported_kind(func))

    def test_an_async_generator_module_level_test_is_refused(self):
        """`iscoroutinefunction` is False for an `async def` that yields."""
        mod = self._module(
            "async def test_async_gen_probe():\n"
            "    yield\n"
            "    raise AssertionError('body ran')\n")
        func = dict(_collect._module_level_tests(mod))["test_async_gen_probe"]
        self.assertIsNotNone(_collect._unsupported_kind(func))

    def test_refused_kinds_are_reported_by_collect_not_silently_dropped(self):
        """Refusing must be LOUD: silently dropping would also hide the test.

        `uncollectable` is what `run_tests.py` exits 1 on and what
        `test_nothing_is_collectable_only_by_pytest` asserts is empty. A kind
        that were merely skipped would leave both green, which is the same
        false-green this whole module exists to prevent.

        Uses a real file in `tests/` because `_collect.collect` imports by
        package name: a temporary directory would resolve back to the already
        imported `tests` package and prove nothing.
        """
        probe = os.path.join(_ROOT, "tests", "test_zz_kind_probe.py")
        if os.path.exists(probe):                     # pragma: no cover
            self.skipTest("probe file already present")
        baseline = _collect.collect("tests")[1]["module_level_added"]
        with open(probe, "w") as fh:
            fh.write("async def test_async_probe():\n"
                     "    raise AssertionError('body ran')\n"
                     "def test_gen_probe():\n"
                     "    yield\n"
                     "def test_sync_probe():\n"
                     "    pass\n")
        try:
            sys.modules.pop("tests.test_zz_kind_probe", None)
            _, diag = _collect.collect("tests")
        finally:
            os.remove(probe)
            sys.modules.pop("tests.test_zz_kind_probe", None)
        joined = " ".join(diag["uncollectable"])
        self.assertIn("test_async_probe", joined)
        self.assertIn("test_gen_probe", joined)
        self.assertNotIn("test_sync_probe", joined,
                         "control: the ordinary sync probe must NOT be refused")
        self.assertEqual(
            diag["module_level_added"], baseline + 1,
            "exactly the one sync probe may be added; the async and generator "
            "probes must not be counted as runnable tests")

    # ---- the runtime backstop -----------------------------------------

    def test_a_decorated_async_test_fails_rather_than_passing(self):
        """The case static inspection CANNOT see.

        `functools.wraps` around an `async def` reports
        `iscoroutinefunction() is False`, so the collector accepts it — and
        calling it returns an un-awaited coroutine. Real pytest FAILS this
        exact case, so failing it here is parity, not extra strictness.
        """
        mod = self._module(
            "import functools\n"
            "def _wrap(f):\n"
            "    @functools.wraps(f)\n"
            "    def inner():\n"
            "        return f()\n"
            "    return inner\n"
            "@_wrap\n"
            "async def test_decorated_async():\n"
            "    raise AssertionError('body ran')\n")
        func = dict(_collect._module_level_tests(mod))["test_decorated_async"]
        self.assertIsNone(
            _collect._unsupported_kind(func),
            "premise of this test: static inspection does NOT catch this one")
        case = _collect._case_for(mod, [("test_decorated_async", func)])
        res = unittest.TextTestRunner(
            stream=open(os.devnull, "w"), verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(case))
        self.assertEqual(len(res.failures) + len(res.errors), 1,
                         "an un-awaited coroutine result must fail the test")
        self.assertIn("never awaited", (res.failures + res.errors)[0][1])

    def test_a_test_returning_a_plain_value_still_passes(self):
        """Parity guard: real pytest only WARNS on a non-None return.

        Failing here instead would make this runner stricter than pytest, and
        the two would disagree in the opposite direction. Verified against
        pytest 9: `test_returns_non_none` warns and passes.
        """
        mod = self._module("def test_returns_value():\n    return 42\n")
        func = dict(_collect._module_level_tests(mod))["test_returns_value"]
        case = _collect._case_for(mod, [("test_returns_value", func)])
        res = unittest.TextTestRunner(
            stream=open(os.devnull, "w"), verbosity=0).run(
                unittest.TestLoader().loadTestsFromTestCase(case))
        self.assertTrue(res.wasSuccessful())

    # ---- the third door: an async METHOD of a TestCase ------------------

    def test_an_async_method_of_a_testcase_is_refused(self):
        """The door the module-level refusal does not cover.

        `loader.discover` collects a `TestCase`; `TestCase.run` calls the
        method, gets an un-awaited coroutine back, discards it, and records a
        pass. Found by the independent review of this fix pack and reproduced
        before fixing: `run_tests.py` exited 0 with `ran:950 failures:0` for a
        method whose body raised.
        """
        probe = os.path.join(_ROOT, "tests", "test_zz_async_method_probe.py")
        if os.path.exists(probe):                     # pragma: no cover
            self.skipTest("probe file already present")
        with open(probe, "w") as fh:
            fh.write("import unittest\n\n\n"
                     "class AsyncInsideTestCase(unittest.TestCase):\n"
                     "    async def test_async_method(self):\n"
                     "        raise AssertionError('body ran')\n")
        try:
            sys.modules.pop("tests.test_zz_async_method_probe", None)
            _, diag = _collect.collect("tests")
        finally:
            os.remove(probe)
            sys.modules.pop("tests.test_zz_async_method_probe", None)
        joined = " ".join(diag["uncollectable"])
        self.assertIn("test_async_method", joined,
                      "an async TestCase method was collected as runnable; "
                      "run_tests.py would write a green report for a body "
                      "that never ran")

    def test_CONTROL_an_IsolatedAsyncioTestCase_method_is_still_accepted(self):
        """Anti-vacuity: the refusal must not blanket every coroutine method.

        `IsolatedAsyncioTestCase` supplies an event loop and genuinely runs
        coroutine methods. Refusing it would break a legitimate style and make
        the test above pass for the wrong reason.
        """
        probe = os.path.join(_ROOT, "tests", "test_zz_iso_async_probe.py")
        if os.path.exists(probe):                     # pragma: no cover
            self.skipTest("probe file already present")
        with open(probe, "w") as fh:
            fh.write("import unittest\n\n\n"
                     "class RealAsyncCase(unittest.IsolatedAsyncioTestCase):\n"
                     "    async def test_iso_runs(self):\n"
                     "        self.assertTrue(True)\n")
        try:
            sys.modules.pop("tests.test_zz_iso_async_probe", None)
            _, diag = _collect.collect("tests")
        finally:
            os.remove(probe)
            sys.modules.pop("tests.test_zz_iso_async_probe", None)
        self.assertNotIn(
            "test_iso_runs", " ".join(diag["uncollectable"]),
            "IsolatedAsyncioTestCase can run coroutine methods and must not "
            "be refused")

    def test_a_generator_method_of_a_testcase_is_refused(self):
        """Same door, generator shape."""
        probe = os.path.join(_ROOT, "tests", "test_zz_gen_method_probe.py")
        if os.path.exists(probe):                     # pragma: no cover
            self.skipTest("probe file already present")
        with open(probe, "w") as fh:
            fh.write("import unittest\n\n\n"
                     "class GenInsideTestCase(unittest.TestCase):\n"
                     "    def test_gen_method(self):\n"
                     "        yield\n")
        try:
            sys.modules.pop("tests.test_zz_gen_method_probe", None)
            _, diag = _collect.collect("tests")
        finally:
            os.remove(probe)
            sys.modules.pop("tests.test_zz_gen_method_probe", None)
        self.assertIn("test_gen_method", " ".join(diag["uncollectable"]))

    # ---- end to end, through the real build runner ---------------------

    def test_the_build_runner_refuses_and_writes_no_green_report(self):
        """ADVERSARIAL CONTROL, end to end: a failing async test in the tree.

        This is the mission's control: inject a deliberately failing
        async calibration-style test and assert the canonical runner does NOT
        report green. Runs the real `run_tests.py` in a subprocess against a
        real file in `tests/`, then removes it.
        """
        probe = os.path.join(_ROOT, "tests", "test_zz_async_control_probe.py")
        if os.path.exists(probe):                     # pragma: no cover
            self.skipTest("probe file already present")
        with open(probe, "w") as fh:
            fh.write("import _bootstrap  # noqa: F401\n\n\n"
                     "async def test_async_calibration_style_probe():\n"
                     "    raise AssertionError('probe body ran')\n")
        report = os.path.join(_ROOT, "test_report.json")
        saved = None
        if os.path.exists(report):
            with open(report, encoding="utf-8") as fh:
                saved = fh.read()
        try:
            out = subprocess.run(
                [sys.executable, "run_tests.py"], cwd=_ROOT,
                capture_output=True, text=True, timeout=900)
            self.assertNotEqual(
                out.returncode, 0,
                "the build runner reported success for a suite containing a "
                "test whose body never ran")
            self.assertIn("test_async_calibration_style_probe",
                          out.stdout + out.stderr)
            if saved is not None:
                with open(report, encoding="utf-8") as fh:
                    self.assertEqual(
                        fh.read(), saved,
                        "a refused collection must not overwrite the report "
                        "the LIVE gate reads")
        finally:
            os.remove(probe)
            if saved is not None:
                with open(report, "w", encoding="utf-8") as fh:
                    fh.write(saved)


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
