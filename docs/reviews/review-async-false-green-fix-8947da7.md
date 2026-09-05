# Release-assurance review — async false-green fix

* **Subject**: `claude/railway-atlas-third-door-fix` @ `8947da7993f061a40e1d0f3355051d67abc87f0b`
* **Base**: `main` @ `3d1cc2bf400d1bb64fe8c079e9e174bd6f054c37` (merge base `0e44b40`, whose tree main carries byte for byte)
* **Delta**: 5 files, +167 −7. `tests/_collect.py`, `tests/_netblock.py`,
  `tests/test_prod_access_mode.py`, `tests/test_runner_parity.py`,
  `docs/premerge-fix-pack.md`
* **Date**: 2026-09-05
* **Reviewer independence**: **NOT INDEPENDENT.** This review was produced by the
  session that authored `8947da7`. Under the rule this engagement has followed —
  the author of a change is never its only reviewer — the verdict below cannot
  serve as the independent sign-off. Every claim is backed by a reproducible
  command and observable side effects rather than by judgement, so an
  independent reviewer can re-run all of it; but they still must.

Nothing was merged, deployed, or changed on Railway. No credential was read or
requested. No broker write was authorized.

---

## Verdict

```
BLOCKERS = 0
HIGH     = 0
MEDIUM   = 2
LOW      = 3

MERGE_READY = YES   (conditions below; not a substitute for independent review)
```

The fix is a strict improvement. It closes three of four false-green doors,
opens none, regresses nothing, and changes no production byte. The currently
deployed code is strictly worse than the fix: on `main` the release runner
writes a **green** `test_report.json` for a test whose body never executed.

Two conditions should land with it:

1. **Correct the docstring.** `tests/_collect.py` now claims "THREE doors …
   All three are closed below" and names the decorator-hidden case as one of
   them. That case is closed only at module level, not for methods. An
   inaccurate safety claim in a safety-critical file is worse than no claim,
   because it stops the next reader from looking.
2. **Close the fourth door before CAPITAL_LIVE** (MEDIUM-1). A verified
   one-line remedy exists.

---

## Method

Two detached worktrees, `main@3d1cc2b` and `fix@8947da7`. Twelve probe modules,
one per test shape. **Every probe writes a marker file from inside its body**,
so "did this test actually execute?" is answered by an observable side effect
rather than by an exit code or a pass count. Each probe was run through the
real collector, the real release runner (`run_tests.py`), and real `pytest`.

Python 3.11.15, pytest 9.1.1.

---

## 1. The false green, reproduced end to end on deployed code

`main@3d1cc2b` + one `async def test_*` method on a `unittest.TestCase` whose
body raises:

```
EXIT=0
Discovered 950 tests (945 via unittest discovery + 5 module-level)
Ran 950 tests in 42.693s
OK
body executed?  NO
report written? YES   ran=950 failures=0 errors=0
```

A green artifact, on disk, describing a body that never ran. That artifact is
what `model_gatekeeper.check_live_allowed()` consults before real money.

Same probe on `fix@8947da7`:

```
EXIT=1
CRITICAL: tests collectable only by pytest -- test_report.json would
under-report the suite the LIVE gate relies on:
  - test_zz_probe.AsyncInsideTestCase.test_probe_async_method
    (async def; needs an event loop, pytest-only)
report unchanged? YES-untouched
```

The refusal names the offender and, decisively, **does not overwrite the
existing report** — verified by md5 before and after. Refusing is worthless if
the refusal path still leaves a green file behind.

## 2. Probe matrix

`refused` = listed in `diagnostics["uncollectable"]`. `body ran` = marker file
written. Build runner = the real `run_tests.py` collector plus `TestCase.run`.

| probe shape | main: refused | main: body ran | main: verdict | fix: refused | fix: body ran | fix: verdict |
|---|---|---|---|---|---|---|
| `async def` method on `TestCase` | no | **no** | **false PASS** | **yes** | n/a | refused |
| `async def` module-level | yes | n/a | refused | yes | n/a | refused |
| generator method on `TestCase` | no | **no** | **false PASS** | **yes** | n/a | refused |
| generator module-level | yes | n/a | refused | yes | n/a | refused |
| async-generator method | no | **no** | **false PASS** | **yes** | n/a | refused |
| async-generator module-level | yes | n/a | refused | yes | n/a | refused |
| **decorated async method** | no | **no** | **false PASS** | **no** | **no** | **false PASS** |
| decorated async module-level | no | no | FAIL (runtime backstop) | no | no | FAIL (runtime backstop) |
| ordinary sync passing | no | **yes** | PASS | no | **yes** | PASS |
| ordinary sync failing | no | **yes** | FAIL | no | **yes** | FAIL |
| `subTest` | no | **yes** | PASS, 3 subtests | no | **yes** | PASS, 3 subtests |
| `IsolatedAsyncioTestCase` | no | **yes** | PASS | no | **yes** | PASS |

