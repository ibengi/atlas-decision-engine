# Independent critical review — RC-1 (`826d12e..e2d9d41`)

Reviewer: independent session. No authoring, editing, testing, designing or
reviewing history on `151860a`, `5757c09`, `e0e2f83`, `5d6e994`, `826d12e`,
`e2d9d41`. `REVIEWER_NOT_INDEPENDENT=FALSE`.

Scope: the delta `826d12e..e2d9d41` only. Parent deployed baseline `9b906e8`
(deployment `2a3da86f`, DEMO). No production code was modified, nothing was
merged, deployed, promoted or enabled; no Railway variable was written. All
mutation testing was done in throwaway `git worktree` checkouts, which have
been removed; `git status` is clean.

Every claim below was re-derived. The commit message, code comments,
`release_evidence.json` and `docs/LIVE_CONVERGENCE_BOARD.md` were read but used
as *claims to test*, never as evidence.

---

## Verdict

| Field | Result |
|---|---|
| `BLOCKERS=` | **0** |
| `HIGH=` | **1** (M1) |
| `MEDIUM=` | **3** (M2, M3, M4) |
| `LOW=` | **5** (L1–L5) |
| `A_BUILD_TRUTH=` | **PASS** (with M1) |
| `COUNTS_MATCH=` | **YES — 828 = 828 = 828, and the *id sets* are identical, not merely the totals** |
| `MUTATION_A_REPRODUCED=` | **YES — both sides, independently** |
| `B_MONEY_PATH=` | **PASS** |
| `UNPROTECTED_PATHS=` | **0 order-submission paths. 3 cancel paths ungated by design (see L1 on the wording "PROTECTED=5")** |
| `C_STRICT_CONFIG=` | **PASS** |
| `D_BUILD_PINNING=` | **FAIL as stated / PARTIAL in substance** (M2) |
| `VACUOUS_TESTS_FOUND=` | **2** (M3) |
| `SURVIVING_MUTATIONS=` | **1 class** (M1) |
| `RC1_MERGE_READY=` | **YES**, conditional on M1 (one-line fix) |
| `RC1_DEPLOY_READY=` | **NO for capital. YES for a DEMO redeploy** — see "Deploy readiness" |

---

## A. Build test truth — PASS, with one surviving mutation class

### A.1 Counts — verified, and verified more strongly than claimed

Reproduced from scratch after installing `requirements.txt` +
`requirements-dev.txt` (note: the system `cryptography` 41.0.7 is broken —
`ModuleNotFoundError: _cffi_backend` → `pyo3_runtime.PanicException` — exactly
as `requirements-dev.txt` warns; `pip install --ignore-installed cryptography`
fixes it. Without that fix pytest aborts collection at 818 and every count in
this section is wrong.)

| Source | Count |
|---|---|
| `pytest tests/ --collect-only -q` | **828** |
| `tests/_collect.collect("tests")` → `diag["total"]` | **828** |
| `run_tests.py` → `test_report.json` `"collected"` | **828** |
| `run_tests.py` → `test_report.json` `"ran"` | **828** |
| `suite.countTestCases()` | 828 |
| distinct `TestCase.id()` values | **828 — no duplicates** |

`ran == collected == 828`, so nothing is collected and then silently dropped.

I went further than the claim: I normalised both collections to
`(module, test name)` and **differenced the sets**.

```
PYTEST-ONLY: []
BUILD-ONLY:  []
```

The two runners see the *same tests*, not merely the same number — the counts
cannot be matching by an offsetting double-count and miss.

Both runners green on the pristine tree:
`pytest` → `828 passed, 142 subtests passed`, exit 0.
`run_tests.py` → `ran:828, failures:0, errors:0, skipped:0`, exit 0.

The five module-level functions are confirmed to be exactly
`tests/test_calibration.py`'s (Brier, ECE, calibration curve, pair collection,
bin-count validation), and `test_calibration.py` is the **only** module in the
tree written in that style.

### A.2 Mutation A — reproduced on both sides

Injected `raise AssertionError(...)` as the first statement of
`test_brier_and_ece_known_values`.

