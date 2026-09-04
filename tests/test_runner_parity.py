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


if __name__ == "__main__":                              # pragma: no cover
    unittest.main()
