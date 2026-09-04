# Independent delta security review — `e0e2f83..5d6e994`

**Repository** `ibengi/atlas-decision-engine`
**Branch reviewed** `claude/railway-atlas-inspection-1gtkyf`
**Head reviewed** `5d6e994a20bbdfbab471d2de250c831b210bdf82`
**Baseline (already independently reviewed)** `e0e2f83`
**Reviewer** independent session with no authorship history on any commit under review
**Date** 2026-09-04

Nothing in a commit message, code comment, docstring or `docs/` file was accepted
as evidence. `docs/order-gating-verification.md` is the author's own account and
was read only to check its claims against measurements made here. Every number
below was produced in this session. Environment: Python 3.11.15 (system) and
Python 3.13.12 (matching the Dockerfile pin) in clean venvs built from the
repository's own `requirements.txt` + `requirements-dev.txt`; pytest 9.1.1;
`pytest-randomly` confirmed **not** installed, so no run below is described as
randomised — module order was changed by shuffling the file arguments, which
genuinely changes pytest's collection order.

No production code was modified. Every mutation was applied to a backed-up file
and reverted; `git status` is clean and `git diff 5d6e994` is empty (0 bytes),
verified per-file by blob SHA. No order was placed, no gate weakened, no Railway
variable touched, no deployment made, PR-D untouched.

---

## 1. Delta scope

`git diff --name-status e0e2f83..5d6e994`:

| Status | File | Lines |
| --- | --- | --- |
| M | `docs/order-gating-verification.md` | +71 |
| M | `run_tests.py` | +11 |
| A | `tests/_gates.py` | +67 |
| M | `tests/conftest.py` | +14 / −41 |
| M | `tests/test_daily_quarantine.py` | +102 |
| M | `tools/restart_harness.py` | +26 |

Total 6 files, +286 / −46.

The delta is limited to exactly the four categories claimed: the shared test gate
bootstrap (`tests/_gates.py`, `tests/conftest.py`), the runner integration
(`run_tests.py`), the restart-harness anti-vacuity restoration
(`tools/restart_harness.py`), and the tests and doc covering those changes.

Confirmed unchanged (empty diff over the delta range):

* `config.py` — production defaults and both gate parsers
* `execution_engine.py` — daily quarantine
* `order_manager.py` — daily quarantine and the secondary money-path guard
* `risk_manager.py`, `position_sizer.py`, `ml_model.py`, `btc_probability_model.py`,
  `btc_strategy.py`, `btc_context.py`, `backtest_btc15m.py`, `strategy_router.py`,
  `market_scanner.py`, `opportunity_pipeline.py`, `model_gatekeeper.py`,
  `kalshi_alpha_bot.py`
* `Dockerfile`, `Procfile`, `.github/`, `requirements.txt`,
  `requirements-dev.txt`, `pytest.ini`, `model_validation.json`

A negative diff over the whole tree excluding `tests/`, `docs/`, `tools/` and
`run_tests.py` returns empty: **no production module is touched at all.**