**At `9b906e8`:**
```
Discovered 794 tests in 51 modules
run_tests.py EXIT = 0
test_report.json = {"ran":794,"failures":0,"errors":0,"skipped":0,"failed_tests":[]}
pytest          = 1 failed, 798 passed   EXIT = 1
```
**The mutation SURVIVES the build.** The report is green, exit is 0, and the
image would ship. I also confirmed the Dockerfile's second guard (`RUN python
-c ... test_report.json non vert`) does **not** catch it: the report is
*honestly* green about a suite that never contained the broken test.

**At `e2d9d41`:**
```
Discovered 828 tests (823 via unittest discovery + 5 module-level)
run_tests.py EXIT = 1
test_report.json failures:1, failed_tests:
  ["test_brier_and_ece_known_values (tests.test_calibration.ModuleLevelTests...)"]
```
**Killed.** `MUTATION_A_REPRODUCED = YES, both sides.`

### A.3 Adversarial probes of the collector

| Probe | Result |
|---|---|
| Double-count a function present in two modules | **No.** `__module__` filter holds. |
| Miss a module in a nested test subpackage | **Yes, but caught.** `_module_names` uses a non-recursive `root.glob`, so a module-level test in `tests/sub/` is missed (829 vs pytest 830) — the parity test then **fails loudly**. Not silent. |
| `run_tests.py` refuses a pytest-only test | **Yes.** A parameterised module-level test gives `EXIT=1`, names the offender, and **exits before writing `test_report.json`** — no under-reporting artifact is produced. |
| Wrap a function so it silently never executes | **YES — see M1.** |

### M1 (HIGH) — `_case_for` silently passes an `async def` test; the build ships green while pytest is red

`_collect._case_for` wraps a module-level function as `def method(self, _f=func): _f()`.
For an `async def test_*`, `inspect.isfunction` is `True` and
`inspect.signature(...).parameters` is empty, so it is accepted, wrapped, and
**counted** — but calling it only builds a coroutine that is never awaited. The
body never runs.

Reproduced on `e2d9d41` with a probe asserting `False`:

```
pytest      : 1 failed, 828 passed        (FAILED ... async def functions are not natively supported)
run_tests.py: EXIT=0  ran:829 failures:0 errors:0 skipped:0
parity test : 7 passed
```

The counts still match (829 = 829), so `uncollectable` stays empty and
`test_runner_parity.py` stays green. This is precisely the failure mode
Workstream A exists to close — a build that is green about a test that never
executed — reopened through a different door. A generator (`yield`) test
behaves the same way in the build (vacuous pass), though pytest errors at
collection there, so it is at least noisy.

Impact today is **latent**: the repository contains zero async tests and no
async test style. But the guarantee the RC-1 evidence rests on is "the build
runs what pytest runs", and that guarantee is one `async def` away from being
false again — in the report the LIVE gatekeeper reads.

Fix is one line in `collect()`, alongside the existing parameter check:

```python
if inspect.iscoroutinefunction(func) or inspect.isgeneratorfunction(func):
    uncollectable.append(f"{modname}.{name} (async/generator; pytest-only)")
    continue