Three doors close. Ordinary tests, failing tests, subtests and
`IsolatedAsyncioTestCase` are untouched, and their bodies demonstrably execute.
One door stays open, and it is the one the docstring claims to have closed.

## 3. Counts

Clean `fix@8947da7`, both runners run independently:

| source | count |
|---|---|
| `run_tests.py` discovered | 952 (947 unittest + 5 module-level) |
| `run_tests.py` executed (`ran`) | 952 |
| `test_report.json` `collected` | 952 |
| `pytest` collected | 952 |
| `pytest` executed | 952 passed, 228 subtests passed |

952 = the 949 on main plus the three tests this change adds. No total is
hard-coded anywhere; the parity test compares the runners against each other.

## 4. Adversarial mutations

Each mutation edits `tests/_collect.py` to restore the false green, then runs
the **full release runner**. A mutation is killed only if `run_tests.py` exits
non-zero.

| # | mutation | result | killed by |
|---|---|---|---|
| A1 | delete the method scan entirely | KILLED | async + generator method tests |
| A2 | method scan always returns nothing | KILLED | async + generator method tests |
| A3 | exempt every `TestCase`, not just `IsolatedAsyncio` | KILLED | async + generator method tests |
| A4 | exempt by class **name** instead of by type | KILLED | async method test |
| A5 | suite walker stops at the top level (no recursion) | KILLED | async + generator method tests |
| A6 | check coroutines only, drop generators | KILLED | generator method test |
| A7 | **anti-vacuity**: drop the `IsolatedAsyncio` exemption | KILLED | the CONTROL test |

**7 of 7 killed, 0 survivors.** A7 matters most: it proves the control is not
vacuous. Without it, a collector that refused *every* coroutine method would
satisfy all six other tests while breaking a legitimate, working test style.

## 5. Robustness of the new suite walker

| shape | result |
|---|---|
| test module with an import error (`_FailedTest` in the suite) | no crash, collection completes |
| non-function attribute shadowing a method name | no crash |
| async method attached dynamically after class creation | refused |
| async method **inherited** from a base `TestCase` | refused on base and child |

Inheritance does not open a hole, and malformed discovery does not crash the
scan — either would have been a worse bug than the one being fixed.

## 6. Safety guards

The delta touches **no production file**. Verified by object hash, not by
reading the diff:

`kalshi_client.py`, `kalshi_alpha_bot.py`, `config.py`, `model_gatekeeper.py`,
`order_manager.py`, `execution_engine.py`, `position_manager.py`,
`opportunity_pipeline.py`, `strategy_router.py`, `risk_manager.py`,
`Dockerfile`, `railway.json`, `requirements.txt`, `run_tests.py` — **all
identical between `3d1cc2b` and `8947da7`.**

No added or removed line anywhere in the delta references `PROD_ACCESS_MODE`,
`LIVE_BROKER_WRITES_AUTHORIZED`, `ALLOW_ORDER_SUBMISSION`, `MODEL_APPROVED`,
`DAILY_RESEARCH_ORACLE_APPROVED`, `KXBTCD`, `_assert_broker_write_allowed`,
`_is_mutating_method`, `create_order`, `cancel_order` or `KILL_SWITCH`.

Confirmed behaviourally as well:

* guard suites on the fix branch — `test_live_write_authorization`,
  `test_prod_access_mode`, `test_pre_live_gate_matrix`,
  `test_broker_write_guard_pinning`, `test_shadow_write_layer_isolation` —
  **98 passed, 82 subtests passed**;
* with a green, complete 952-test report on disk and the model flags forced
  permissive (`NO_LIVE_PROMOTION=0`, `MODEL_APPROVED_FOR_LIVE=YES`),
  `check_live_allowed()` still returns
  `(False, ['model_validation.json absent ou non approuve'])`.

