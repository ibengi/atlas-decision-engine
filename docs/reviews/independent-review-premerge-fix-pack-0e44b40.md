# Independent delta review — pre-merge fix pack `e377df6..0e44b40`

Reviewer: independent session. I did not author, edit, design, test or review
this delta. Everything below was reproduced by execution in this container;
where I could not reproduce something, I say so rather than repeat the
author's claim.

| | |
|---|---|
| Scope | `e377df6..0e44b40` |
| Source branch | `claude/railway-atlas-premerge-fix-pack` |
| HEAD verified | `0e44b40f04a6f44d9c92479b98918c5d276fad1d` — matches EXPECTED_HEAD |
| Working tree at review start | clean |
| Effort | maximum; every mutation run against the full build runner |

**Sources deliberately not trusted:** the commit message, `docs/premerge-fix-pack.md`,
`docs/cancellation-operator-procedure.md`, and every count they state. Each
load-bearing claim below was re-derived.

**Prohibitions honoured.** No merge, no deploy, no Railway call of any kind, no
production credential, no broker write, no order submission or cancellation.
`MODEL_APPROVED`, `DAILY_RESEARCH_ORACLE_APPROVED` and `ALLOW_ORDER_SUBMISSION`
untouched. PR-D remains HOLD. No reviewed commit was modified; every mutation
was reverted with `git checkout --` and the tree verified clean afterwards. No
test was weakened. Nothing contacted a Kalshi endpoint — see §11 for the one
non-broker outbound attempt a required mutation produced.

---

## Environment note (a claim in the brief that is false here)

The brief states "venv at `.venv`; `source .venv/bin/activate`. Python 3.11,
pytest 9.x." **There is no `.venv` in this container.** System Python is
3.11.15 and the only `pytest` on `PATH` was a `uv` tool install in an isolated
environment that cannot import `requests`, `cryptography` or the project at
all — running it collected 36 import errors and zero tests.

I installed `pytest==9.0.2` and `cffi` into the system interpreter to obtain a
real pytest. This is an environment repair, not a change to the delta; no
repository file was touched. Anyone re-running this review must do the same or
they will mistake a broken interpreter for a broken suite.

---

## 1 · Delta scope

Twelve files, +2315 / −16.

| file | Δ | kind |
|---|---|---|
| `kalshi_client.py` | +58/−16 | **production** |
| `kalshi_alpha_bot.py` | +27 | **production** |
| `tests/_collect.py` | +77 | test infrastructure |
| `tests/_netblock.py` | +140 | test infrastructure (new) |
| `tests/test_runner_parity.py` | +210 | tests |
| `tests/test_shadow_write_layer_isolation.py` | +510 | tests (new) |
| `tests/test_cli_mode_selection_only.py` | +324 | tests (new) |
| `tests/test_broker_write_guard_pinning.py` | +362 | tests (new) |
| `tests/test_network_isolation.py` | +196 | tests (new) |
| `tests/test_prod_access_mode.py` | +60/−… | tests |
| `docs/premerge-fix-pack.md` | +234 | docs (new) |
| `docs/cancellation-operator-procedure.md` | +133 | docs (new) |

Exactly two production files changed, and both changes are confined to the
areas the brief names.

**`kalshi_client.py`** adds `READ_HTTP_METHODS`, `_normalized_http_method`,
`_is_mutating_method`, and rewires `_req` to classify with them and to
normalize `method` before signing and sending. Minimal and justified: the old
`method.upper() in MUTATING_HTTP_METHODS` genuinely mis-classified `bytes`.

One consequence the delta's own documentation does not mention, and which I
checked separately because it touches the signature: `_sign_headers(method,
url)` now receives the **normalized** verb. Previously `_req` signed with the
raw `method` while sending `method.upper()`. For `str` these agreed after
`_sign_headers`' internal `.upper()`; for `bytes` they did not — the signed
message would have interpolated `b'POST'`, producing a signature over a
different string than the request line. The change silently repairs that too.
It is strictly an improvement and I found no regression in it.

**`kalshi_alpha_bot.py`** adds one block in the `READ_ONLY` arm of `main()`
that refuses startup when `LIVE_BROKER_WRITES_AUTHORIZED` resolves true. It
adds no other statement and rewrites no variable. Minimal and justified.

**No production file outside those two was touched**, and no scientific gate,
gatekeeper criterion, order-gate default or Railway variable was changed.
I re-read `model_gatekeeper.py` and confirmed `check_live_allowed()` is
untouched by this delta.

```
DELTA_SCOPE_CLEAN=YES
UNRELATED_PRODUCTION_CHANGES=0
```

---

## 2 · FIX 1 — false-green collector

### The premise, reproduced on the base commit