```

This is the reason A_BUILD_TRUTH is PASS *with a condition* rather than clean.

---

## B. Money-path kill switch and client backstop — PASS

### B.1 Inventory, rebuilt from scratch across the whole repository

I did not read the author's inventory before building my own. Searching the
entire tree (root, `tools/`, `research/`) for `create_order`, `cancel_order`,
`replace_order`, `amend_order`, `decrease_order`, and for every non-GET HTTP
call:

**Write methods defined:** exactly two — `KalshiClient.create_order`
(`kalshi_client.py:252`) and `KalshiClient.cancel_order` (`kalshi_client.py:347`).
No `replace_order`, no `amend_order`, no batch endpoints exist.

**Call sites — 5 total, confirming `BROKER_WRITE_PATHS_TOTAL=5`:**

| # | Site | Kind | Status |
|---|---|---|---|
| 1 | `order_manager.py:784` | create | full gate ladder, then client backstop |
| 2 | `kalshi_demo_execution_check.py:113` | create | client backstop applies |
| 3 | `order_manager.py:938` | cancel | ungated **by design** |
| 4 | `order_manager.py:1058` | cancel (recovery) | ungated **by design** |
| 5 | `kalshi_demo_execution_check.py:176` | cancel | ungated **by design** |

**Raw HTTP audit — the decisive check.** Across the entire repository there are
exactly **two** non-GET calls to Kalshi:

```
kalshi_client.py:302   self._req("POST",   self.ORDERS_V2_PATH, json=payload)   # inside create_order
kalshi_client.py:353   self._req("DELETE", f"{self.ORDERS_V2_PATH}/{order_id}") # inside cancel_order
```

No other module constructs a `requests` call to a Kalshi host. `alert_notifier.py`
POSTs to an operator webhook; `btc_context.py`, `health_monitor.py` and
`research/fetch_btc_candles.py` are GETs to non-broker hosts. Therefore
`create_order` genuinely is the **single unavoidable choke point** for order
submission, and gating it there is sufficient. `UNPROTECTED_PATHS = 0` for
submission.

### B.2 Guard behaviour — verified by mutation, not by reading

The three client guards are the first statements of `create_order`, before any
payload construction. `_req` calls `self.session.request(...)`, so the tests'
`session.request.call_count == 0` assertion is the right probe and is **not**
vacuous.

Ladder in `place_and_track` (verified by reading the code, in order):
`ALLOW_ORDER_SUBMISSION` → **`KILL_SWITCH`** → `ticker_is_wellformed` →
`daily_quarantine_blocks`. The kill switch is read before the dedup lock and
the pending-intent record are written, so a blocked order leaves no residue.

Every guard mutation was killed by a **named** test:

| Mutation | Killed by |
|---|---|
| `place_and_track` kill-switch block deleted | `MoneyPathReReadsTheKillSwitch::test_kill_switch_blocks_before_create_order` (+2 more) |
| client `ALLOW_ORDER_SUBMISSION` guard deleted | `ClientLayerBackstop::test_refuses_when_submission_is_disabled`, `BrokerWriteInventoryStaysClosed::test_the_backstop_that_makes_direct_calls_safe_exists` |
| client `KILL_SWITCH` guard deleted | `ClientLayerBackstop::test_refuses_when_the_kill_switch_is_engaged` (+1) |
| client quarantine guard deleted | `ClientLayerBackstop::test_refuses_a_quarantined_daily_ticker` (+1) |
| `place_and_track` quarantine guard neutered | 5 tests across `test_daily_quarantine.py` and `test_money_path_kill_switch.py` |

`SURVIVING_MUTATIONS` among the B guards: **none**.

### B.3 Can anything still reach the broker with the switch engaged?

**Order submission: no.** Both `create_order` call sites pass through the
client guard, and no other code can emit the POST.

**Cancellation: yes, deliberately.** DELETE still goes out with the switch
engaged.

### B.4 The recovery-cancel path (`order_manager.py:1058`) — argued both ways

*For leaving it ungated (my conclusion — correct as built).* Both cancel sites
fire only when `status not in TERMINAL and filled < count`, i.e. strictly on
resting, under-filled orders; a cancel can only **reduce** exposure. The
recovery path runs at startup to reconcile orders left open by the previous
process. Gating it on `KILL_SWITCH` would mean that engaging the breaker after
a crash *leaves resting orders live on the exchange and still able to fill* —
the breaker would increase exposure, which inverts its purpose. The path is
also fail-closed on its own terms: it raises when `reduced_by` does not prove
the reduction, rather than assuming success.

*Against.* Three honest counter-arguments. (i) A cancel is still an
authenticated broker write and consumes rate limit; a looping process with the
switch engaged can hammer DELETE. (ii) Some operators read a kill switch as
"touch nothing at all" — freeze for forensics — and an automatic recovery-cancel
destroys the state they wanted to inspect. (iii) It means "no network call
reaches the broker when the switch is engaged" is not literally true, so the
claim needs the qualifier "no *order-submitting* call".

On balance the current design is right; the standard meaning of a trading kill
switch is "open no new risk, and let open risk be closed". I would not change
it. I would change the *wording* of the claim (L1).

### B.5 Does the backstop break a legitimate flow?

One flow changes: `kalshi_demo_execution_check.py`. Because production runs
with `ALLOW_ORDER_SUBMISSION` set (value not readable by this session, claimed
false), the probe now cannot submit unless that gate is opened. It degrades
cleanly — `except KalshiAPIError` prints `[ORDER_SUBMIT_FAILED]`, writes the
proof file, exits 1 — so nothing crashes. That coupling is arguably correct
(the probe really does place a live DEMO order), but it means the "real demo
proof" evidence artifact can no longer be regenerated without opening the
submission gate. See L2 for the misleading `http_status=0` in that output.

---

## C. Strict safety config — PASS

### C.1 The polarity argument is correct

I enumerated the value space against both parsers rather than trusting the
reasoning. `_env_b` returns `True` for **everything** it does not recognise as
false, including `""`, `"   "`, `"flase"`, `"maybe"`, `"2"`, `"null"`.

- For a gate that **permits** (`ALLOW_*`), `_env_b`'s bias points toward
  *open*. Migrating to `_env_gate(default=False)` is strictly safer. Correct.
- For a switch that **forbids** (`KILL_SWITCH`), `_env_b`'s bias happened to
  point toward *engaged*. A naive `_env_gate(default=False)` would have turned
  the breaker **fail-OPEN** — `KILL_SWITCH="treu"` would have let orders
  through where it previously cut. `on_invalid=True` prevents exactly that.

The author's reasoning is sound, and mutation-confirmed: flipping
`on_invalid=True` → `False` is killed by
`StrictSafetyConfigParsing::test_kill_switch_reads_garbage_as_ENGAGED`.
Reverting either `ALLOW_*` to `_env_b` is killed by
`test_allow_gates_read_garbage_as_closed`.

### C.2 "Identical for absent and canonical values" — needs one correction

Absent → identical (`False`) for all three. Canonical **true** words
(`true/1/yes/y/on`, any case, surrounding whitespace) → identical. But:

| Value | `_env_b` (old) | `_env_gate` (new) |
|---|---|---|
| `"n"`, `"off"`, `"OFF"` | **True** | **False** |

`"n"` and `"off"` are canonical false words in the project's own
`GATE_FALSE_WORDS`, and their reading **changed**. So "behaviour is identical
for canonical values" is not literally true. The change is in the *correct*
direction in every case (an operator typing `off` means off), and for the
`ALLOW_*` gates it is also the *safe* direction. Precision correction, not a
defect — recorded as **L3**.

The author self-reported only `"off"`; `"n"` and `"OFF"` share the regression
and are not covered by
`test_kill_switch_reads_garbage_as_ENGAGED` (which tests `""`, `"   "`,
`"maybe"`, `"flase"`, `"2"`).

### C.3 The self-reported `KILL_SWITCH="off"` regression — acceptable

Before: `KILL_SWITCH=off` silently **ENGAGED** the breaker. After: it
**DISENGAGES**. This is the one input in the migration moving toward
permissive, and it is the only one that could turn a non-trading system into a
trading one.

I verified the mitigating fact directly rather than accepting it. Reading the
Railway production service `ibengi/atlas-decision-engine`
(project `valiant-respect`, environment `production`), the variable list is:

```
ALLOW_ORDER_SUBMISSION, ANTHROPIC_API_KEY, BTC_DAILY_SHADOW_ENABLED, DATA_DIR,
KALSHI_DEMO_KEY_ID, KALSHI_DEMO_PRIVATE_KEY, MAX_CONTRACTS_PER_ORDER,
REQUIRE_PERSISTENT_STATE, RESEARCH_API_TOKEN  (+ RAILWAY_* injected)
```

**`KILL_SWITCH`, `ALLOW_FRESH_STATE` and `ALLOW_FALLBACK_CAPITAL` are all
absent.** The claim is confirmed. `DAILY_RESEARCH_ORACLE_APPROVED` and
`MODEL_APPROVED*` are also absent, so both default to closed. The regression is
therefore **inert in the current deployment**, and the new reading matches
operator intent. **Acceptable.**

Two honest limits on that check: this session's Railway credential returns
variable *names* only (`valuesRedacted: true`), so I **could not** confirm
`ALLOW_ORDER_SUBMISSION=false` — only that it is set. And
`REQUIRE_PERSISTENT_STATE` is set with an unreadable value; that matters
because `ALLOW_FRESH_STATE` is only consulted when it is true
(`persistence.verify_state_root`). Since `ALLOW_FRESH_STATE` is *absent*, both
parsers return `False` either way, so there is no behaviour change regardless.

### C.4 Hunt for other safety-relevant booleans still on `_env_b`

All remaining `_env_b` booleans, classified by which way the parser's bias
points:

| Variable | Bias points toward | Risk |
|---|---|---|
| `SHADOW_MODE` | True = *do not send* | safe |
| `REQUIRE_PERSISTENT_STATE` | True = *stricter* | safe |
| `CANCEL_UNFILLED_ORDERS` | True = *reduce exposure* | safe |
| `ONE_TRADE_PER_MKT` | True = *more restrictive* | safe |
| `DRY_RUN` | True = *do not act* | safe |
| `DASHBOARD_ENABLED`, `API_CACHE_ENABLED`, `API_PARALLEL_ENABLED`, `SCANNER_PARALLEL_SERIES`, `BTC_CONTEXT_CYCLE_CACHE`, `ALERT_*` | feature/perf | not order-gating |
| **`KELLY_ENABLED`** | **True = larger positions** | **the one that points at risk** |

`KELLY_ENABLED` is the single remaining boolean where a typo (`KELLY_ENABLED=off`,
`=flase`, `=""`) **enables** the riskier behaviour. It is absent in production,
so nothing is live, but it is the correct next candidate for `_env_gate` and
the only one I would name.

**The author's bound claim — verified, with two caveats.** `position_sizer.py:79-81`:

```python
max_pct = min(float(CFG.MAX_POS_PCT), float(getattr(CFG, "KELLY_MAX_POS_PCT", 10.0)))
pct = min(full * fraction * 100.0, max_pct)
```

The bound `min(MAX_POS_PCT, KELLY_MAX_POS_PCT)` is real. With shipped defaults
(`MAX_POSITION_PCT=1.0`, `KELLY_MAX_POSITION_PCT=10.0`) the effective cap is
**1.0 %** — *tighter* than the legacy path, which allows up to
`min(base_pct, MAX_POS_PCT)` with `base_pct` up to 2.0. So under defaults,
enabling Kelly by accident cannot increase position size. Two caveats
(**L4**): (i) if `MAX_POSITION_PCT` were raised above 2.0, the Kelly branch's
ceiling rises with it while legacy stays ≤ 2 %; (ii) the `KELLY_MIN_BET` floor
at lines 92-94 can raise `alloc` back **above** the percentage bound on a small
account (`$1` floor is 5 % of a `$20` balance). Both are finally clamped by
`MAX_CONTRACTS_PER_ORDER`, which is set in production.

---

## D. Build pinning — FAIL as stated, PARTIAL in substance

### D.1 The pin is not in effect, and may not be able to take effect

Reading the live Railway service configuration:

```json
"build": { "builder": "RAILPACK", "buildEnvironment": "V3" }
"source": { "repo": "ibengi/atlas-decision-engine", "branch": "main" }
```

The service-level builder is **still `RAILPACK`**, and the service deploys from
**`main`**, where `railway.json` does not exist. So today **nothing is pinned**.
That is expected for an unmerged RC and is not itself the finding.

### M2 (MEDIUM) — `railway.json` is a deprecated mechanism with a hard cutoff on 2026-12-01

From the Railway documentation (`docs.railway.com/config-as-code` and
`docs.railway.com/infrastructure-as-code`), retrieved during this review:

> "The settings in the dashboard will not be updated with the settings defined
> in code. **Configuration defined in code will always override values from the
> dashboard.**"

> "**Config as Code is deprecated.** … Existing `railway.json` / `railway.toml`
> files continue to work for services that already use them until
> **2026-12-01** (hard cutoff). **New services cannot opt into Config as Code.**"

> "Config as Code is still read from your service repository during deploy for
> **existing (legacy) services** … New services cannot opt into Config as Code."

Two consequences, both material:

1. **Precedence over the dashboard is real** — so *if* the file is read, the
   `DOCKERFILE` pin does beat the service's `RAILPACK` setting. The author's
   central premise is correct.
2. **But this service has never used Config as Code.** Whether it counts as an
   "existing (legacy) service" that may still opt in, or a service for which
   opt-in is now closed, is genuinely ambiguous from the documentation and
   **cannot be settled from here**. It can only be settled by deploying and
   reading the deployment details pane, where a settings row sourced from the
   file carries a file icon.
3. **Regardless of which reading is right, the pin expires in under three
   months** (today is 2026-09-04; cutoff 2026-12-01). After that date
   `railway.json` stops being read, the service silently reverts to its
   dashboard `RAILPACK` setting, and **nothing in this repository or its tests
   will notice** — `test_build_pinning.py` will still be green.

The durable fix is `.railway/railway.ts` (Infrastructure as Code, generally
available), via `railway config migrate`. At minimum, the expiry needs to be an
explicitly tracked, dated finding rather than an unstated assumption.

### D.2 Is the accompanying test meaningful or decorative? — both, in different parts

I mutation-tested it rather than judging by inspection. Every Dockerfile
mutation was caught:

| Mutation | Killed by |
|---|---|
| rename the `AS tests` stage | `test_that_dockerfile_still_runs_the_test_stage` |
| delete `RUN python run_tests.py` | `test_that_dockerfile_still_runs_the_test_stage` |
| delete `COPY --from=tests … test_report.json` | `test_that_dockerfile_still_runs_the_test_stage` |
| delete `test -f /app/model_validation.json` | `test_the_runtime_stage_still_verifies_both_gate_artifacts` |
| `builder` → `RAILPACK` | `test_builder_is_explicitly_the_dockerfile` |
| `dockerfilePath` → a nonexistent file | caught |

So as a **regression guard on file content** the test is genuinely load-bearing,
not decorative — it is the part of Workstream D I would keep unchanged.

What it **cannot** do is verify that Railway honours the pin. It asserts what
the repository says, never what the platform did. The module docstring's claim
that `railway.json` "removes the ambiguity. These tests keep it removed"
overstates by exactly that gap. `D_BUILD_PINNING=FAIL` refers to the *claim*;
the *tests* pass on their own terms.

The only evidence that would close D is a deployment of this commit whose
details pane shows `builder` sourced from `railway.json`. **Not run** — deploying
is out of scope for this review.

---

## Vacuous tests and tests that pass for the wrong reason

### M3 (MEDIUM) — the two client-layer anti-vacuity controls never reach the request layer

`test_money_path_kill_switch.py` states "Anti-vacuity throughout: every refusal
test is paired with a control in which the same call SUCCEEDS once the
condition is lifted". For the `ClientLayerBackstop` class, **that pairing does
not hold**. I instrumented both controls:

```
private key loaded (_pk is not None): False
create_order raised: KalshiAPIError HTTP 0: POST /portfolio/events/orders:
                     requete authentifiee IMPOSSIBLE — cle RSA non chargee
