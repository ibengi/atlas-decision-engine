"""Canonical test collection — used by BOTH `run_tests.py` and the parity test.

WHY THIS FILE EXISTS
    `unittest` collects only `TestCase` subclasses. `pytest` also collects
    module-level functions named `test_*`. The suite contains both styles, so
    the two runners disagreed on how many tests exist: 794 under unittest, 799
    under pytest.

    That mattered far beyond bookkeeping. `run_tests.py` is what the Docker
    build runs, and the `test_report.json` it writes is the artifact
    `model_gatekeeper.check_live_allowed()` reads before permitting LIVE. The
    five tests the build could not see were `tests/test_calibration.py`'s —
    Brier score, ECE, and the calibration curve — i.e. exactly the statistics
    the promotion rule turns on. The evidence the last lock consults omitted
    the tests guarding the number that decides promotion.

WHAT IT DOES
    Adds the missing collection rule to unittest rather than hard-coding a
    count: every zero-argument module-level `test_*` function in a discovered
    test module is wrapped into a real `TestCase` and appended to the suite.
    A future test written in that style is picked up automatically; nothing
    needs updating when the total changes.

WHAT IT DELIBERATELY DOES NOT DO
    It does not invent a fixture system. A module-level test function that
    declares parameters is something only pytest can run, so this collector
    refuses it and REPORTS it in `uncollectable`. `test_runner_parity.py`
    fails on a non-empty `uncollectable`, so the suite cannot drift back into
    a silent split between the two runners: the divergence becomes a red test
    instead of a quiet five-test hole in the LIVE gate's evidence.

WHAT THIS FILE IS NOT
    It is not production code. No production module imports `tests`, and the
    Dockerfile's `runtime` stage never executes `run_tests.py`.
"""


import inspect
import importlib
import pathlib
import unittest

#: Test-module filename pattern, matching `unittest discover`'s default.
TEST_GLOB = "test_*.py"


def _module_names(start_dir):
    """Import names of the test modules, in the order discovery walks them."""
    root = pathlib.Path(start_dir)
    package = root.name
    for path in sorted(root.glob(TEST_GLOB)):
        yield f"{package}.{path.stem}"


def _module_level_tests(module):
    """(name, function) for every module-level `test_*` defined HERE.

    `inspect.isfunction` plus the `__module__` check keeps an imported helper
    from being counted twice when two modules share one `test_*` symbol —
    pytest attributes a function to the file that defines it, and so do we.
    """
    for name in sorted(vars(module)):
        if not name.startswith("test_"):
            continue
        obj = vars(module)[name]
        if not inspect.isfunction(obj):
            continue
        if obj.__module__ != module.__name__:
            continue
        yield name, obj


def _case_for(module, funcs):
    """Wrap a module's bare test functions into one real TestCase."""
    attrs = {}
    for name, func in funcs:
        def method(self, _f=func):
            _f()
        method.__name__ = name
        method.__doc__ = func.__doc__
        attrs[name] = method
    cls = type("ModuleLevelTests", (unittest.TestCase,), attrs)
    cls.__module__ = module.__name__
    cls.__qualname__ = "ModuleLevelTests"
    return cls


def collect(start_dir="tests"):
    """Return (suite, diagnostics).

    `diagnostics["uncollectable"]` lists module-level test functions this
    collector cannot run. It must stay empty; the parity test enforces that.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir)
    discovered = suite.countTestCases()

    uncollectable, added = [], 0
    for modname in _module_names(start_dir):
        try:
            module = importlib.import_module(modname)
        except Exception:
            # unittest discovery already turned this into a _FailedTest, which
            # fails the run loudly. Re-raising here would only mask it.
            continue
        runnable = []
        for name, func in _module_level_tests(module):
            if inspect.signature(func).parameters:
                uncollectable.append(f"{modname}.{name} (declares parameters)")
                continue
            runnable.append((name, func))
        if runnable:
            suite.addTests(loader.loadTestsFromTestCase(_case_for(module, runnable)))
            added += len(runnable)

    return suite, {
        "discovered_by_unittest": discovered,
        "module_level_added": added,
        "total": discovered + added,
        "uncollectable": uncollectable,
    }


#: DELIBERATELY ABSENT: a second, hand-rolled estimate of pytest's collection.
#: Re-implementing pytest's rules here would give the parity test a reference
#: that shares this file's blind spots — the two would agree precisely where
#: both are wrong, and report COUNTS_MATCH for the wrong reason. The parity
#: test therefore asks the REAL pytest (installed in the Docker test stage via
#: requirements-dev.txt) and skips where it is genuinely unavailable, rather
#: than comparing this module against a mirror of itself.