The scientific gate is untouched and still refuses.

The two changes inside safety *tests* are strengthenings, not relaxations:
`connect_ex` now records its attempts instead of silently returning 1, and
`StartupMatrix` now asserts the network block actually installed — previously
"no attempts recorded" was satisfied both by a guarded child and by a silently
unguarded one.

---

## Findings

### MEDIUM-1 — a fourth door remains, and the docstring says it does not

An `async def` test method wrapped in a `functools.wraps` decorator is
invisible to `inspect.iscoroutinefunction`, so `_unsupported_methods` does not
refuse it. `TestCase.run` calls it, receives a coroutine, discards it, and
records a pass. Reproduced on the fix branch: not refused, body never ran,
reported as passing.

The module-level path does not have this hole, because `_case_for` wraps each
function and `_unexecuted_body` inspects the *returned value* at runtime —
verified: the decorated module-level probe fails correctly. The method path
ships only the static layer. The fix's own docstring argues that one layer
cannot be enough ("Two layers close it, because one cannot") and then ships one
layer for methods while claiming all three doors are closed.

**A verified remedy exists.** CPython already emits, synchronously, for exactly
this case:

```
DeprecationWarning: It is deprecated to return a value that is not None
from a test case
```

Promoting that one warning to an error makes the failure explicit:

```
wasSuccessful: False   failures: 0   errors: 1
  -> DeprecationWarning: It is deprecated to return a value that is not None
     from a test case
```

That is shape-independent: it catches any method returning non-`None`,
decorated or not, coroutine or generator, without needing to out-guess
`inspect`. It gives the method path the same two-layer defence the module path
already has. Recommended before CAPITAL_LIVE; the filter should be narrowed to
this specific message rather than promoting all `DeprecationWarning`s.

### MEDIUM-2 — the parity harness is structurally blind to this defect class *(pre-existing)*

`test_canonical_collection_matches_real_pytest` is the system's main defence
against the two runners disagreeing. It cannot catch any method-shaped false
green, because **pytest false-greens them identically**. Measured on `main`:

| probe | pytest result | body ran under pytest |
|---|---|---|
| async method on `TestCase` | `1 passed, 2 warnings` | no |
| generator method | `1 passed, 1 warning` | no |
| async-generator method | `1 passed, 1 warning` | no |
| decorated async method | `1 passed, 2 warnings` | no |

pytest delegates `unittest.TestCase` subclasses to unittest's own machinery, so
it inherits the same behaviour. Both runners collect one test and both pass it:
the counts agree while both are wrong.

Consequence for this review: the static refusal in `_collect.py` is the **sole**
defence for this class. There is no independent cross-check, and any future
regression here is invisible to the parity harness. This is not introduced by
the delta, but it is why the delta matters more than its size suggests.

### LOW-1 — the LIVE gate ignores the completeness fields *(pre-existing)*

`check_live_allowed()` reads only `failures`, `errors` and the age of
`generated_ts`. It never reads `ran`, `collected` or `skipped`, and
`run_tests.py` never asserts `ran == collected`. A green report describing a
much smaller suite, or one where everything was skipped, satisfies the gate.

Fail-closed still holds in the Docker pipeline — a refusal exits 1, the build
fails, no image ships — so this is a defence-in-depth gap rather than a live
hole. But the artifact carries completeness evidence the gate declines to use.

### LOW-2 — duplicate entries in the refusal list

Because `_unsupported_methods(suite)` runs after the synthesized module-level
cases are added, a dynamically attached module-level async function is reported
twice: once as a module function and once as a method of the generated class.
Cosmetic, and it errs toward noise rather than silence, but the refusal list is
operator-facing output.

### LOW-3 — probe files can be left behind on abnormal termination

The three new tests write `tests/test_zz_*_probe.py` into the real test
directory and remove them in a `finally`. A `SIGKILL` between write and cleanup
leaves a stray probe that the next run would collect and refuse — a red build
that looks like a genuine defect. The pattern predates this change; a temporary
directory on `sys.path` would avoid it.

---

## What would change the verdict

* If the decorated-method door were reachable by accident rather than by
  deliberately writing a decorator around an async test, MEDIUM-1 would be
  HIGH. It is not: every naturally occurring shape is now refused.
* If the delta had touched any production file, no amount of test evidence
  would support merging without a full money-path re-review. It touches none.