session.request.call_count = 0          # <-- never reached the network layer
cancel_order raised: KalshiAPIError HTTP 0: DELETE ... cle RSA non chargee
cancel session.request.call_count = 0   # <-- never reached the network layer
```

`tests/_bootstrap.py` supplies dummy credentials, so `KalshiClient._pk` is
`None`, and `_req` refuses every `/portfolio` path **before** `session.request`.
Consequently:

- `test_control_gates_open_reaches_the_request_layer` — its docstring says
  "with every gate open the call proceeds past the guards" and that "without
  this, all three refusals above could be produced by a `create_order` that
  never works at all". It proves neither. Its only assertion is
  `assertNotIn("refuse au niveau client", str(exc))`, which a `create_order`
  replaced wholesale by `raise KalshiAPIError(0, "nope")` would satisfy. The
  `RuntimeError("REACHED_NETWORK")` side effect it arms is **never triggered**.
- `test_cancel_is_deliberately_not_blocked_by_the_kill_switch` — same shape. It
  does still catch the specific regression it names (adding a client refusal to
  `cancel_order` would put the sentinel string in the message), so it is not
  fully vacuous; but it does not show that cancellation reaches the broker.

**This is a test-quality defect, not a safety hole.** My own mutation testing
(B.2) independently establishes that the three refusals are discriminating:
deleting each guard makes its named test fail, because with the guard gone the
error message becomes the RSA-key error and the `assertIn` fails. So the guards
*are* tested — but by my mutation run, not by the control the file relies on.

Fix: add `self.assertGreaterEqual(self.client.session.request.call_count, 1)`
to both controls, and give the class a client whose `_pk` is a real generated
key (or patch `_req` at the boundary) so the request layer is genuinely
reachable. Until then, `VACUOUS_TESTS_FOUND=2`.

**Controls I checked and found sound:** `MoneyPathReReadsTheKillSwitch::test_control_the_same_order_succeeds_with_the_switch_off`
(asserts `create_order.call_count == 1` against a mock client — real);
`test_plain_unittest_discovery_alone_is_not_enough` (asserts
`module_level_added > 0` — would catch the collector becoming a no-op);
`test_wrapped_functions_really_execute` (runs a wrapped failing function and
asserts it fails — real, and the direct antidote to M1 for *sync* functions);
`test_the_calibration_tests_are_in_the_built_suite` (asserts by test id, not by
count).

### M4 (MEDIUM) — `test_no_unsanctioned_direct_create_order_call` is narrower than its message claims

The inventory guard globs `pathlib.Path(_ROOT).glob("*.py")` — **root only, and
only the literal substring `.create_order(`**. It does not scan `tools/`,
`research/`, or any future package, and it would not see
`getattr(client, "create_order")(...)`, a raw
`client._req("POST", client.ORDERS_V2_PATH, ...)`, or a new
`requests.post` to the orders endpoint. Its failure message —
"direct broker-write path(s) outside the gate ladder" — reads as if the whole
repository were audited.

Today the narrowness is harmless: I verified by exhaustive repo-wide grep that
no such path exists anywhere (B.1), and the client-layer backstop means even a
missed direct caller would still be gated. But the test is the stated mechanism
by which "the inventory cannot silently regrow", and it does not cover the
places the inventory would most plausibly regrow into. Recommend
`Path(_ROOT).rglob("*.py")` with an explicit exclusion list, and adding
`ORDERS_V2_PATH` / `_req("POST"` to the searched needles.

### Existing coverage was not weakened

The delta removes exactly **five** lines from `tests/`, all from
`test_the_gate_defaults_are_loaded_before_discovery`:

```
-        src = open(os.path.join(root, "run_tests.py"), encoding="utf-8").read()
-        self.assertIn("_gates", src, ...)
-        self.assertLess(src.index("_gates"), src.index("loader.discover"), ...)
```

That was a source-position string comparison that a comment could satisfy and
that would have broken the moment discovery moved behind `_collect.collect()`.
It is replaced by **two stronger tests**: a behavioural subprocess probe (merely
importing `run_tests` must already have set both gate variables) and an
AST-parsed structural check (the `_gates` import is at module scope and precedes
`main`'s definition). Net strengthening. No test file was deleted, no assertion
weakened, no test skipped or quarantined. `skipped: 0` in the final report
confirms nothing became a silent skip.

---

## Repository hygiene

`git status` is **clean** on the review branch, with all worktrees removed.

The `.gitignore` change is load-bearing and I verified the gap it closes. At
`9b906e8`:

```
orders_state.json.bak1     NOT-IGNORED
orders_state.json.sha256   NOT-IGNORED
cycle_report.json.bak1     NOT-IGNORED
universe_cache.json.sha256 NOT-IGNORED
```

At `e2d9d41` all four are ignored, and a full suite run leaves 19 runtime-state
artifacts (`orders_state.json*`, `pending_intents.json*`,
`submission_guard.json*`, `test_report.json`, `__pycache__/`) — **every one of
them ignored**, `git status --porcelain` empty. No tracked file was newly
ignored (`git ls-files` matches none of the new patterns), so nothing dropped
out of version control.

**No secret or runtime state is committed in the delta.** The only credential-shaped
strings added are `{"KALSHI_DEMO_KEY_ID": "test", "KALSHI_DEMO_PRIVATE_KEY": "test"}`
in a test subprocess environment.

---

## Low findings

- **L1 — "PROTECTED=5, UNPROTECTED=0" is true only under an unstated definition.**
  Three of the five paths are cancels that are, by explicit design, **not**
  gated by the kill switch. "Protected" here must mean "cannot increase exposure
  past the gates", not "gated". As written it reads as "all five are gated",
  which is false and would mislead a reader auditing the breaker's coverage.
  Suggest: `SUBMISSION_PATHS=2 (both gated); CANCEL_PATHS=3 (ungated by design,
  exposure-reducing only)`.

- **L2 — the client refusal raises `KalshiAPIError(0, …)`, colliding with the
  "network timeout, order may exist" semantics.** `order_manager.py` documents
  `status=0` as "la requete est partie sans reponse exploitable" — the ambiguous
  case in which an order may have been created. A refusal that emitted *no*
  request is now indistinguishable by status code from that case. It is
  unreachable from `place_and_track` today (the OM ladder tests the identical
  three predicates first, so the client guard can never fire from there), but if
  the ladder is ever reordered, a refusal would arrive **after** the dedup lock
  and pending-intent record are written and flushed, permanently blocking that
  ticker on an intent that never resolves. `kalshi_demo_execution_check.py` also
  prints `http_status=0` for a refusal, which reads as a network failure.
  Suggest a distinct sentinel status (e.g. `-1`) or a `KalshiRefusedLocally`
  subclass.

- **L3 — "identical for absent and canonical values" is not literally true.**
  `"n"`, `"off"` and `"OFF"` changed from `True` to `False` on all three
  migrated gates (see C.2). Correct and safe in every case, but the claim needs
  the qualifier, and the `KILL_SWITCH` garbage test should include `"off"`,
  `"n"` and `"OFF"` in its table given they are the reported regression.

- **L4 — the Kelly bound holds under defaults but is not absolute.** See C.4:
  the `KELLY_MIN_BET` floor can raise the allocation above
  `min(MAX_POS_PCT, KELLY_MAX_POS_PCT)` on a small account, and the Kelly
  ceiling tracks `MAX_POSITION_PCT` upward while the legacy path stays ≤ 2 %.
  `MAX_CONTRACTS_PER_ORDER` is the real backstop.

- **L5 — `release_evidence.json` commits `"git_sha": "PENDING_COMMIT"`.** A
  versioned evidence artifact that binds to a placeholder binds to nothing.
  Related: `research/test_btc_candles.py` is a test file outside `tests/` and is
  therefore in **neither** runner's collection — the 828 is the count for
  `tests/` only. Pre-existing, not introduced here. `requirements-dev.txt` pins
  only `pytest>=8`; this review ran pytest 9.1.1, and an unpinned major is a
  drift risk for a count the LIVE gate depends on.

---

## What I did not verify

Reported as "not run" rather than inferred:

- **The value of `ALLOW_ORDER_SUBMISSION` in production.** This session's
  Railway credential returns names only. I confirmed the variable is *set* and
  that `KILL_SWITCH`, `ALLOW_FRESH_STATE`, `ALLOW_FALLBACK_CAPITAL`,
  `DAILY_RESEARCH_ORACLE_APPROVED` and `MODEL_APPROVED*` are *absent*.
- **Whether Railway actually honours `railway.json` for this service.** Requires
  a deployment; out of scope. See M2.
- **A real Docker build.** I ran `run_tests.py` and `pytest` directly on Python
  3.11.15; the image builds on 3.13-slim. Version-specific behaviour is unproven.
- **`restart_harness 17/17`.** Not exercised; it runs in no CI step and in
  neither Docker stage (the author's own open finding F4).
- **Runtime behaviour of RC-1.** It has not been built or deployed, which
  `release_evidence.json` states correctly (`"stale": true`, `"deployed": false`).
- **E2 and H1** (author's own DISCOVERED findings) are outside this delta, but I
  did independently confirm **E2 is accurate**: `create_order`'s guards are
  `ALLOW_ORDER_SUBMISSION`, `KILL_SWITCH` and the daily quarantine — none is
  keyed on `self.env == "prod"`. Nothing refuses a write *because* the
  environment is production.

---

## Merge and deploy readiness

**`RC1_MERGE_READY = YES`**, conditional on M1.

The delta does what it claims on its central points, and I reproduced the
load-bearing one — the baseline mutation survives and is killed here — myself,
on both sides. The money-path work is sound: the inventory is complete when
rebuilt independently, the choke point is genuinely unavoidable, and all five
guard mutations die against named tests. The config migration's polarity
argument is correct and the one regression it introduces is inert in the
current production environment, which I verified directly rather than accepted.
No existing coverage was weakened; five lines were removed and replaced by two
stronger tests.

M1 is the one item I would want fixed before merge. It is a one-line addition
to `_collect.collect()` plus a probe test, and it closes the only door I found
through which a green build can again describe a test that never ran — which is
the exact property Workstream A exists to establish. M3 and M4 are test-quality
work that can follow, but M3 should not be left standing long: a file whose
docstring promises anti-vacuity throughout, and whose designated control is
itself vacuous, is the kind of thing a future reviewer will reasonably trust
without re-deriving.

**`RC1_DEPLOY_READY = NO` for capital.** Independent of this delta:
`MODEL_APPROVED` is false, `model_validation.json` carries `approved:false` and
is stale (H1), no model has beaten its out-of-sample baseline, and E2 — no
environment-keyed read-only enforcement — is a genuine HIGH that must close
before any LIVE credential exists on the service. This delta neither addresses
nor worsens any of them.

**For a DEMO redeploy the delta is safe**, with M2 understood: `railway.json`
either takes effect or is ignored, and in both cases the build is no worse than
today's, because the service already builds via the Dockerfile through RAILPACK
detection. What must not happen is treating the pin as durable — it expires
2026-12-01 whatever the outcome, and no test in this repository will say so.
