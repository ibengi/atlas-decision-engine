"""Test-suite environment defaults.

`ALLOW_ORDER_SUBMISSION` used to default to TRUE whenever the variable was
absent, so this suite quietly depended on a permissive default: hundreds of
order-path tests only submitted because nobody had set the variable at all.

That default was the B1 defect. The gate is now strictly fail-closed — absent,
blank or unreadable all mean "blocked" — which is what production needs, and
it means the suite has to say out loud that its order-path tests want
submissions enabled. Saying it here, once, is the honest version of what was
previously an accident.

Production is unaffected either way: the deployed service sets the variable
explicitly, and the tests that assert a CLOSED gate set `CFG.ALLOW_ORDER_SUBMISSION`
on the object, which overrides anything read from the environment.
"""

import os

# Same defaults as `_bootstrap.py`, which 40 of the 51 test modules import as
# their first statement so that `config` is built with dummy DEMO credentials.
# The 11 modules that skip that convention leave CFG.DEMO_KEY_ID empty if they
# happen to be collected first, and every later module that builds a real
# KalshiClient then fails. Setting them here, where pytest guarantees we run
# before any test module, makes collection order stop mattering. Dummy values,
# never real secrets.
os.environ.setdefault("PROBE_PROVIDERS_ON_START", "0")
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")

# Must run before any test module imports `config`, whose class attributes are
# evaluated once at import time. pytest imports conftest first, so it does.
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
# asserts that the SHIPPED default with a clean environment is False. That is
# the property production depends on; this line only stops unrelated modules
# from tripping over a ticker prefix.
os.environ.setdefault("DAILY_RESEARCH_ORACLE_APPROVED", "true")