I extracted `e377df6` to a clean directory (`git archive`, so no reviewed
commit was touched), dropped in a deliberately failing module-level
`async def` test, and ran both runners:

| runner on `e377df6` + failing async probe | result |
|---|---|
| `pytest tests/ -q` | **`1 failed, 883 passed, 186 subtests passed`** |
| `python run_tests.py` | **`RC=0`, `"ran": 884, "failures": 0, "errors": 0`** |

The defect is real and I reproduced it independently: a green
`test_report.json` — the artifact `model_gatekeeper.check_live_allowed()`
reads — for a test whose body never executed.

(The fix-pack document states "1 failed, **828** passed" for the pytest side.
The measured figure is 883. Cosmetic, but it is a stated number that is wrong;
see §12.)

### The four required cases at HEAD

| case | real pytest | `python run_tests.py` |
|---|---|---|
| (a) module-level failing `async def` | `1 failed` | `RC=1`, refuses collection, **report byte-identical (not overwritten)** |
| (b) module-level generator (`yield`) | collection **error** | `RC=1`, refuses collection, **report not overwritten** |
| (c) `functools.wraps`-wrapped `async def` | `1 failed` | `RC=1`, `"failures": 1` — report written but **red** |
| (d) test returning a plain `42` | `1 passed` + `PytestReturnNotNoneWarning` | **passes** |

For (c) I first confirmed the premise the fix rests on:
`inspect.iscoroutinefunction()` is `False` and `_collect._unsupported_kind()`
returns `None`, so static inspection genuinely cannot see it; the runtime
backstop is what fails it. For (d) the collector agrees with pytest rather
than being stricter — parity holds in both directions.

### Is the collector stricter than pytest anywhere?

Yes, in one deliberate place: a module-level `test_*` that **declares
parameters** (a pytest fixture test) is put in `uncollectable`, and
`run_tests.py` then exits 1 for the whole build. pytest would run it. This is
a fail-closed divergence — the build refuses rather than under-reports — and
it is documented as intentional. I record it as designed behaviour, not a
defect, but a contributor who legitimately adds a fixture-based test will
break the build and should know why.

### Mutations (full build runner)

| mutation | result |
|---|---|
| `_UNSUPPORTED_KINDS = ()` (remove static async/generator rejection) | **KILLED** — `RC=1`, 5 failures |
| `_unexecuted_body()` returns `None` immediately (neuter runtime backstop) | **KILLED** — `RC=1`, 1 failure (`test_a_decorated_async_test_fails_rather_than_passing`) |

### Residual hole I found — see MEDIUM-1 in §12

An `async def` test **method inside a `unittest.TestCase` subclass** is still
counted green. This is outside what the delta claims to fix, and pytest agrees
with the build here, but it does violate the invariant as the brief words it.
Details and the minimal patch are in §12.

```
ASYNC_FALSE_GREEN_CLOSED=YES  (module-level functions; see MEDIUM-1 for TestCase methods)
GENERATOR_FALSE_GREEN_CLOSED=YES
PYTEST_PARITY=YES  (949 == 949, discovered; non-None return agrees; one deliberate fail-closed divergence on parameterised tests)
```

---

## 3 · FIX 2 — shadow executable proof

I did not read the test's assertions and believe them. I imported its harness,
drove `_execute_decision` myself, and printed the boundary state:

| mode | `placed` | report | `place_and_track` | `create_order` | `cancel_order` | mutating HTTP | all HTTP |
|---|---|---|---|---|---|---|---|
| `READ_ONLY` | 0 | `risk_passed=1`, `would_submit=1`, `rejections={'prod_read_only':1}` | 0 | 0 | 0 | 0 | **`[]`** |
| `CAPITAL` | 0¹ | `risk_passed=1`, `orders_submitted=1` | **1** | **1** | 1 | **2** | `POST /portfolio/events/orders`, 16×`GET`, `DELETE …/test-order-1`, `GET /portfolio/fills` |
| `CAPITOL` (typo) | 0 | `would_submit=1`, `prod_read_only=1` | 0 | 0 | 0 | 0 | `[]` |

¹ the CAPITAL order times out and is cancelled by the TTL path, so `placed`
ends at 0 — but it reached `create_order` and emitted a real `POST`, which is
the only thing the control needs to establish.

The `READ_ONLY` row satisfies every part of the brief: the decision genuinely
runs (it reaches `risk_passed` and sizing, so the zeros are not an early
refusal), `would_submit` telemetry is produced, and the write layer is not
entered at any of the three depths. `http == []` is stronger than
"no mutating HTTP" — nothing at all was attempted.