Note on classification: `run_tests.py` is build-time infrastructure (it is what
the Dockerfile's `tests` stage executes) and `tools/restart_harness.py` is a
manually-run developer tool. Neither is on the runtime import path — verified
dynamically in §3.

**DELTA_SCOPE_CLEAN=YES**
**UNRELATED_CHANGES=NONE**

---

## 2. Real build runner

### 2.1 `python run_tests.py`, both gate variables absent

Invoked as `env -u ALLOW_ORDER_SUBMISSION -u DAILY_RESEARCH_ORACLE_APPROVED python run_tests.py`
(both variables confirmed absent from the ambient environment first).

| Run | Exit | ran | failures | errors | `test_report.json` |
| --- | --- | --- | --- | --- | --- |
| 1 | **0** | 794 | **0** | **0** | generated, green |
| 2 | **0** | 794 | **0** | **0** | generated, green |

### 2.2 The baseline defect, reproduced independently

To confirm the blocker being fixed was real rather than asserted, `e0e2f83` was
checked out into a throwaway git worktree and run the same way:

| At `e0e2f83`, gates absent | Result |
| --- | --- |
| `python run_tests.py` | **exit 1**, ran 790, **failures 88, errors 47** |
| `pytest -q` | exit 0, 795 passed + 142 subtests |
| `tools/restart_harness.py` | **exit 1, 15/16**, `restart cannot duplicate a submitted order` failing with `status=blocked:submission_disabled` |

This reproduces the earlier review's finding exactly (88/47, and 15/16). The
blocker was genuine and `5d6e994` clears it. The worktree was removed afterwards.

### 2.3 Bootstrap ordering — proven, not assumed

*Statically.* `run_tests.py` imports only stdlib (`json`, `sys`, `time`,
`unittest`) on lines 4–7, then `from tests import _gates` at **module level, line
19**. `loader.discover("tests")` is on **line 24, inside `main()`**, which is
called on line 47 under `if __name__ == "__main__"`. A module-level import
necessarily completes before any function body runs, so no test module can be
imported before the gate defaults are applied.

*Dynamically.* In a scrubbed subprocess, importing `run_tests` alone:

```
before: None None
config imported before? False
after : true true
config in sys.modules after importing run_tests? False
```

Both gate variables are set by the import, and `config` is **not** yet in
`sys.modules` — so `config`'s class attributes (which freeze at import) are still
unevaluated when discovery begins. That is the precise property the fix needs.

### 2.4 Runner mutations

Applied one at a time to a backed-up `run_tests.py`, full suite run each time,
file restored and verified identical afterwards.

| Mutation | Exit | failures | errors | Killed? |
| --- | --- | --- | --- | --- |
| M-A: `from tests import _gates` moved **after** `loader.discover` | **1** | 89 | 47 | **YES** |
| M-B: `from tests import _gates` **removed** | **1** | 89 | 47 | **YES** |

Both mutations additionally failed the two dedicated tests by name:
`BothRunnersShareTestGates.test_importing_run_tests_applies_the_shared_gate_defaults`
and `...test_the_gate_defaults_are_loaded_before_discovery`.

(89 rather than the baseline's 88 because the delta adds four tests, one of which
also fails when the bootstrap is absent. The counts are consistent.)

**RUN_TESTS_PASS=YES** (exit 0, 794 ran, 0 failures, 0 errors, twice, gates absent)
**BOOTSTRAP_ORDER_CORRECT=YES** (proven statically and dynamically)
**RUNNER_MUTATIONS_CAUGHT=YES** (2/2)

---

## 3. Production default isolation

### 3.1 Clean subprocess importing production `config` only

Each row is a separate interpreter, `cwd` = repo root, `PYTHONPATH` emptied,
importing `config` and nothing from `tests`. `test_modules_imported` was `[]` in
**every** row — the production config path pulls in no test module.

| `ALLOW_ORDER_SUBMISSION` / `DAILY_RESEARCH_ORACLE_APPROVED` | `CFG.ALLOW_ORDER_SUBMISSION` | `CFG.DAILY_..._APPROVED` | `daily_oracle_approved()` | warned |
| --- | --- | --- | --- | --- |
| absent | **False** | **False** | **False** | — |
| `""` (empty) | **False** | **False** | **False** | both |
| `"   "` (blank) | **False** | **False** | **False** | both |
| `"fasle"` | **False** | **False** | **False** | both |
| `"trueish"` | **False** | **False** | **False** | both |
| `"false"` | **False** | **False** | **False** | — |
| `"true"` | True | True | True | — |

Absent, empty and malformed all read **FALSE**, and malformed values are recorded
in `GATE_PARSE_WARNINGS` so a gate closed by a typo is visible rather than silent.
The daily approval flag and its single resolver `daily_oracle_approved()` behave
identically. This is the shipped `_env_gate` behaviour, unchanged by the delta.

### 3.2 No production module reaches the test plumbing

Repository-wide grep for `import _bootstrap`, `from tests`, `import tests`,
`tests._gates`, `tests.conftest`, `import _gates`, excluding `tests/` itself,
returns exactly two hits:

* `run_tests.py:19` — the build-time runner (intended)
* `tools/restart_harness.py:24` — `import _bootstrap` only (dummy DEMO
  credentials via `setdefault`); it does **not** import `tests._gates`

Dynamically confirmed: importing the runtime entrypoint `kalshi_alpha_bot` in a
scrubbed subprocess pulls in **no** `tests` module, does not import `_bootstrap`,
and leaves both gates `False`.

### 3.3 Docker runtime stage

The `runtime` stage's `CMD` is `["python", "kalshi_alpha_bot.py", "--loop", "--demo"]`
— identical to the `Procfile`. `run_tests.py` is executed **only** in the `tests`
stage (`RUN python run_tests.py`, line 47); the runtime stage never runs it and
imports nothing that reaches `tests/_gates.py`. Normal startup therefore cannot
execute the test bootstrap.

One caveat, recorded as a LOW finding (§9, F3): the runtime stage does `COPY . .`,
so `run_tests.py`, `tests/` and `_bootstrap.py` **are physically present** in the
deployed image even though nothing on the startup path imports them. This is a
defence-in-depth gap, not an active path, and it predates this delta.

**PRODUCTION_DEFAULTS_UNCHANGED=YES**
**TEST_BOOTSTRAP_ISOLATED_FROM_RUNTIME=YES** (no execution path; see F3 for the file-presence caveat)
**CLEAN_ENV_FAIL_CLOSED=YES**

---

## 4. `setdefault` semantics

`tests/_gates.py` uses `os.environ.setdefault(...)` for both gates and contains no
`os.environ[...]` assignment, no `import config`, and no write to `CFG` in its
executable source (docstring and comments mention them by name only).

Behavioural matrix — each row is a subprocess that sets the variable explicitly,
imports `run_tests` (which imports the bootstrap), then imports `config`:

| Explicitly set to | Value after import | `CFG.ALLOW_ORDER_SUBMISSION` |
| --- | --- | --- |
| `false` | `'false'` (preserved) | **False** |
| `FALSE` | `'FALSE'` (preserved) | **False** |
| `0` | `'0'` (preserved) | **False** |
| `off` | `'off'` (preserved) | **False** |
| `no` | `'no'` (preserved) | **False** |
| `fasle` (malformed) | `'fasle'` (preserved) | **False** |
| `""` (empty) | `''` (preserved) | **False** |
| `true` | `'true'` (preserved) | True |
| *(unset)* | `'true'` (default applied) | True |

An explicit `false` survives. So does an explicit **empty string** — `setdefault`
does not overwrite a key that exists with an empty value — and so does a malformed
value, which then still reads FALSE through the strict parser. A deliberately
closed run stays closed.

Parser tests are not masked. `CleanEnvironmentDefaults` and `ImportOrderIsIrrelevant`
make every default/blank/malformed assertion in a **separate interpreter** with
exactly `GATE_VARS` scrubbed, and `GATE_VARS` is now imported from the module that
sets them, so the scrub list cannot drift from the thing it scrubs. There is also a
guard-the-guard test (`test_the_probe_sees_a_scrubbed_environment`) asserting that
the parent process *does* have both gates set — which fails if the bootstrap ever
stops running, closing the loop.

**SETDEFAULT_SAFE=YES**
**EXPLICIT_FALSE_PRESERVED=YES**
**PARSER_TESTS_NOT_MASKED=YES**

---

## 5. pytest / unittest equivalence

All runs below with both gate variables scrubbed.

| Run | Result |
| --- | --- |
| `pytest -q` run 1 | exit 0 — **799 passed + 142 subtests**, 44.5s |
| `pytest -q` run 2 | exit 0 — **799 passed + 142 subtests**, 44.8s |
| `python run_tests.py` runs 1 & 2 | exit 0 — **794 ran, 0 failures, 0 errors** |
| pytest, shuffled file order seed 11 | exit 0 — 799 passed |
| pytest, shuffled file order seed 27 | exit 0 — 799 passed |
| pytest, shuffled file order seed 43 | exit 0 — 799 passed |
| adversarial unittest, **reversed** module order, run_tests bootstrap | exit 0 — **794 ran, 0 failures, 0 errors** |

`pytest-randomly` is not installed (verified), so the three shuffles were produced
by permuting the `tests/test_*.py` arguments with fixed seeds; that genuinely
changes collection order. The adversarial run replicates `run_tests.py`'s
bootstrap and then reverses the discovered module list.

### Collected-test set: a real divergence (pre-existing)

Because pytest reports 799 and the build runner reports 794, both collections were
enumerated and diffed. The gap is exactly five tests, all in one file:

```
pytest-only (5):
    test_calibration.test_analyze_is_json_friendly_and_aliases_work
    test_calibration.test_brier_and_ece_known_values
    test_calibration.test_calibration_curve_has_empty_bins_and_includes_one
    test_calibration.test_collect_pairs_accepts_shadow_records_and_skips_unresolved
    test_calibration.test_invalid_bin_count_rejected
unittest-only (0):
```

These are module-level `def test_*()` functions, which `unittest.TestLoader.discover`
does not collect. `run_tests.py` — the runner whose `test_report.json`
`model_gatekeeper.check_live_allowed()` reads — therefore never executes them.

This is **not** a regression from the delta: `tests/test_calibration.py` is
untouched by `e0e2f83..5d6e994`, and the functions were introduced in `b0d5327`
(PR #34), long before this branch. Reported as MEDIUM finding F1 below.

The delta's own claim is narrower and is met: the two runners now see **identical
assumptions**. Their **test sets** were never identical and still are not.

**PYTEST_PASS=YES** (799 + 142 subtests, twice)
**UNITTEST_PASS=YES** (794 ran, 0/0, twice)
**RUNNER_BEHAVIOR_EQUIVALENT=YES for assumptions; NO for collected test sets** — see F1
**IMPORT_ORDER_SAFE=YES** (3 shuffles + 1 adversarial reversal, all green)

---

## 6. Restart harness

### 6.1 Safety audit of `bot.CFG.ALLOW_ORDER_SUBMISSION = True`

| Property | Verdict | Evidence |
| --- | --- | --- |
| Local to this process's config object | **YES** | Attribute set on the `CFG` class object at `tools/restart_harness.py:121`. Class-attribute mutation is confined to the interpreter that performs it. |
| Not an environment mutation | **YES** | The file contains no `os.environ` write, no `putenv`, no `setdefault` for this variable. Grep for `os.environ\|putenv\|subprocess\|Popen` in the harness returns only docstring/comment matches. |
| Not persisted | **YES** | Nothing writes the flag to disk; `DATA_DIR` is a `tempfile.mkdtemp()` that is `rmtree`d at the end. |
| Cannot reach production runtime | **YES** | The harness spawns no subprocess (nothing to inherit an env that is not set anyway), is imported by no production module, and is executed by no build or CI step. |
| MagicMock broker only | **YES** | `fresh_client()` returns a `MagicMock`; no `KalshiClient` and no network is constructed. |
| Synthetic NON-DAILY ticker | **YES** | `TICKER = "KXTEST-CANARY-T1"`. `is_daily_ticker()` is False for it, and the suite pins this in `SecondaryMoneyPathGuard.test_restart_harness_ticker_is_not_a_daily_ticker`. |
| Daily approval untouched | **YES** | `DAILY_RESEARCH_ORACLE_APPROVED` appears nowhere in the harness except a comment. Verified in-process: `CFG.ALLOW_ORDER_SUBMISSION` reads **False** as imported (shipped default) before the opt-in line runs. |
| Quarantine still in force inside the harness | **YES** | Daily approval stays False, so `daily_quarantine_blocks()` would refuse any KXBTCD ticker in this same process. |

### 6.2 Harness result at head

`env -u ALLOW_ORDER_SUBMISSION -u DAILY_RESEARCH_ORACLE_APPROVED python tools/restart_harness.py`
→ **exit 0, TOTAL 17/17 checks passed**, including:

```
PASS [PRE] seed order really reached the broker (dedup guard armed)
     -- create_order_calls=1 status=executed guard_tickers=['KXTEST-CANARY-T1']
PASS [VOLUME] restart cannot duplicate a submitted order
     -- resubmit -> status=blocked:duplicate_submission_guard create_order_calls=0
```

`git status` clean afterwards; the harness leaves no repository artefact.

### 6.3 Duplicate guard — independently exercised, not taken on trust

A probe was written from scratch (not reusing the harness or any suite helper)
that rebuilds the scenario and **asserts** rather than prints. Result:

```
env ALLOW_ORDER_SUBMISSION = None
CFG.ALLOW_ORDER_SUBMISSION as imported = False      <- shipped gate is closed
FACT 1 seed order reached the mocked broker: create_order_calls=1, order_id=ord-1, status=executed
FACT 2 guard armed in memory: session_submitted=['KXTEST-CANARY-T1']
FACT 2b guard persisted to disk: True {'KXTEST-CANARY-T1': 1788528739.7283707}
FACT 3 duplicate rejected: status=blocked:duplicate_submission_guard
FACT 4 create_order NOT called on the duplicate: call_count=0
CONTROL with guard entry cleared: status=executed create_order_calls=1
CONTROL OK -- the block in FACT 3 is attributable to the duplicate guard alone
PROBE_RESULT=ALL_FOUR_FACTS_PROVEN
```

All four required facts hold, plus an **attribution control** the harness itself
does not perform: removing only the `session_submitted` entry and repeating the
identical call lets it through to the broker (`create_order_calls` 0 → 1). That
rules out the possibility that FACT 3's rejection came from some other gate, which
is the failure mode that would make the whole check meaningless.

### 6.4 Harness mutations

| Mutation | Result | Killed? |
| --- | --- | --- |
| M-C: remove `bot.CFG.ALLOW_ORDER_SUBMISSION = True` | **exit 1, 15/17.** `[PRE] seed order really reached the broker` FAILS (`create_order_calls=0 status=blocked:submission_disabled guard_tickers=[]`); `[VOLUME] restart cannot duplicate a submitted order` also FAILS | **YES** |
| M-D1: remove the `[PRE]` check, keep the opt-in | exit 0, 16/16 (nothing to detect — the run is not vacuous) | n/a |
| M-D2: remove the `[PRE]` check **and** the opt-in (the vacuity case) | **exit 1, 15/16.** `[VOLUME] restart cannot duplicate a submitted order` FAILS with `status=blocked:submission_disabled` | **YES** |

M-D2 answers the question the brief asks: with the `[PRE]` check gone, vacuity is
**still** detected — by a second, independent mechanism. `restart cannot duplicate
a submitted order` asserts the *exact* status string
`blocked:duplicate_submission_guard`, so a vacuous run (which yields
`blocked:submission_disabled`) fails it. The `[PRE]` check is therefore a
diagnostic amplifier that names the root cause immediately, not the sole defence.
Anti-vacuity does not depend on a single assertion.

M-C reproduces the author's stated 15/17 exactly, with the two named checks failing.

**RESTART_HARNESS_SAFE=YES**
**ANTI_VACUITY_EFFECTIVE=YES** (two independent detectors; verified by M-C and M-D2)
**DUPLICATE_GUARD_ACTUALLY_EXERCISED=YES** (four facts proven independently + attribution control)

---

## 7. B1/B2 regression check

The delta touches **no production code** (§1, verified by an exclusion diff that
returns empty), so B1/B2 cannot have been altered by it. Confirmed behaviourally
anyway, twice over.

*Targeted suite.* `tests/test_daily_quarantine.py` (54 tests) with gates scrubbed:
**54 passed** under pytest, **54 ran / 0 failures / 0 errors** under unittest
discovery with the `run_tests.py` bootstrap.

*Independent reproduction* (probe written here, not reusing suite helpers), fresh
`OrderManager` and fresh MagicMock broker per cell, ticker `KXBTCD-26SEP04-B100000`:

| `ALLOW_ORDER_SUBMISSION` | `DAILY_..._APPROVED` | status | `create_order` calls |
| --- | --- | --- | --- |
| False | False | `blocked:submission_disabled` | **0** |
| False | True | `blocked:submission_disabled` | **0** |
| True | False | `blocked:daily_oracle_unapproved` | **0** |
| True | True | `executed` | 1 |

The four-cell matrix holds: the two guards are independent — the global gate blocks
regardless of daily approval, and the daily guard blocks when the global gate is
open. Only both-open reaches the broker.

*B1 fail-closed parser* — §3.1: absent / empty / blank / malformed all FALSE, with
malformed recorded in `GATE_PARSE_WARNINGS`.

*B2 secondary money-path guard* — `order_manager.place_and_track` enforces, in
order: `ALLOW_ORDER_SUBMISSION` → `ticker_is_wellformed` → `daily_quarantine_blocks`,
each returning before any broker call. Keyed on the **ticker**, not `market_type`,
so it holds for a hand-built Decision or a direct tool call.

*Ticker canonicalisation* (independently probed):

| Input | canonical | daily | wellformed | quarantined |
| --- | --- | --- | --- | --- |
| `'KXBTCD-A'` | `'KXBTCD-A'` | True | True | **True** |
| `' kxbtcd-a '` | `'KXBTCD-A'` | True | True | **True** |
| `'KxBtCd-A'` | `'KXBTCD-A'` | True | True | **True** |
| `'\tKXBTCD-A\n'` | `'KXBTCD-A'` | True | True | **True** |
| `b'KXBTCD-A'` | `'KXBTCD-A'` | True | False | **True** |
| `'KX BTCD-A'` | `'KX BTCD-A'` | False | **False** | — (blocked as malformed) |
| `None` / `''` / `12` | `''` / `''` / `'12'` | False | **False** | — (blocked as malformed) |

Border whitespace and case fold; **inner** whitespace never does, so
`'KX BTCD-A'` never becomes a valid ticker. Unclassifiable values are refused by
the wellformed gate, which runs *before* the daily gate — fail-closed in the right
order. Both guards call the single shared classifier, so they cannot diverge.

*15m unchanged.* No 15m production file appears in the delta
(`btc_strategy.py`, `btc_probability_model.py`, `backtest_btc15m.py`,
`btc_context.py`, `ml_model.py` — all empty diffs). Behaviourally, a non-daily
15m ticker with daily approval OFF and the global gate ON reaches the broker
normally (`status=executed`, `create_order_calls=1`) — the daily quarantine does
not leak onto the 15m path. Suite coverage
(`test_15m_is_unaffected_and_proceeds_past_both_gates`,
`test_15m_unaffected_by_daily_approval_state`,
`test_fifteen_minute_path_is_unchanged_by_the_daily_state`) all pass.

**B1_STILL_PASS=YES**
**B2_STILL_PASS=YES**
**15M_STILL_UNCHANGED=YES**

---

## 8. Build reproducibility

**The real Docker build could not be run, and no pass is claimed for it.**

Docker CLI 29.3.1 is installed but no daemon was running. A daemon was started
successfully (`sudo dockerd`, then restarted with the session's `HTTPS_PROXY` and
CA configuration). Both attempts at `docker build --target tests .` failed at the
**first instruction**, `FROM python:3.13-slim`:

```
ERROR: failed to solve: python:3.13-slim: failed to resolve source metadata for
docker.io/library/python:3.13-slim: failed to copy: httpReadSeeker: failed open:
... GET https://production.cloudfront.docker.com/registry-v2/... : 403 Forbidden
```

`production.cloudfront.docker.com` is denied by this session's egress policy, both
directly and through the agent proxy. Per the proxy's own guidance a policy 403 is
reported, not retried or routed around. No image layer was ever fetched, no image
was built, nothing was pushed, nothing was deployed.

### What was run instead

The closest faithful equivalent of the `tests` stage, executed locally with the
**same pinned interpreter version** the Dockerfile specifies:

1. A clean build context was materialised from the working tree applying
   `.dockerignore` verbatim (`.git`, `.github`, `.venv`, `venv`, `__pycache__`,
   `*.pyc`, `.pytest_cache`, `.env`, `*.log`, `raw_samples/`, `backups/`,
   `data/`, `test_report.json`) — 170 files, with `.git` and `test_report.json`
   confirmed absent, mirroring `COPY . .`.
2. A fresh venv on **Python 3.13.12** (the Dockerfile pins `3.13-slim`), then
   `pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt`
   — the stage's dependency step.
3. `python run_tests.py` in that context with both gate variables scrubbed
   → **exit 0, ran 794, failures 0, errors 0**, `test_report.json` written.
4. The Dockerfile's own report-green guard (lines 53–58) run **verbatim** against
   that report → **exit 0**.
5. The runtime stage's two artefact guards run verbatim against the same context:
   `test_report.json` present, `model_validation.json` present, and its schema
   check (lines 93–99) → **exit 0**. `model_validation.json` still carries
   `approved=false`, so `model_gatekeeper.check_live_allowed()` remains blocked.

### What this does NOT prove

* It did **not** run inside `python:3.13-slim`. Differences in the base image's
  OS packages, libc, or system libraries could still break the real build —
  notably `cryptography`, whose wheel differs between a Debian slim image and
  this container.
* It did **not** exercise BuildKit layer caching, `COPY --from=tests`, or the
  actual multi-stage assembly.
* It did **not** verify the final image's contents or that it starts.

A green Docker `tests` stage remains **unverified**. On the evidence available,
the step most likely to have failed (`RUN python run_tests.py`) now succeeds under
the pinned Python version, but that is an inference, not an observation.

**DOCKER_TEST_STAGE_PASS=NOT_RUN — base image blocked by egress policy (403 on
`production.cloudfront.docker.com`); stage-equivalent run locally on Python 3.13
passed, which does not substitute for the real build.**

---

## 9. Findings

### Blockers

**None.** The `e0e2f83` blocker is genuinely fixed: the build runner exits 0 with
the gate variables absent, the bootstrap provably precedes discovery, both runner
mutations are killed, and the harness is back to a non-vacuous 17/17 with the
duplicate guard independently proven to fire.

### High

**None.**

### Medium

**F1 — The build gate does not execute five tests that pytest does.**
`unittest.TestLoader.discover` collects 794 tests; pytest collects 799. The five
extra are module-level `def test_*()` functions in `tests/test_calibration.py`
(`test_brier_and_ece_known_values`, `test_calibration_curve_has_empty_bins_and_includes_one`,
`test_collect_pairs_accepts_shadow_records_and_skips_unresolved`,
`test_analyze_is_json_friendly_and_aliases_work`, `test_invalid_bin_count_rejected`),
which `discover` cannot see. `run_tests.py` writes the `test_report.json` that
`model_gatekeeper.check_live_allowed()` consults before LIVE, so those five
calibration tests are outside the artefact that gates promotion.

*Pre-existing, not introduced here* — `tests/test_calibration.py` is untouched by
this delta and the functions date from `b0d5327` (PR #34). The delta's own claim
(identical *assumptions* across runners) is met; identical *test sets* was never
claimed and is not achieved. Suggested follow-up: convert the five to
`unittest.TestCase` methods, or have `run_tests.py` also collect plain test
functions. Not a merge blocker for this delta.

### Low

**F2 — The dedicated ordering test is fooled by a comment.**
`test_the_gate_defaults_are_loaded_before_discovery` asserts
`src.index("_gates") < src.index("loader.discover")` on `run_tests.py`'s raw
source. Demonstrated here: a variant that moves the real import below
`loader.discover` while leaving a comment reading `# see tests/_gates.py for
details` above `main()` **satisfies the assertion** (index 855 < 938). The
mutation is still caught — by ~136 downstream test failures — so this is a weak
assertion, not an exploitable hole. A stronger form would import `run_tests` in a
subprocess and assert the variables are set before `main()` is reachable (which
`test_importing_run_tests_applies_the_shared_gate_defaults` already does, and
which did catch both real mutations).

**F3 — The runtime image ships the test plumbing.**
The Dockerfile's `runtime` stage does `COPY . .`, so `run_tests.py`,
`tests/_gates.py` and `_bootstrap.py` are present in the deployed image. Nothing
on the startup path imports them — verified dynamically (importing
`kalshi_alpha_bot` pulls in no `tests` module and leaves both gates `False`) and
the `CMD` is the bot, not the runner — so there is no execution path and no
finding of an actual gate change at runtime. It is a defence-in-depth gap: an
`ALLOW_ORDER_SUBMISSION=true`-setting module need not exist in the production
image at all. Pre-existing; the delta adds one more such file. Suggested
follow-up: exclude `tests/` and `run_tests.py` from the runtime stage's copy.

**F4 — The restart harness is not run by any automated gate.**
`tools/restart_harness.py` is executed by no CI step and by neither Docker stage
(the `tests` stage runs only `run_tests.py`). Its only automated linkage to the
suite is one source-text assertion that its ticker is not a daily ticker. The new
`[PRE]` anti-vacuity check therefore protects a **manually invoked** tool: a
future regression back to a vacuous harness would not be caught automatically.
This is a limitation of where the check sits, not a defect in the check.

### Verified out of delta scope — informational

Independently reproduced here, all in production code the delta does **not**
touch, and all already listed by the author as deliberate follow-ups. Confirming
the author's own account is accurate on these points:

* **NBSP-wrapped gate values read TRUE.** `"\xa0true\xa0"` → `True`, because
  Python's `str.strip()` removes U+00A0. Reproduced. The direction is permissive
  only for a value someone deliberately set to `true`; `"\xa0false\xa0"` correctly
  reads `False`. Not a fail-open on an unintended value.
* **Case-folding launders non-ASCII into the ticker grammar.** Reproduced:
  `'ßKXBTCD-A'` → canonical `'SSKXBTCD-A'`, `wellformed=True`; likewise `'ı'`→`I`
  and `'ﬀ'`→`FF`. The original non-ASCII string is what would be sent to the
  broker. **No daily-quarantine bypass was demonstrated** — `'KXBTCDß-A'` still
  classifies as daily and is quarantined, and zero-width characters
  (`'KXBTCD​-A'`) are correctly rejected as malformed. The weakness is that
  the "unclassifiable tickers are refused" invariant admits some non-ASCII input,
  not that a real daily market escapes quarantine.
* `kalshi_demo_execution_check.py` calling `client.create_order` directly, and the
  `_env_b` permissive parser still backing `ALLOW_FRESH_STATE` /
  `ALLOW_FALLBACK_CAPITAL`, were **not** re-verified in this review — they are
  outside the delta and outside this review's scope. Their presence in the
  author's follow-up list is noted but not independently confirmed here.

### Where an invariant could not be fully proven

* **The Docker `tests` stage (§8).** Blocked by egress policy at the base-image
  pull. The local equivalent passed on the pinned Python 3.13, but the real build
  remains unobserved. This is the one requested check that returns "not proven"
  rather than "pass".

---

## Final verdict

```
DELTA_SECURITY=PASS
BUILD_RUNNER_COMPATIBILITY=PASS
PRODUCTION_DEFAULT_ISOLATION=PASS
RESTART_HARNESS=PASS
B1_B2_REGRESSION=PASS

BLOCKERS=NONE
HIGH_FINDINGS=NONE
MEDIUM_FINDINGS=1 (F1: run_tests.py/unittest discovery misses 5 pytest-collected
                   tests in tests/test_calibration.py, so they are absent from the
                   test_report.json the LIVE gatekeeper reads. Pre-existing,
                   not introduced by this delta.)
LOW_FINDINGS=3    (F2 ordering test is comment-foolable; F3 runtime image ships
                   the test plumbing; F4 the restart harness is not run by any
                   automated gate.)

PR_MERGE_READY=YES
PR_DEPLOY_READY=NO
PR_D_STATUS=DO_NOT_DEPLOY
MODEL_APPROVED=false
READY_FOR_DEMO_CANARY=NO
READY_FOR_LIVE=NO
```

`PR_MERGE_READY=YES` reflects the delta only: it changes no production code, it
fixes a real and independently reproduced build blocker, it restores a real and
independently reproduced test invariant, and every mutation applied to it was
killed. It is **not** a statement that the branch is deployable. F1 should be
tracked before the `test_report.json` artefact is relied on as a complete record
of the suite.

---

### Reproduction notes

Repository state after this review: `git status` clean, `git diff 5d6e994` empty
(0 bytes), all five touched files byte-identical to their committed blobs by SHA.
Six mutations were applied during the review (2 to `run_tests.py`, 3 to
`tools/restart_harness.py`, 1 hypothetical evaluated in memory only) and all were
reverted. A temporary git worktree at `e0e2f83` was created for the baseline
reproduction and removed. Generated `test_report.json` and `.pytest_cache/` were
deleted; both are ignored and neither was ever tracked.
