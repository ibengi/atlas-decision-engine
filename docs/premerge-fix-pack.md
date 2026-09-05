# Pre-merge fix pack — RC-1 + RC-2 + RC-3

Base: `e377df6` (RC-3), which sits on `a16bebe` (RC-2), `e2d9d41` (RC-1) and
`826d12e`, on top of `main` at `9b906e8`.

This branch closes the items the three independent Opus reviews raised as
conditions of merge, plus the two they marked "required before production
credentials exist". It merges nothing, deploys nothing, provisions no
credential, and changes no scientific or model gate.

---

## What was wrong, and what each fix establishes

### FIX 1 — RC-1 M1 (HIGH): a false-green build report

`tests/_collect.py` wrapped a module-level `async def test_*` into a
`TestCase` and counted it. Calling it built a coroutine that was never
awaited, so the body never ran and the test "passed".

Reproduced on `e377df6` before touching anything, with a deliberately failing
async calibration-style test in `tests/`:

| runner | result |
|---|---|
| `pytest` | `1 failed, 828 passed` |
| `run_tests.py` | `EXIT=0`, `ran:884 failures:0`, `OK` |

`test_report.json` is what `model_gatekeeper.check_live_allowed()` reads
before permitting LIVE. A green report describing a test that never executed
is precisely the failure mode RC-1 exists to close, reopened through another
door.

Closed in two layers, because one is not enough:

* **Statically**, `collect()` refuses coroutine, generator and
  async-generator functions into `uncollectable`. `run_tests.py` then exits 1
  and writes **no report at all** rather than a green one.
* **At runtime**, the wrapper fails any test whose call returns an un-awaited
  coroutine or generator. Static inspection cannot see this case: a
  `functools.wraps` decorator around an `async def` reports
  `iscoroutinefunction() is False` while still returning a coroutine.

Parity with pytest was checked in both directions rather than assumed. Real
pytest **fails** the decorated-async case, so failing it here is parity; real
pytest only **warns** on a plain non-None return, so that case still passes —
this collector's job is to agree with pytest, not to be stricter than it.

After the fix, with the failing async probe still present:

```
CRITICAL: tests collectable only by pytest -- test_report.json would
under-report the suite the LIVE gate relies on:
  - tests.test_zz_m1_probe.test_async_calibration_style_probe
    (async def; needs an event loop, pytest-only)
EXIT=1     (no report written)
```

**`ASYNC_SILENT_PASS_CLOSED=YES` · `GENERATOR_SILENT_PASS_CLOSED=YES`**

### FIX 2 — RC-3 HIGH-1: the shadow claim was defended by reading, not running

`SHADOW_INVOKES_WRITE_LAYER=NO` rested on assertions over the *source text* of
`_execute_decision` — that `prod_is_read_only()` appears before
`place_and_track`. A test that reads the program cannot fail for the reason
the program is wrong.

`tests/test_shadow_write_layer_isolation.py` executes the real
`_execute_decision` against a **real** `OrderManager` and a **real**
`KalshiClient`, with the write boundary instrumented at three depths
(`place_and_track`, `create_order`, `_req` split into mutating and read
verbs). Under `PROD_ACCESS_MODE=READ_ONLY` the decision runs to completion —
book, risk gates, sizing, `risk_passed=1`, `would_submit=1` — with all three
recorders at zero.

The decisive evidence that this is not the old test in new clothing: mutant
**A2**, which inverts the mode test, **passes the entire old suite (34/34)**
and is killed by the new one.

**Anti-vacuity control**: an identical run differing only in the access mode
(`CAPITAL`) must drive the same recorders above zero, including a `POST` at
the transport. Without that, every zero above would be satisfied by a broken
harness. The same shape covers reconciliation: read-only recovery attempts no
cancellation, and the `CAPITAL` control proves the cancel path is reachable.

**`SHADOW_EXECUTABLE_PROOF=YES` · `CAPITAL_CONTROL_NON_VACUOUS=YES`**

### FIX 3 — RC-3 HIGH-2: a mode flag could have granted an authorization

Nothing forbade `--live-capital` from also exporting `LIVE_TRADING=1`. The
separation between *selecting* capital mode and *being authorized* to trade
rested on nobody having written that line yet.

`tests/test_cli_mode_selection_only.py` runs the real `main()` with real argv
in a scrubbed environment, then inspects the environment directly. Neither
flag may set `LIVE_TRADING`, `LIVE_TRADING_CONFIRMED`,
`LIVE_BROKER_WRITES_AUTHORIZED`, `MODEL_APPROVED`, `MODEL_APPROVED_FOR_LIVE`,
`ALLOW_ORDER_SUBMISSION` or `DAILY_RESEARCH_ORACLE_APPROVED`.

Mutants **A7** and **A8** (and two more spellings) each **pass the entire old
suite** and are killed here.

Behaviour was already correct; only the test was missing. Nothing in the
entrypoint changed for this fix.

**`CLI_MODE_SELECTION_ONLY=YES` · `CLI_AUTO_AUTHORIZATION=NO`**

### FIX 4 — RC-2 MEDIUM-1: a bytes verb escaped the transport backstop

`method.upper() in MUTATING_HTTP_METHODS` classifies `b"POST"` as
`b"POST"`, which is not in a set of *strings*. The request was therefore
treated as a read, skipped the guard, and was handed to `requests` — which
sends it as an ordinary POST. **The policy check and the send looked at
different values.** This is the one fix in this pack that changes production
behaviour, because it was a genuine defect.