**The CAPITAL control is not vacuous.** It drives every recorder the read-only
assertions rely on above zero, including a `POST` at the transport, under an
otherwise identical run. If the control ever stopped reaching `create_order`
the test asserts `len(create_order) == 1` and would fail rather than pass
quietly — I verified that assertion exists and is not a `assertGreaterEqual(…,
0)`-style no-op. `test_CONTROL_the_boundary_recorder_reports_zero_only_when_true`
additionally proves the recorder is not a constant zero and does not count
`GET` as a mutation.

**Real objects, not agreeable mocks.** `om = order_manager.OrderManager(client,
…)` is the real class; `client.create_order` / `cancel_order` are the real
bound methods wrapped by recorders; only `_req` is replaced, one layer *below*
the guard. The `CAPITAL` run producing a genuine `POST /portfolio/events/orders`
through that real `create_order` is the proof the objects are real.

### Mutations of the read-only branch (full build runner)

| mutation in `execution_engine._execute_decision` | result |
|---|---|
| delete the branch (`if False:`) | **KILLED** — 9 failures + 2 errors |
| invert it (`not prod_is_read_only()`) | **KILLED** — 10 failures |
| `self.client.env != "demo"` → `== "prod"` | **KILLED** — 6 failures (all six `env=` subtests) |

```
SHADOW_EXECUTABLE_PROOF=YES
CAPITAL_CONTROL_NON_VACUOUS=YES
```

---

## 4 · FIX 3 — CLI mode selection only

The tests do not rest on exit codes. `_assert_nothing_armed` reads
`os.environ` after the real `main()` has run, over seven authorization
variables; `_refusal_log` additionally asserts on the specific log line; and
`test_CONTROL_the_inspection_really_observes_variables_being_set` proves the
same read *can* see a variable the entrypoint did set (`PROD_ACCESS_MODE`), so
"nothing was armed" is not blindness. `test_CONTROL_every_authorization_var_is_actually_checked`
proves each of the seven names can trip the helper on its own. This is exactly
the "passes for the wrong reason" trap the brief warns about, and the file
closes it.

I confirmed by execution that `--live-read-only` sets `PROD_ACCESS_MODE=READ_ONLY`
and `--live-capital` sets `CAPITAL`, and that neither sets any authorization
variable.

### Mutations (full build runner)

| mutation: `--live-capital` additionally exports | result |
|---|---|
| `LIVE_TRADING=true` | **KILLED** — 2 failures |
| `LIVE_TRADING_CONFIRMED=true` | **KILLED** — 2 failures |
| `LIVE_BROKER_WRITES_AUTHORIZED=true` | **KILLED** — 2 failures |
| `MODEL_APPROVED=true` | **KILLED** — 2 failures |

```
CLI_MODE_SELECTION_ONLY=YES
CLI_AUTO_AUTHORIZATION=NO
```

---

## 5 · RC-2 medium fixes — method normalization and guard pinning

I built my own client (real `KalshiClient`, counting session that records and
then raises) under `PROD_ACCESS_MODE=READ_ONLY`, `env="prod"`, and pushed 23
method values through `_req`:

| method | outcome | sends |
|---|---|---|
| `b"POST"`, `b"DELETE"`, `bytearray(b"PUT")`, `b"post"` | `BrokerWriteForbidden` | `[]` |
| `"post"`, `"Post"`, `"PoSt"`, `"  post  "`, `"\tPOST\n"` | `BrokerWriteForbidden` | `[]` |
| `""`, `"   "` | `BrokerWriteForbidden` | `[]` |
| `b"\xff\xfe"` (non-ASCII bytes) | `BrokerWriteForbidden` | `[]` |
| object whose `.upper()` returns `"GET"` | `BrokerWriteForbidden` | `[]` |
| `None`, `42`, `3.5`, `object()`, `["POST"]` | `BrokerWriteForbidden` | `[]` |
| `"GET\r\nX-Inject: 1"`, `"POST /x"` | `BrokerWriteForbidden` | `[]` |
| `"TRACE"`, `"CONNECT"` | `BrokerWriteForbidden` | `[]` |

Everything unclassifiable fails **closed**, and in every refusal zero network
sends were attempted — "refused" and "sent but failed" are not conflated.

**Reads still work** (a guard that refuses everything is not a fix):
`"GET"`, `"get"`, `" Get "`, `b"GET"`, `"HEAD"`, `"OPTIONS"` all reach the
transport, normalized to the canonical upper-case string.

One adversarial case worth recording, which I found and which is **not** an
escape: a `str` **subclass** that overrides both `strip()` and `upper()` to
return `"GET"` is classified as a read *and is then sent as `GET`*. The
classified value and the wire value are the same object — which is precisely
the property MEDIUM-1 was about. Such a class can only downgrade its own write
to a read; it cannot smuggle a `POST` past the guard.

