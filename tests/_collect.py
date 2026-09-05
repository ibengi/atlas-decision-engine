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

WHY IT ALSO REFUSES `async def` AND GENERATOR TESTS  (RC-1 M1)
    A wrapped `async def test_*` was ACCEPTED and COUNTED, but calling it only
    builds a coroutine that is never awaited: the body never runs and the test
    "passes". Reproduced on e377df6 with a deliberately failing async test —
    pytest reported `1 failed`, `run_tests.py` reported `ran:884 failures:0`
    and exited 0. That is a false-green `test_report.json`, i.e. the exact
    failure mode this file exists to close, reopened through another door.

    THREE doors, not two: a bare `async def test_*` at module level, the same
    thing written as a METHOD of a `unittest.TestCase` (which `discover`
    collects and `TestCase.run` calls without awaiting), and a decorated
    function whose coroutine-ness is invisible to `inspect`. All three are
    closed below.

    Two layers close the first and third, because one cannot:

    * STATICALLY, `collect()` refuses coroutine, generator and async-generator
      functions into `uncollectable`. The build then refuses to write a report
      at all rather than write a green one.
    * AT RUNTIME, the wrapper in `_case_for` fails a test whose call returns an
      un-awaited coroutine/generator. Static inspection cannot see this case: a
      `functools.wraps` decorator around an `async def` reports
      `iscoroutinefunction() is False` while still returning a coroutine.
      Verified that real pytest FAILS that same case, so failing it here is
      parity, not divergence.

    A test that merely returns a non-None value is deliberately NOT failed:
    real pytest only warns there (`PytestReturnNotNoneWarning`) and passes, and
    this collector's job is to agree with pytest, not to be stricter than it.

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


#: Results that prove the test body never actually ran to completion. A
#: coroutine or generator handed back by a test function is an UNEXECUTED body,
#: not a value: awaiting/iterating is what would have run the assertions.
def _unexecuted_body(result):
    """Describe `result` if it proves the test body did not run, else None."""
    if inspect.iscoroutine(result):
        return "returned a coroutine that was never awaited"
    if inspect.isasyncgen(result):
        return "returned an async generator that was never iterated"
    if inspect.isgenerator(result):
        return "returned a generator that was never iterated"
    return None


#: Function kinds this runner CANNOT execute, whatever their signature. Each
#: needs machinery unittest does not have (an event loop, or pytest's generator
#: handling); wrapping one and calling it would count a test whose body never
#: ran. `isasyncgenfunction` is listed separately because
#: `iscoroutinefunction()` is False for an `async def` that yields.
_UNSUPPORTED_KINDS = (
    (inspect.iscoroutinefunction, "async def; needs an event loop, pytest-only"),
    (inspect.isasyncgenfunction, "async generator; pytest-only"),
    (inspect.isgeneratorfunction, "generator (yield); pytest-only"),
)


def _unsupported_kind(func):
    """Reason this runner cannot execute `func`, or None if it can."""
    for predicate, reason in _UNSUPPORTED_KINDS:
        if predicate(func):
            return reason
    return None


def _case_for(module, funcs):
    """Wrap a module's bare test functions into one real TestCase."""
    attrs = {}
    for name, func in funcs:
        def method(self, _f=func):
            result = _f()
            unexecuted = _unexecuted_body(result)
            if unexecuted is not None:
                # Closing the result explicitly keeps the failure clean instead
                # of trailing a "coroutine was never awaited" RuntimeWarning
                # from the garbage collector at some unrelated later point.
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise AssertionError(
                    f"{_f.__module__}.{_f.__name__} {unexecuted}; its body "
                    f"never ran, so a pass here would be false. This runner "
                    f"executes tests synchronously and does not provide an "
                    f"event loop or fixtures.")
        method.__name__ = name
        method.__doc__ = func.__doc__
        attrs[name] = method
    cls = type("ModuleLevelTests", (unittest.TestCase,), attrs)
    cls.__module__ = module.__name__
    cls.__qualname__ = "ModuleLevelTests"
    return cls


def _iter_tests(suite):
    """Every leaf TestCase in a (possibly nested) suite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _unsupported_methods(suite):
    """Ids of discovered TEST METHODS this runner cannot execute.

    THE THIRD DOOR. The module-level refusal above covers bare `test_*`
    functions. It does not see an `async def test_*` written as a METHOD of a
    `unittest.TestCase`: `loader.discover` collects it, `TestCase.run` calls
    it, the call returns an un-awaited coroutine, the return value is
    discarded, and the test "passes". Reproduced at 0e44b40 --
    `run_tests.py` exited 0 with `ran:950 failures:0` for a method whose body
    raised. Same false-green artifact, same LIVE gate reading it, reached
    through a door the first fix did not cover.

    `IsolatedAsyncioTestCase` is exempt: it provides an event loop and really
    does run coroutine methods, so refusing it would be wrong.
    """
    refused = []
    for test in _iter_tests(suite):
        if isinstance(test, unittest.IsolatedAsyncioTestCase):
            continue
        name = getattr(test, "_testMethodName", "")
        method = getattr(type(test), name, None)
        if method is None:
            continue
        reason = _unsupported_kind(method)
        if reason is not None:
            refused.append(f"{test.id()} ({reason})")
    return refused


def collect(start_dir="tests"):
    """Return (suite, diagnostics).

    `diagnostics["uncollectable"]` lists tests this collector cannot run --
    module-level functions AND TestCase methods. It must stay empty; the
    parity test enforces that, and `run_tests.py` refuses to write a report.
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
            unsupported = _unsupported_kind(func)
            if unsupported is not None:
                uncollectable.append(f"{modname}.{name} ({unsupported})")
                continue
            if inspect.signature(func).parameters:
                uncollectable.append(f"{modname}.{name} (declares parameters)")
                continue
            runnable.append((name, func))
        if runnable:
            suite.addTests(loader.loadTestsFromTestCase(_case_for(module, runnable)))
            added += len(runnable)

    uncollectable.extend(_unsupported_methods(suite))

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