`_normalized_http_method` canonicalizes `str`, `bytes` and `bytearray`;
`_is_mutating_method` returns True for a write verb **or for anything it
cannot prove is a read**. An unclassifiable method — `None`, an int, a bytes
sequence that is not ASCII, an object with a lying `.upper()` — is refused as
a write. A lost read is an incident; an unguarded write is an order.

Ordering matters and is deliberate: the policy refusal comes first, so an
unauthorized production account answers with `BrokerWriteForbidden` rather
than a type error that would hide the real reason.

**`METHOD_NORMALIZATION_SAFE=YES`**

### FIX 5 — RC-2 MEDIUM-2: four guard-weakening mutations survived

Each was correct behaviour that nothing pinned. All four are now killed, and
each has a control so the pin cannot be satisfied by a guard that refuses
everything:

| # | mutation | now pinned by |
|---|---|---|
| 1 | `if self.env != "prod": return` | eight non-`demo` env strings refuse; control: exactly `demo` passes |
| 2 | dropping `.upper()` | lowercase and padded verbs refuse; control: a lowercase read is normalized and sent |
| 3 | `getattr(CFG, ..., True)` | a deleted class attribute does not authorize and sends nothing |
| 4 | `if "demo" in self.base_url` | a prod client on a `demo`-looking url refuses; control: a demo client on a prod-looking url passes |

**`RC2_PINNING_MUTATIONS_KILLED=4/4`** (6/6 including the two MEDIUM-1 mutants)

### FIX 6 — RC-3 MED-2: contradictory operator state at startup

A `READ_ONLY` process could start with `LIVE_BROKER_WRITES_AUTHORIZED`
already armed. Nothing could mutate *today* — read-only dominates — but a
later `CAPITAL` start from that same environment would inherit a write
authorization nobody decided on for that mode.

**Option A**, as instructed: production `READ_ONLY` startup **refuses** when
the authorization resolves true, using the same strict `_env_gate` parsing,
and checking the frozen config value as well as the live environment. The
variable is **not** rewritten — silently clearing it would hide the
misconfiguration that produced it.

The tests assert the *specific* refusal, not merely a non-zero exit: an
earlier draft passed with the check deleted, because a production start
without credentials also exits non-zero. Mutants **A9** and **A10** are killed
only by the strengthened version.

**`READ_ONLY_WITH_ARMED_WRITE_AUTH=REFUSED`**

### FIX 7 — RC-3 MED-3: tests could authenticate to the real account

`StartupMatrix._start` passed `os.environ` to a child process minus seven
keys, and `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY` were not among them. On a
machine with production credentials exported, three tests booted far enough to
issue authenticated `GET /portfolio/*` against the real production account.
Reads only — but "the tests do not touch the broker" was a property of CI
happening to have no credentials, not a property of the tests.

Two independent changes, in `tests/_netblock.py`:

* every production credential is stripped from the child environment, so the
  credential gate stops the boot **deterministically** — which is what the
  docstring already claimed happened; and
* a generated `sitecustomize.py` on the child's `PYTHONPATH` makes DNS
  (`getaddrinfo`) and connection (`socket.connect`, `create_connection`) raise
  before any application code runs, recording every attempt. Two layers,
  because a caller with a hard-coded IP bypasses the first.

`_start` now asserts, for every case in the class, that the child attempted
**no outbound connection of any kind**.

One subtlety worth recording: behind an HTTP proxy the host a client resolves
is the *proxy*, so a request on its way to Kalshi records `127.0.0.1` and
names no Kalshi host. A broker-hostname check alone would therefore be
vacuous. That is why the guard asserts zero attempts rather than zero broker
attempts, and why `BROKER_HOST_MARKERS` is documented as reporting only.

`tests/test_network_isolation.py` tests the block itself — a block nobody
tests is a block that quietly stops working, and its failure mode is silent.

**`TESTS_CANNOT_CONTACT_REAL_PROD=YES`**

### FIX 8 — RC-2 MEDIUM-3: cancellation policy documented

`docs/cancellation-operator-procedure.md`. No redesign: the policy is
recorded, not changed. It states the mode matrix, the emergency procedure for
stopping new exposure, how to inspect open orders (always available — reads
are never gated), the only supported cancellation route, and the consequence
that matters:

> Revoking write authorization strands open orders. Flatten first.

The documented matrix is pinned by tests, including the asymmetry that
`KILL_SWITCH` blocks submission but deliberately does **not** block
cancellation, so the runbook cannot silently drift from the code.

**`CANCELLATION_OPERATOR_PROCEDURE_CREATED=YES`**

---

## What did NOT change

* No scientific or model gate. `check_live_allowed()` is untouched.
* `ALLOW_ORDER_SUBMISSION`, `MODEL_APPROVED` and `DAILY_RESEARCH_ORACLE_APPROVED`
  remain false/unset. No Railway variable was read or written.
* No credential was provisioned, and no Kalshi endpoint was contacted.
* No merge, no deploy, no PR. PR-D remains HOLD.
* Only one production behaviour changed: the HTTP method classification
  (FIX 4), which was a defect. FIX 6 adds a startup refusal. Everything else
  in this pack is test and documentation work.

## Scope of this evidence

It establishes that the eight items are closed on this branch, that the suite
agrees under both runners without a hard-coded total, and that each fix is
pinned by a mutation that fails without it. It does **not** establish that the
system is ready for LIVE in either mode: `MODEL_APPROVED` is false,
`model_validation.json` is stale, no model has beaten its out-of-sample
baseline, LIVE reconciliation has never run against a real account, and no
production credential exists. None of that is in this pack's scope.