### Guard-weakening mutations re-run (full build runner)

| # | mutation | result |
|---|---|---|
| 1 | `if self.env == "demo": return` → `if self.env != "prod": return` | **KILLED** — 8 errors |
| 2 | drop `.upper()` in `_normalized_http_method` | **KILLED** — 1 error |
| 3 | `getattr(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True)` (fail-open) | **KILLED** — 1 error |
| 4 | `if "demo" in self.base_url: return` (url as trust anchor) | **KILLED** — 5 errors |
| 5 | revert MEDIUM-1: `str(method).upper() in MUTATING_HTTP_METHODS` | **KILLED** — 6 failures + 5 errors |

5/5 killed. Mutation 2 is killed by exactly one test — the read control
(`test_CONTROL_a_lowercase_read_is_normalized_not_refused`) — because with
`.upper()` gone, lowercase *writes* still fail closed and only lowercase
*reads* change behaviour. The pin is real but narrow; it depends on that one
control continuing to exist. I record it as an observation, not a finding.

```
METHOD_NORMALIZATION_SAFE=YES
RC2_PINNING_COMPLETE=YES  (5/5 killed, including the MEDIUM-1 revert)
```

---

## 6 · READ_ONLY with an armed write authorization

I ran the real entrypoint as a subprocess, 25 times, each with the network
block installed, an isolated `DATA_DIR`, and **dummy production credentials
deliberately exported in the parent** (`KALSHI_KEY_ID=DUMMY-PARENT-KEY-ID-NOT-REAL`,
a dummy PEM). I did not assert on the exit code — every one of these exits 1 —
but on **which** refusal fired.

| `LIVE_BROKER_WRITES_AUTHORIZED` | armed refusal? | credential gate reached? | READ_ONLY banner? | outbound attempts | key reached child |
|---|---|---|---|---|---|
| `true`, `TRUE`, `1`, `yes`, `y`, `on`, `"  true  "`, `YES`, `On` | **yes** | no | no | 0 | no |
| `false`, `0`, `no`, `n`, `off`, `non`, `""`, `"   "` | no | yes | yes | 0 | no |
| `ture`, `maybe` (unreadable) | no | yes | yes | 0 | no |
| absent | no | yes | yes | 0 | no |

The armed rows refuse **specifically**: the log carries
`PROD_ACCESS_MODE=READ_ONLY avec LIVE_BROKER_WRITES_AUTHORIZED deja ARMEE:
etat contradictoire …`, the credential-gate message `identifiants invalides`
is **absent**, and the `PRODUCTION EN LECTURE SEULE` banner never prints —
so startup did not proceed past the mode block. The non-armed rows reach the
credential gate and print the banner, which makes the control non-vacuous: the
refusal is about the armed flag, not a blanket refusal.

Both routes are covered: the `--live-read-only` CLI flag and a bare
`PROD_ACCESS_MODE=READ_ONLY` environment variable both trigger it. `CAPITAL`
with the same armed authorization correctly does **not** hit this refusal.

Unreadable values (`ture`, `maybe`) are not treated as armed. That is
consistent rather than a gap: `_env_gate(..., on_invalid=False)` reads them as
false everywhere, so a later `CAPITAL` start would find the authorization
false too and refuse the write at the client. Nothing is inherited.

**The variable is not rewritten.** Running `main()` in-process:
`before='true'`, `after='true'`, `UNCHANGED=True`, exit 1.

### Mutations (full build runner)

| mutation | result |
|---|---|
| delete the refusal (`if False:`) | **KILLED** — 7 failures |
| clear `LIVE_BROKER_WRITES_AUTHORIZED` instead of refusing | **KILLED** — 8 failures |
| ignore the live env value (`armed = False`, keep only the frozen `CFG`) | **KILLED** — 7 failures |

```
READ_ONLY_ARMED_WRITE_AUTH_REFUSED=YES
```

---

## 7 · Test network isolation

### The block is real and is the module actually loaded

```
SITECUSTOMIZE_FILE = /tmp/nb-…/sitecustomize.py      (the generated one, not a shadow)
socket.getaddrinfo        -> _blocked_getaddrinfo
socket.socket.connect     -> _blocked_connect
socket.create_connection  -> _blocked_create_connection
```

### Every outbound path I could think of, in a netblocked child

