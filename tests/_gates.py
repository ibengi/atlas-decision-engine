"""Test-suite defaults for the two POLICY GATES — loaded by BOTH runners.

WHY THIS FILE EXISTS
    `ALLOW_ORDER_SUBMISSION` and `DAILY_RESEARCH_ORACLE_APPROVED` are strictly
    fail-closed in production: absent, blank or unreadable all mean "blocked".
    That is what production needs, and it means the suite has to say out loud
    that its order-path tests want submissions enabled — hundreds of them only
    ever submitted because nobody had set the variable at all.

    Saying it once, here, is the honest version of what used to be an accident.

WHY NOT tests/conftest.py
    `conftest.py` is a PYTEST mechanism. The repository's primary automated
    runner is not pytest: `run_tests.py` uses `unittest.TestLoader().discover`
    and writes `test_report.json`, which the Dockerfile's test stage runs and
    `model_gatekeeper.check_live_allowed()` later reads. unittest never loads a
    conftest, so gate defaults living only there made the suite pass under
    pytest and fail under the runner that actually gates the build.

    This module is therefore imported by BOTH entry points — `tests/conftest.py`
    for pytest, and `run_tests.py` before it discovers anything — so the two
    runners see identical test assumptions.

WHAT THIS FILE IS NOT
    It changes NO production default. `config._env_gate` still defaults both
    gates to False, and `tests/test_daily_quarantine.py` proves that
    behaviourally in subprocesses with these variables scrubbed. Nothing here
    executes in the runtime image: the Dockerfile's `runtime` stage never runs
    `run_tests.py`, and no production module imports this package.

    `setdefault` throughout: an operator or CI job that sets either variable
    explicitly keeps their value, so a deliberately CLOSED run stays closed.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Dummy DEMO credentials + provider probe off. 40 of the 51 test modules import
# this same module as their first statement; the 11 that skip that convention
# used to leave CFG.DEMO_KEY_ID empty when collected first, and every later
# module that built a real KalshiClient then failed. Loading it from the two
# RUNNERS makes collection order stop mattering. Dummy values, never secrets.
import _bootstrap  # noqa: F401,E402

# Must run before any test module imports `config`, whose class attributes are
# evaluated once at import time. Both entry points import this first, so it does.
os.environ.setdefault("ALLOW_ORDER_SUBMISSION", "true")

# 29 test modules use a KXBTCD ticker as their GENERIC example when exercising
# order plumbing — dedup locks, 503 cooldowns, contract caps, envelope schemas.
# None of them are testing daily settlement policy; KXBTCD is simply the ticker
# the suite grew up with. The B2 quarantine refuses that prefix at the money
# path, so without this the suite would report ~130 failures that say nothing
# about the code under test.
#
# Enabling it HERE and nowhere else keeps the policy assertions in one file:
# tests/test_daily_quarantine.py patches CFG explicitly for both branches, and
# asserts that the SHIPPED default with a clean environment is False.
os.environ.setdefault("DAILY_RESEARCH_ORACLE_APPROVED", "true")

#: The variables this module owns, named once so the tests that prove the
#: shipped defaults can scrub exactly these and nothing else.
GATE_VARS = ("ALLOW_ORDER_SUBMISSION", "DAILY_RESEARCH_ORACLE_APPROVED")