| probe | rc | raised `NetworkBlockedInTests` | recorded | SyntaxError |
|---|---|---|---|---|
| `socket.getaddrinfo('api.elections.kalshi.com',443)` | 1 | yes | 1 | no |
| `socket.create_connection(('1.1.1.1',443))` | 1 | yes | 1 | no |
| raw `socket().connect(('1.1.1.1',443))` | 1 | yes | 1 | no |
| `requests.get('https://api.elections.kalshi.com/…')` | 1 | yes | 1 | no |
| `requests.get('https://example.com')` (proxy path) | 1 | yes | 1 | no |
| `urllib.request.urlopen(...)` | 1 | yes | 1 | no |
| `http.client.HTTPSConnection(...)` | 1 | yes | 1 | no |
| `socket().connect_ex(('1.1.1.1',443))` | 0 | no (stub returns `1`) | **0** | no |

I checked the SyntaxError trap the brief names explicitly: **no probe failed
for a syntax error**, and in the entrypoint runs of §6 the child got all the
way to the credential gate, which is only reachable by executing real
application code. The children fail for the block, not for being broken.

`connect_ex` is stubbed to return `1` without connecting — safe, but it is the
one path that is **not recorded**, so an attempt through it would be invisible
to `attempts()`. Nothing in `requests`/`urllib3`'s request path uses it. LOW-1
in §12.

### Credentials

`prod_credentials_config()` reads only `CFG.KEY_ID`/`CFG.PRIV_KEY`, which come
only from `KALSHI_KEY_ID`/`KALSHI_PRIVATE_KEY`. Both are in `STRIPPED_ENV`.
With dummy values exported in the parent across all 25 entrypoint runs in §6,
`KALSHI_KEY_ID` was **absent from the child env every time** and outbound
attempts were **0 every time**.

### Mutations (full build runner)

| mutation in `tests/_netblock.py` | result |
|---|---|
| remove `socket.getaddrinfo` block | **KILLED** — 2 failures |
| remove `socket.socket.connect` block | **KILLED** — 1 failure |
| remove `KALSHI_KEY_ID`/`KALSHI_PRIVATE_KEY` from `STRIPPED_ENV` | **KILLED** — 2 failures |

```
TESTS_CANNOT_CONTACT_REAL_PROD=YES
```

---

## 8 · Cancellation operator procedure

Checked against the code, not the prose.

| documented row | code | verdict |
|---|---|---|
| `demo` → cancel allowed | `_assert_broker_write_allowed` returns early on `env == "demo"` | **accurate** |
| `prod`, mode unset/blank/misspelled → refused | `prod_access_mode()` returns `None` for anything not exactly `READ_ONLY`/`CAPITAL`; `prod_is_read_only()` is then true | **accurate** |
| `prod` + `READ_ONLY` + auth true → refused | read-only branch precedes the authorization branch in `_assert_broker_write_allowed`; verified behaviourally in §5 | **accurate** |
| `prod` + `CAPITAL` + auth false/unset/unreadable → refused | `if not CFG.LIVE_BROKER_WRITES_AUTHORIZED` with `_env_gate(on_invalid=False)` | **accurate** |
| `prod` + `CAPITAL` + auth true → allowed | reaches transport | **accurate** |
| `KILL_SWITCH` blocks `create_order`, not `cancel_order` | `CFG.KILL_SWITCH` is checked at `kalshi_client.py:407` inside `create_order` only; `cancel_order` (line 475) calls only `_assert_broker_write_allowed` | **accurate** |
| `KILL_SWITCH` fails closed on an unreadable value | `_env_gate("KILL_SWITCH", default=False, on_invalid=True)` | **accurate** |

Every log marker the runbook tells an operator to look for exists in the code:
`[RECOVERY_READ_ONLY]` (`order_manager.py:1076`), `[RECONCILE_VERIFY]`
(`position_manager.py`), `[CANCEL_V2_CONFIRMED]` (`kalshi_client.py:495`),
`ALLOW_ORDER_SUBMISSION=false` (`order_manager.py`, `execution_engine.py`),
`reduced_by` proof (`order_manager.py:949-966`).

§3's claim that both recovery call sites sit inside `except KalshiAPIError` is
correct: `place_and_track`'s TTL cancel and `reconcile_startup`'s cancel are
both inside such a handler, and `BrokerWriteForbidden` subclasses
`KalshiAPIError`, so a refused cancellation is handled fail-closed and the
order stays in `orders_state.json`. The stranded-order warning that follows
from this is stated plainly and is correct.

**No instruction in the document bypasses an access control.** §6 tells the
operator to *set* `LIVE_BROKER_WRITES_AUTHORIZED=true` and start in `CAPITAL`
with the confirmations that mode already requires — that is using the control
as designed, and it explicitly notes selecting the mode grants none of the
other gates. §4's emergency procedure only closes gates. §7 explicitly refuses
to automate away the contradictory-state clearing.

```
CANCELLATION_PROCEDURE_ACCURATE=YES
```

---

## 9 · Full test truth

| run | result |
|---|---|
| `python run_tests.py` (1st) | `ran 949, failures 0, errors 0, skipped 0` — `OK`, RC 0 |
| `python run_tests.py` (2nd) | `ran 949, failures 0, errors 0, skipped 0` — identical |
| `pytest tests/ -q` (1st) | `949 passed, 228 subtests passed` |
| `pytest tests/ -q` (2nd) | `949 passed, 228 subtests passed` — identical |
| `tools/restart_harness.py` | `TOTAL 17/17 checks passed`, RC 0 |

**949 == 949.** The count is **discovered, not hard-coded**: I grepped the
tree for `949`, `944`, `799`, `794`, `884`, `947` and found them only in
comments and docstrings — never in an assertion. `test_runner_parity.py`
shells out to the real `pytest` and compares totals, skipping if pytest is
genuinely absent, rather than comparing `_collect.py` against a mirror of
itself.

**No test-isolation defect found.** Both suites were run twice; results were
byte-identical, and `git status` was clean before and after every run.
`test_report.json` is `.gitignore`d and, importantly, also excluded in
`.dockerignore` with a comment explaining why — so a developer's stale report
cannot enter an image.

```
UNITTEST=949 passed / 0 failures / 0 errors
PYTEST=949 passed (+228 subtests)
COUNTS_MATCH=YES (discovered, not hard-coded)
RESTART_HARNESS=PASS 17/17
```

---

## 10 · Mutation spot check

Every mutation in this review was run against the **full** build runner
(`python run_tests.py`), not a targeted file. Twenty in total.

| area | representative mutation | verdict |
|---|---|---|
| collector | `_UNSUPPORTED_KINDS = ()` | KILLED (5 failures) |
| collector | neuter runtime backstop | KILLED (1 failure) |
| shadow path | delete the read-only branch | KILLED (9F + 2E) |
| shadow path | invert the read-only branch | KILLED (10 failures) |
| shadow path | `!= "demo"` → `== "prod"` | KILLED (6 failures) |
| CLI authorization | `--live-capital` arms `LIVE_TRADING` | KILLED (2 failures) |
| CLI authorization | …arms `LIVE_TRADING_CONFIRMED` / `LIVE_BROKER_WRITES_AUTHORIZED` / `MODEL_APPROVED` | KILLED (2 failures each) |
| READ_ONLY dominance | guard polarity `env != "prod"` | KILLED (8 errors) |
| READ_ONLY dominance | url as trust anchor | KILLED (5 errors) |
| READ_ONLY dominance | `getattr(..., True)` fail-open | KILLED (1 error) |
| READ_ONLY dominance | drop `.upper()` | KILLED (1 error) |
| READ_ONLY dominance | revert MEDIUM-1 bytes escape | KILLED (6F + 5E) |
| armed-write refusal | delete / clear-instead-of-refuse / ignore live env | KILLED (7, 8, 7 failures) |
| network isolation | remove DNS block | KILLED (2 failures) |
| network isolation | remove raw-connect block | KILLED (1 failure) |
| network isolation | remove credential stripping | KILLED (2 failures) |

**`MUTATION_SPOTCHECK=20/20 KILLED`. No mutation survived.**

---

## 11 · Safety of the review itself

Every run in this review used mocks, fakes, or the network block. Across all
entrypoint subprocesses, recorded outbound attempts were **0**. No Kalshi
hostname was resolved or connected to at any point.

One disclosure. The brief required me to mutate the raw-connect block out of
`tests/_netblock.py`. With that layer removed, `test_a_child_cannot_connect_a_raw_socket_to_an_ip`
attempts a TCP connect to the hard-coded literal `1.1.1.1:443` — a public DNS
resolver, **not a broker endpoint** and carrying no credential, request body or
account reference. The test failed as intended and the mutation was reverted.
I judged running the mutation the brief asked for preferable to weakening it,
but I am recording the attempt rather than leaving it implicit.

The `pytest`/`cffi` installation described in the environment note is the only
change I made outside the repository, and it changed no repository file.

---

## 12 · Findings

### BLOCKERS — none

### HIGH — none

### MEDIUM-1 · A false-green survives for an `async def` test method inside a `TestCase`

**Where:** `tests/_collect.py` (`collect()` inspects module-level functions only).

**Reproduced.** With this probe in `tests/`:

```python
import unittest
import _bootstrap  # noqa: F401

class AsyncInsideTestCase(unittest.TestCase):
    async def test_probe_e_async_method(self):
        raise AssertionError('probe E body ran')
```

| runner | result |
|---|---|
| `pytest` | `1 passed, 2 warnings` |
| `python run_tests.py` | **`RC=0`, `"ran": 950, "failures": 0, "errors": 0`** — a green `test_report.json` written for a test whose body never ran |

**Why it is MEDIUM and not HIGH.** It is not a regression: the delta neither
introduced nor claims to cover this path — `_collect.py` and
`docs/premerge-fix-pack.md` both scope the fix to *module-level* `async def`
functions, so this is a pre-existing gap the delta leaves where it found it.
pytest reports green here too, so it is **not** a runner-parity break and
`COUNTS_MATCH` is unaffected. And no `async def` exists anywhere in the suite
today — I grepped; the only occurrences are the parity test's own synthetic
probes. The hole is latent, requiring someone to write a new malformed test.

**Why it still matters.** It is the same failure mode FIX 1 exists to close —
a green artifact for an unexecuted body, in the file
`model_gatekeeper.check_live_allowed()` reads — reached through a third door.
The invariant as the brief words it ("`run_tests.py` must NEVER report green
for a test whose body did not run") is not literally true at `0e44b40`.

**Minimal patch — NOT applied.** In `tests/_collect.py`, after building the
suite in `collect()`, walk it and refuse unsupported method kinds, exempting
`IsolatedAsyncioTestCase`, which can legitimately run them:

```python
def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item

# inside collect(), after `suite = loader.discover(start_dir)`:
for test in _iter_tests(suite):
    if isinstance(test, unittest.IsolatedAsyncioTestCase):
        continue                      # this one really does run coroutines
    method = getattr(type(test), getattr(test, "_testMethodName", ""), None)
    reason = _unsupported_kind(method) if method is not None else None
    if reason is not None:
        uncollectable.append(f"{test.id()} ({reason})")
```

`uncollectable` already makes `run_tests.py` exit 1 without writing a report,
and `test_nothing_is_collectable_only_by_pytest` already asserts it is empty,
so no other machinery is needed. A behavioural test mirroring
`test_the_build_runner_refuses_and_writes_no_green_report`, but with the probe
written as a `TestCase` method, would pin it.

### LOW-1 · `connect_ex` attempts are silently unrecorded

**Where:** `tests/_netblock.py`, `SITECUSTOMIZE_SOURCE`:
`socket.socket.connect_ex = lambda self, address: 1`.

No connection is made, so isolation holds — but unlike the other three hooks
this one does not call `_record()`, so an attempt through `connect_ex` would
not appear in `attempts()` and the `attempts() == []` assertion in
`StartupMatrix._start` would pass over it. Nothing in `requests`/`urllib3`'s
request path uses `connect_ex`; this is an observability gap, not an isolation
gap.

**Minimal patch — NOT applied:**

```python
def _blocked_connect_ex(self, address):
    _record(("connect_ex",) + tuple(address if isinstance(address, tuple)
                                    else (address,)))
    return 1

socket.socket.connect_ex = _blocked_connect_ex
```

### LOW-2 · `_start` never asserts the block was installed in *that* child

`StartupMatrix._start` asserts `attempts(network_log) == []`. That is satisfied
both by "block installed and nothing was tried" and by "block silently failed
to install and nothing was tried". `site.execsitecustomize()` swallows a
non-`ImportError` failure inside `sitecustomize` with only a stderr warning, so
a future edit that made the generated source raise would leave children
unguarded without failing a test. `tests/test_network_isolation.py` proves the
block works, but in its *own* children.

Credential stripping — the deterministic layer — is structural and unaffected,
so this is hardening, not a live hole.

**Minimal patch — NOT applied:** have the child print a marker
(`sitecustomize` sets `os.environ["ATLAS_NETBLOCK_INSTALLED"] = "1"`, or write
a sentinel line into the log file at import) and assert it in `_start`.

### LOW-3 · A stated number in `docs/premerge-fix-pack.md` is wrong

The FIX 1 table records pytest on `e377df6` as `1 failed, **828** passed`. The
measured value is `1 failed, **883** passed, 186 subtests passed`. The
`run_tests.py` side of the same table (`EXIT=0`, `ran:884 failures:0`) matches
my reproduction exactly. Cosmetic; the argument is unaffected.

### Observations (no action required)

- The collector is **stricter** than pytest for module-level tests that declare
  parameters: the whole build exits 1 rather than under-report. Deliberate and
  documented, but a contributor adding a legitimate fixture-based test will hit
  it.
- RC-2 mutation 2 (drop `.upper()`) is killed by exactly one test — the read
  control. Writes still fail closed under that mutation, so the pin is narrow
  by nature.
- A `str` subclass that lies in both `strip()` and `upper()` is classified and
  sent as the same value; it can only downgrade its own write to a read.
- Refusing to write a report leaves any prior green `test_report.json` on disk,
  and `check_live_allowed()` only checks presence, age < 7 days and
  `failures`/`errors` — so a local operator could read a stale green report
  after a refused run. **The Docker path is not affected**: `.dockerignore`
  excludes `test_report.json` from the build context (with a comment saying
  exactly why), and `RUN python run_tests.py` aborts the build on refusal, so
  no image can carry one.

---

## 13 · Verdict

```
BLOCKERS=0
HIGH_FINDINGS=0
MEDIUM_FINDINGS=1   (MEDIUM-1: async TestCase method still reports green)

MANDATORY_FIX_1=PASS   (false-green collector; both mutations killed; base-commit defect reproduced and closed)
MANDATORY_FIX_2=PASS   (shadow executable proof; control non-vacuous; three mutations killed)
MANDATORY_FIX_3=PASS   (CLI mode selection only; four mutations killed)

RC2_MEDIUM_FIXES=PASS  (5/5 mutations killed; fail-closed on every unclassifiable method; reads unaffected)
RC3_MED2=PASS          (READ_ONLY + armed write auth refuses specifically; variable not rewritten; three mutations killed)
RC3_MED3=PASS          (zero outbound attempts of any kind; credentials stripped; three mutations killed)
CANCELLATION_PROCEDURE=PASS  (matrix matches the code; no instruction bypasses a control)

UNITTEST=949 passed / 0 failures / 0 errors  (twice, identical)
PYTEST=949 passed (+228 subtests)            (twice, identical)
COUNTS_MATCH=YES
RESTART_HARNESS=PASS 17/17
MUTATION_SPOTCHECK=20/20 KILLED
PROD_CREDENTIAL_SAFETY=PASS

MERGE_READY=YES
DEPLOY_READY=NO
```

**`MERGE_READY=YES`** because there are no blockers, no HIGH findings, all
three mandatory fixes pass under independent reproduction *and* mutation, the
RC-2/RC-3 items pass, production credential safety passes, and the delta is a
strict improvement over `e377df6` on every axis I could test.

MEDIUM-1 is recorded, not waived. I judged it MEDIUM rather than HIGH because
it is pre-existing, outside the delta's stated scope, shared with pytest (so
not a parity break), and unreachable by any test in the suite today. **A
reviewer who reads the false-green invariant as absolute should treat it as
HIGH and flip this verdict to NO.** I am naming that boundary explicitly
rather than deciding it silently.

**`DEPLOY_READY=NO`**, and nothing in this delta moves that. `MODEL_APPROVED`
is false, `DAILY_RESEARCH_ORACLE_APPROVED` is false, `ALLOW_ORDER_SUBMISSION`
is false, no production credential exists, and LIVE reconciliation has never
run against a real account. PR-D remains HOLD.

---

## 14 · What I did NOT verify

Stated plainly, because a review's silence is not evidence.

1. **Real broker behaviour.** Nothing in this review contacted Kalshi. Every
   `_req` was a recorder or a raising fake. That the guards refuse a write is
   proved; that a *permitted* write would be accepted by Kalshi is not, and
   cannot be from here.
2. **The signature repair end to end.** I reasoned about `_sign_headers` now
   receiving the normalized verb and read both code paths, but I did not verify
   a real `KALSHI-ACCESS-SIGNATURE` against a live endpoint. No credential
   exists to do so.
3. **The historical mutant labels.** The fix-pack document cites mutants
   "A2", "A7"–"A10" as having passed the old suite. I re-derived the
   *equivalent* mutations at HEAD and killed them, but I did not check out the
   old suite and replay the author's numbered mutants, so I cannot confirm the
   "passes the entire old suite (34/34)" claim as stated.
4. **The Docker build.** I did not build the image. My conclusions about
   `.dockerignore`, the `tests` stage and the `COPY --from=tests` are read from
   the `Dockerfile` and `.dockerignore`, not executed.
5. **Behaviour under Python other than 3.11.15.** The `async`-method false-green
   in MEDIUM-1 depends on `unittest`'s handling of a non-`None` test return,
   which is deprecated and whose behaviour I believe changes in a later
   CPython. **I am not certain which version turns it into an error**; anyone
   relying on that should verify against current CPython release notes rather
   than on this sentence.
6. **Concurrency and long-running behaviour.** No load, no soak, no
   multi-cycle run. Nothing here says anything about the engine over hours.
7. **Everything outside the delta.** Roughly 900 of the 949 tests were run but
   not reviewed. I audited the twelve changed files and the production code
   they touch, not the rest of the system.
8. **Non-`_start` subprocess tests.** The network block covers
   `StartupMatrix._start` and `tests/test_network_isolation.py`. I did not
   audit every other test in the suite for subprocess or socket use, so
   "the suite cannot reach production" is established for the paths the delta
   addresses and for the paths I probed, not proved exhaustively for all 949.
