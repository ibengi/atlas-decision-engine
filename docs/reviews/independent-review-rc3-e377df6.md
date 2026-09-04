# Independent critical review — RC-3 (`e377df6`)

**Scope**: the delta `a16bebe..e377df6` only, on `claude/railway-atlas-rc3-live-read-only`.
RC-1 (`826d12e..e2d9d41`) and RC-2 (`e2d9d41..a16bebe`) were not re-reviewed; RC-3's
effect on them is assessed in "Effect on RC-1 / RC-2".

**Independence**: `REVIEWER_NOT_INDEPENDENT=FALSE`. This reviewer has no authoring,
editing, testing, designing or reviewing history for `151860a`, `5757c09`, `e0e2f83`,
`5d6e994`, `826d12e`, `e2d9d41`, `a16bebe` or `e377df6`.

**Method**: nothing below is taken from the commit message, code comments,
`docs/prod-access-modes.md` or the convergence board. Every claim is re-derived by
execution (a real `KalshiClient` built through its real `__init__` with a real
generated RSA key, transport replaced by a recorder) or by mutation. Where a result
is asserted from source text rather than behaviour, that is stated.

**Constraints honoured**: no production code modified (mutations applied and restored;
`git status` clean, verified after every batch), no merge, no deploy, no credential
installed, no Railway variable changed, no order submission enabled, `MODEL_APPROVED` /
`DAILY_ORACLE_APPROVED` untouched, PR-D untouched. **No packet was sent to any Kalshi
endpoint**: every harness replaces `requests.Session.request` before the code under test
runs, and records what *would* have been sent.

---

## Verdict

```
BLOCKERS=0   HIGH=2   MEDIUM=6   LOW=5

MODE_PARSING_STRICT=PASS
READ_ONLY_DOMINANCE=PASS
READ_ONLY_MODEL_GATE_BYPASS=SAFE
CAPITAL_MODEL_GATE_PRESERVED=PASS
SHADOW_WRITE_LAYER_CALLS=0        (place_and_track=0, create_order=0, cancel_order=0,
                                   mutating HTTP=0, over 13 mode values, by execution)
RECONCILIATION_READ_ONLY=PASS
ENTRYPOINTS_AUDITED=22   BYPASSES_FOUND=0
MUTATIONS_KILLED=20      SURVIVING_MUTATIONS=8
RETARGETED_TESTS_ACCEPTABLE=YES
VACUOUS_TESTS_FOUND=3    (+3 further tests with materially incomplete scope)
UNITTEST=883/883 (0 failures, 0 errors, 0 skipped)
PYTEST=883 passed, 186 subtests passed
COUNTS_MATCH=YES         (collected 883 = unittest 878 + module-level 5)
RESTART_HARNESS=17/17
RC3_MERGE_READY=YES  (conditional — see "Conditions")
```

**I could not refute the claimed invariant.** Every attempt to produce a Kalshi
mutation under `PROD_ACCESS_MODE=READ_ONLY` failed. The two HIGH findings are not
defects in shipped behaviour; they are places where a claim the branch makes in
prose is not defended by any test that can fail, which I demonstrated with mutants
that survive the full suite.

---

## 1. Mode parsing — `MODE_PARSING_STRICT=PASS`

`config.prod_access_mode()` reads `PROD_ACCESS_MODE` live from the environment on every
call, `.strip().upper()`s it, and returns the value only if it is in
`("READ_ONLY", "CAPITAL")`, else `None`. `prod_is_read_only()` is
`prod_access_mode() != PROD_CAPITAL`.

Verified by execution against **55 hostile values**: absent, `""`, `" "`, `"\t"`,
`"\n"`, `"\r\n"`, `"\x0b"`, `READONLY`, `read only`, `READ-ONLY`, `CAPITOL`, `CAPITA`,
`CAPITALS`, `CAPITAL2`, `_CAPITAL`, `CAPITAL_`, `CAPITAL;--`, `CAPITAL||READ_ONLY`,
`CAPITAL\nREAD_ONLY`, `CAPITAL READ_ONLY`, `'CAPITAL'`, `"CAPITAL"`, `CAPITAL,CAPITAL`,
`[CAPITAL]`, `{'mode':'CAPITAL'}`, `1`, `0`, `true`, `yes`, `on`, `None`, `null`,
`prod`, `PROD`, `production`, `live`, `LIVE`, `capital ital`, and unicode homoglyph /
normalisation traps — Cyrillic `С`, `с`, `Ł`, fullwidth `ＣＡＰＩＴＡＬ`, `ℂ`, `ᴄ`,
Cherokee `Ꮮ`, roman-numeral `Ⅽ`, zero-width space, BOM, Mongolian vowel separator,
soft hyphen. **None resolved to `CAPITAL`; all resolved to read-only.**

A NUL byte cannot be tested: the OS rejects `os.environ[k] = "...\x00..."` before the
code is reached.

Anti-vacuity control: `CAPITAL`, `capital`, `" Capital "`, `"CAPITAL\n"`,
`"\tCAPITAL\t"` **are** recognised, so the pass is not produced by a helper that
recognises nothing.

The "was CAPITAL requested?" phrasing was verified **behaviourally**, not by reading the
source: an unseen token (`SOMETHING_NEW`) resolves to read-only, which the inverse
phrasing could not do. Mutant **N6** (rephrase to `== PROD_READ_ONLY`) is killed.
`prod_access_mode()` returns `None` — an explicit "unreadable", not a silent default —
and mutant **M6** (unknown → `CAPITAL`) is killed.

## 2. READ_ONLY dominance — `READ_ONLY_DOMINANCE=PASS`

**1 408 write attempts, all refused, zero mutating HTTP.** A real `KalshiClient("prod")`
(real `__init__`, real 2048-bit RSA key, real `PROD_URL`), transport replaced by a
recorder that treats any `POST/PUT/PATCH/DELETE` as an escape. The matrix:

* 16 combinations of `LIVE_BROKER_WRITES_AUTHORIZED`, `ALLOW_ORDER_SUBMISSION`,
  `KILL_SWITCH`, `DAILY_RESEARCH_ORACLE_APPROVED`
* × 2 states of the process env flags (`LIVE_TRADING=1`, `LIVE_TRADING_CONFIRMED=YES`,
  `KALSHI_ENV_CONFIRM=LIVE`, `MODEL_APPROVED_FOR_LIVE=YES`, `NO_LIVE_PROMOTION=0`,
  `DAILY_ORACLE_APPROVED=YES` — all set, vs all cleared)
* × 4 mode values (`READ_ONLY`, `""`, `CAPITOL`, absent)
* × 11 entry points: `create_order`, `cancel_order`, `_req` with `POST`/`PUT`/`PATCH`/
  `DELETE`, lowercase `post`, mixed-case `DeLeTe`, `_req(..., retries=9)`, a
  not-yet-written `/portfolio/amend/future` endpoint, and a mutating verb on a
  non-`/portfolio` path.

Every one raised `BrokerWriteForbidden` naming the read-only prohibition, and the
recorder saw **zero** mutating requests.

Anti-vacuity control: with `PROD_ACCESS_MODE=CAPITAL` and full authorization, the same
`create_order` produced exactly one `POST https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders`
at the transport — so the refusals above are the guard, not a client that never works.

All 7 read endpoints (`get_markets`, `get_market`, `_fetch_balance`, `get_order`,
`get_fills`, `list_orders`, `get_positions`) still reach the transport in `READ_ONLY`,
all as `GET`.

**Two layers hold independently.** `_req` asserts on any verb in
`MUTATING_HTTP_METHODS` before anything else, and `create_order`/`cancel_order` assert
on their own. Mutant **N18** (delete `create_order`'s own guard) and **N3** (move the
transport assert to *after* `session.request`) are both killed. Mutant **N7** (narrow
`MUTATING_HTTP_METHODS` to `{POST}`) is killed despite the author's verb test iterating
over that very set — the self-reference does not make it vacuous.

**Ordering**: the read-only block is textually above the `LIVE_BROKER_WRITES_AUTHORIZED`
block. See MED-1 — the *test* for that ordering cannot fail when the order is swapped.

No code path bypasses `_req`. The only direct `requests` calls outside it are
`btc_context.py` / `research/fetch_btc_candles.py` (BTC candles), `health_monitor.py`
(unauthenticated `GET /markets`) and `alert_notifier.py` (an outbound webhook, not
Kalshi). `market_scanner.py:279` uses `client._req("GET", ...)`.

## 3. Gatekeeper split

`model_gatekeeper.py` is **byte-identical** across `a16bebe..e377df6` — `git diff` for
that path is empty. All four scientific criteria (`NO_LIVE_PROMOTION != 1`,
`MODEL_APPROVED_FOR_LIVE=YES`, `test_report.json` fresh + green, `model_validation.json`
approved + < 30 days) are unchanged. `CAPITAL_MODEL_GATE_PRESERVED=PASS`.

`READ_ONLY` starts without consulting the gatekeeper (mutant **M8**, making the
gatekeeper also block observation, is killed; **M9**, letting CAPITAL skip it, is
killed). `READ_ONLY_MODEL_GATE_BYPASS=SAFE`: the bypass is of the *approval* gate only,
and it is safe because the write prohibition is enforced at the client boundary
independently of it — proven in §2, and mutant **N11** (delete the startup mode
validation entirely) is killed while the guarantee still holds at the client.

### Judging the dropped controls

`READ_ONLY` keeps `KALSHI_ENV_CONFIRM=LIVE` and drops `LIVE_TRADING` and
`LIVE_TRADING_CONFIRMED`. **This is right**, on the merits: those two are named for, and
mean, *trading*; requiring them to read a public order book is the same category error
the commit sets out to fix. `KALSHI_ENV_CONFIRM` is the one that means "I deliberately
intend to touch production", and it is correctly kept for both modes — mutant **N19**
(drop it in READ_ONLY) is killed.

**But the count of independent things that must be true drops from four to two.**
Before RC-3, a process running against production credentials required
`KALSHI_ENV_CONFIRM` + `LIVE_TRADING` + `LIVE_TRADING_CONFIRMED` + a passing gatekeeper.
After RC-3, a `READ_ONLY` process requires `KALSHI_ENV_CONFIRM` + a valid mode string.
That is acceptable *because* RC-2's `LIVE_BROKER_WRITES_AUTHORIZED` is a second,
independent client-boundary prohibition that RC-3 does not remove — but see MED-2:
nothing stops a `READ_ONLY` process from running with that second prohibition already
disarmed, leaving `prod_is_read_only()` alone.

## 4. Shadow path — `SHADOW_WRITE_LAYER_CALLS=0`

Verified **by execution, not by AST**. A real `ExecutionEngine` was constructed against a
production client, `OrderManager.place_and_track`, `KalshiClient.create_order` and
`KalshiClient.cancel_order` were replaced with spies that raise on call, and a complete
`Decision` (`btc_15m_above_strike`, yes, model 0.62 / market 0.40, net edge +0.18) was
driven through `_execute_decision`.

For all 13 read-only-resolving mode values (`READ_ONLY`, absent, `""`, `"  "`, `CAPITOL`,
`readonly`, `capital ital`, `CAPITAL;--`, `PROD`, `production`, `live`, `true`, `1`):

```
place_and_track=0  create_order=0  cancel_order=0  mutating HTTP=0
returned 0, report["would_submit"]=1, rejections["prod_read_only"]=1
```

Anti-vacuity control: with `PROD_ACCESS_MODE=CAPITAL` the **same** decision reaches
`place_and_track("KXBTC15M-26SEP0418-B50", "yes", 12, 40)` — so the zeros are the branch,
not a decision that was rejected earlier. (My first harness *was* vacuous: the decision
was rejected at the market-type allowlist and produced zeros for the wrong reason. The
control caught it. This is exactly the failure mode §9 warns about.)

`DEMO` control: with the same `READ_ONLY` mode, a demo client still reaches
`place_and_track` — the branch does not leak into demo.

The branch returns before `claim_half_open_attempt`, so a read-only cycle does not
consume the circuit breaker's single half-open attempt, and before
`report["orders_submitted"]` is incremented.

**Is the author's AST/source-order assertion sufficient? No — demonstrably.** See HIGH-1.

## 5. Reconciliation — `RECONCILIATION_READ_ONLY=PASS`

By execution, with a client double that raises if any write method is touched.
`reconcile_startup` given one unresolved resting order:

| mode | `cancel_order` called |
|---|---|
| `READ_ONLY` | **no** |
| absent | **no** |
| `CAPITOL` | **no** |
| `CAPITAL` | **yes** (control — proves the test can observe a cancel) |

The branch **skips** (`continue`), it does not log and fall through: the unresolved order
is **retained** in `open_orders` (fail-closed, so a human decides later under CAPITAL)
and **no** local position is opened from it. Mutant **M7** (remove the guard) is killed.
The TTL cancel inside `place_and_track` is guarded too; mutant **N10** (remove it) is
killed. `resolve_pending_intents` is a read path (`find_orders_by_client_order_id`).

## 6. Entrypoints — `ENTRYPOINTS_AUDITED=22  BYPASSES_FOUND=0`

Only **three** call sites construct a `KalshiClient` in the whole repository:

| site | env | verdict |
|---|---|---|
| `kalshi_alpha_bot.py:338` | `KalshiClient(env)` | the main entrypoint; passes the startup mode validation |
| `kalshi_edge_measure.py:333` | `KalshiClient("prod")` | **outside** the startup check — safe, see below |
| `kalshi_demo_execution_check.py:59` | `KalshiClient("demo")` | demo only |

The author's claim about `kalshi_edge_measure.py` is **confirmed**: an AST walk over the
whole module finds no `create_order`, `cancel_order`, `place_and_track`, `_req`,
`request` or `post` attribute anywhere, and `resolve_pending(fetch_market_fn, path)`
receives only the bound `kc.get_market` — it is handed a function, never the client, so
it cannot reach any other method.

Others checked, all clear: `run.py` (thin wrapper around `main`), `dashboard_web.py`
(GET-only `ThreadingHTTPServer` in a daemon thread; reads JSON files from `DATA_DIR`; no
`do_POST`/`do_PUT`/`do_DELETE`, no Kalshi client), `health_monitor.py` (unauthenticated
`requests.get(base + "/markets")`), `status.py` (reads JSON files), `alert_notifier.py`
(outbound webhook `POST`, not Kalshi), the 7 files in `tools/` (offline research +
`restart_harness.py`, which uses a `MagicMock` client), the 4 in `research/` (BTC candle
acquisition), `Procfile` (`--loop --demo`), `Dockerfile` `CMD` (`--loop --demo`),
`railway.json` (build config only), and the 4 background threads in the codebase
(`dashboard_web` server, and locks in `api_cache`/`health_monitor`/`timing`).

Count: 3 client-construction sites + `run.py`, `dashboard_web.py`, `health_monitor.py`,
`status.py`, `alert_notifier.py` (5) + `tools/*.py` (7) + `research/*.py` (4) +
`Procfile`, `Dockerfile` `CMD`, `railway.json` (3) = **22**.

**Confirmed by execution:** a `READ_ONLY` production boot needs only
`KALSHI_ENV_CONFIRM=LIVE`, `PROD_ACCESS_MODE=READ_ONLY` and well-formed credentials, and
it *does* reach the production host — 20 recorded requests to
`api.elections.kalshi.com`, all authenticated `GET /portfolio/balance` and
`GET /portfolio/positions`, **zero mutating verbs**. (Recorded, not sent.) This is the
intended behaviour of the mode; it is also the basis of MED-3.

## 7. Mutations — `MUTATIONS_KILLED=20  SURVIVING_MUTATIONS=8`

28 mutants, each applied to the working tree, full `pytest tests/` run, then restored.
`git status` clean after every batch.

### The author's M1–M9, reimplemented independently

| id | mutation | result |
|---|---|---|
| M1 | read-only guard deleted from the client boundary | KILLED |
| **M2** | **engine read-only branch disabled (`if False and …`), text retained** | **SURVIVED** |
| M3 | cancel permitted in read-only (method guard + `DELETE` backstop removed) | KILLED |
| M4 | `MODEL_APPROVED_FOR_LIVE` upgrades read-only to capital | KILLED |
| M5 | `--shadow` selects `CAPITAL` | KILLED |
| M6 | invalid/unknown mode resolves to `CAPITAL` | KILLED |
| M7 | reconciliation repairs (cancels) under read-only | KILLED |
| M8 | model gatekeeper also blocks `READ_ONLY` observation | KILLED |
| M9 | `CAPITAL` skips the model gatekeeper | KILLED |
| M2b | engine read-only branch **deleted outright** (text removed too) | KILLED |

**The claim "M1–M9 all killed" is not reproducible as stated.** M2 and M2b are
behaviourally identical — both remove the engine's read-only branch entirely — but only
the one that also removes the *string* `prod_is_read_only()` from the source is caught.
The failure for M2b is, verbatim:

```
tests/test_prod_access_mode.py:210: ValueError: substring not found
   ro = src.index("prod_is_read_only()")
tests/test_prod_access_mode.py:219: ValueError: substring not found
   branch = src[src.index("prod_is_read_only()"):]
```

That is a crash on a missing substring, not an observed behaviour. See HIGH-1.

The author's report that **M5 survived a first pass** is credible and consistent with
what I see: `test_shadow_does_NOT_select_a_production_mode` and
`test_shadow_does_not_upgrade_read_only_to_capital` are the two tests that now kill it,
and both are new in RC-3. I did not attempt to reconstruct the pre-fix tree to
reproduce the survival itself.

### Mutations the author did not try

| id | mutation | result |
|---|---|---|
| **N1** | engine `env != "demo"` → `env == "prod"` | **SURVIVED** |
| **N2** | reconciliation `env != "demo"` → `env == "prod"` | **SURVIVED** |
| N3 | transport read-only assert moved *after* the request is issued | KILLED |
| N4 | mode read once at import and cached | KILLED |
| **N5** | read-only check demoted **below** `LIVE_BROKER_WRITES_AUTHORIZED` | **SURVIVED** |
| N6 | helper phrased "is it READ_ONLY?" | KILLED |
| N7 | `MUTATING_HTTP_METHODS` narrowed to `{POST}` | KILLED |
| **N8** | engine honours read-only **only when `SHADOW_MODE`** | **SURVIVED** |
| **N9** | startup accepts `PROD`/`PRODUCTION`/`LIVE` as `CAPITAL` synonyms | **SURVIVED** |
| N10 | TTL cancel guard removed from `place_and_track` | KILLED |
| N11 | startup mode validation deleted entirely | KILLED |
| N12 | `--live-read-only` silently sets `CAPITAL` | KILLED |
| **N15** | client demo exemption `env == "demo"` → `env != "prod"` | **SURVIVED** |
| N16 | a third access mode accepted and treated as writable | KILLED |
| N17 | engine read-only branch moved below the breaker claim and counter | KILLED |
| N18 | `create_order`'s own guard call deleted | KILLED |
| N19 | `KALSHI_ENV_CONFIRM` no longer required in `READ_ONLY` | KILLED |
| **N20** | `--live-capital` also sets `LIVE_TRADING`/`LIVE_TRADING_CONFIRMED` | **SURVIVED** |

Runtime mutation of `PROD_ACCESS_MODE` mid-cycle was probed behaviourally rather than
as a source mutation — see MED-2.

**None of the 8 survivors produces a Kalshi mutation under `READ_ONLY`.** N1, N2, N8 and
N15 leave the client-boundary guard intact and I confirmed it still refuses. They are
regression-protection gaps, not live bypasses.

## 8. Retargeted tests — `RETARGETED_TESTS_ACCEPTABLE=YES`

Both changes are genuine retargeting, and I judge the author's claim **correct**.

`tests/test_live_write_authorization.py` — `PROD_ACCESS_MODE=CAPITAL` is set in
`setUp`/torn down in `tearDown` of **one class only**,
`LiveWritesAreRefusedAtTheClientBoundary`, whose declared subject is the
`LIVE_BROKER_WRITES_AUTHORIZED` gate. That gate now sits below a higher-priority one,
so without `CAPITAL` the class would assert on a refusal it was not written for. The
sibling classes `GateIsStrictAndFailsClosed`, `LiveReadsRemainAvailable` and
`DemoBehaviourIsUnchanged` are untouched and correctly so — none of them is affected by
the new gate.

`tests/test_prod_credential_gate.py` — one added dict key in one method
(`test_full_engine_prod_boot_refuses_after_live_confirmations`), whose stated subject is
the credential gate, which is validated *after* the mode block.

**Is any prior guarantee now untested?** No. The pre-RC-3 configuration those tests ran
under (`PROD_ACCESS_MODE` absent) is now covered by
`ReadOnlyDominatesEveryOtherFlag.test_malformed_mode_also_blocks` and by
`ModeParsingIsStrict`, and independently by my own §2 matrix, which includes the
absent-mode case in all 16 flag combinations. The RC-2 anti-vacuity control
(`test_control_authorized_live_write_reaches_the_transport`, which requires the write to
*succeed*) still runs and still succeeds, now under `CAPITAL` — so the retargeting did
not turn the suite into one that only ever refuses. Mutant **N18** confirms RC-2's
method-level layer is still independently pinned.

One cosmetic consequence: the two refusal messages both contain the phrase
`LECTURE SEULE` (the `LIVE_BROKER_WRITES_AUTHORIZED` message ends "Le LIVE est en
LECTURE SEULE par construction"), so that substring does **not** discriminate between
them. This is what makes MED-1 possible.

## 9. Vacuity — `VACUOUS_TESTS_FOUND=3` (+3 with materially incomplete scope)

**Vacuous / not testing what they claim:**

1. `ShadowDoesNotInvokeTheWriteLayer` (both tests) — assert only on the *order of
   substrings* in `ast.unparse`d source. Survivors M2 and N8 prove they cannot fail on
   behaviour. → HIGH-1.
2. `test_read_only_beats_live_broker_writes_authorized` — cannot fail when the two
   blocks are swapped (N5). → MED-1.
3. `ReconciliationIsReadOnly` (both tests) — same class of assertion as (1):
   `src.index("prod_is_read_only()") < src.index("cancel_order")` plus an
   `assertIn("continue", …)`. The behaviour is in fact correct (I proved it by execution
   in §5), and mutant M7 is killed — but killed the same way M2b was, by removing the
   text the test indexes on. A behaviour-preserving-text variant would survive.

**Materially incomplete scope (not vacuous, but narrower than they read):**

4. `test_read_only_is_expressed_as_NOT_capital` — filters the docstring by dropping only
   lines that *contain* `"""`, leaving the docstring's interior prose inside the "code"
   it then asserts on. Non-vacuous today (the docstring contains neither `!=` nor
   `PROD_CAPITAL`), but one docstring edit away from vacuity. → LOW-1.
5. `NoEntrypointCanEscapeTheGuard._modules_building_prod_clients` — globs `*.py` at the
   repository **root only**. `tools/`, `research/`, `tests/` and any future package are
   unpinned. Its finding is correct today (I verified only 3 `KalshiClient(` sites exist,
   all at root), but a new production-client builder under `tools/` would not trip it.
   → MED-6.
6. `test_no_secondary_entrypoint_calls_a_write_method` — naive substring match for
   `.create_order(`, `.cancel_order(`, `place_and_track`, `._req(` on lines that do not
   *start* with `#`. Does not cover `session.request`, `requests.post`, or a client bound
   to a differently-named attribute. → LOW-4.

I found **no** fabricated `_pk` problem: `_prod_client()` sets `_pk = object()` and stubs
`_sign_headers`, which is a deliberate and correct way to test the guard rather than the
crypto — and my §2 matrix re-ran the same assertions on a client with a **real** loaded
RSA key and got the same results. No test hangs the entrypoint in the current tree.

## 10. Suites

Run in a clean virtualenv (`requirements.txt` + `requirements-dev.txt`). Note: the
system `cryptography` 41.0.7 in this container raises `pyo3_runtime.PanicException` on
import, which makes `run_tests.py` fail before collection; `requirements-dev.txt`
already documents this and installing via pip fixes it. That is an environment issue,
not an RC-3 defect.

```
python run_tests.py       -> Ran 883 tests ... OK
                             {"ran":883,"failures":0,"errors":0,"skipped":0,
                              "collected":883,
                              "collected_by_unittest_discovery":878,
                              "collected_module_level":5}
python -m pytest tests/ -q -> 883 passed, 186 subtests passed in 47.95s
python tools/restart_harness.py -> TOTAL 17/17 checks passed
```

`UNITTEST=883` · `PYTEST=883` · `COUNTS_MATCH=YES` · harness 17/17. The author's counts
are confirmed exactly.

---

## Findings

### HIGH-1 — the `SHADOW_INVOCATES_WRITE_LAYER=NO` claim is not defended by any test that can fail

`docs/prod-access-modes.md` and the commit message state the engine reaches a complete
decision **without calling `place_and_track`**. The property is **true today** — I proved
it by execution in §4. It is protected by nothing.

Two mutants that remove it survive the full suite:

* **M2**: `if False and self.client.env != "demo" and prod_is_read_only():` — the branch
  never runs; in production `READ_ONLY` the engine falls through to `place_and_track`,
  `OrderManager` runs its whole gate sequence, `create_order` is called, and the client
  refuses. Exactly the "try then fail" the commit says must not happen. **883/883 pass.**
* **N8**: `if CFG.SHADOW_MODE and self.client.env != "demo" and prod_is_read_only():` —
  read-only is honoured only in shadow mode, so a production `READ_ONLY` non-shadow run
  attempts a real write on every accepted decision. **883/883 pass.**

Both survive because the only assertions are `src.index("prod_is_read_only()") <
src.index("place_and_track")` and a substring check on the branch text — both still
satisfied. The mutant that *is* caught (M2b, deleting the block) is caught by
`ValueError: substring not found`, i.e. the test crashing on absent source text.

**Fix**: replace the two source-order assertions with an executable one. A working
version (used for §4 of this review) is: construct a real `ExecutionEngine` on a
production client, patch `OrderManager.place_and_track` and `KalshiClient.create_order`
with spies, drive a complete `Decision` through `_execute_decision`, assert
`call_count == 0` and `report["would_submit"] == 1` — **and include the `CAPITAL`
control asserting `place_and_track` *is* called with the same decision**, without which
the test passes on a decision rejected earlier for an unrelated reason (this happened to
me on the first attempt). `ReconciliationIsReadOnly` needs the same treatment.

### HIGH-2 — `--live-capital` has no test forbidding it from supplying the LIVE confirmations

Mutant **N20** makes `--live-capital` also set `LIVE_TRADING=1` and
`LIVE_TRADING_CONFIRMED=YES`. **883/883 pass.**

That collapses the deliberate double confirmation for real money into a single CLI flag.
`test_capital_still_requires_the_live_trading_confirmations` sets `PROD_ACCESS_MODE`
through the environment and never exercises the flag, so the flag's behaviour is
unasserted. This is the exact symmetric case of M5 ("nothing asserted that `--shadow`
must not select an access mode"), which the author found and fixed for `--shadow` but
not for `--live-capital`.

The model gatekeeper would still refuse (`NO_LIVE_PROMOTION` defaults to `1`,
`MODEL_APPROVED_FOR_LIVE` is unset), so this is not a full bypass — but it removes one of
two independent human confirmations on the capital path, undetected.

**Fix**: assert that `--live-capital` sets `PROD_ACCESS_MODE` **and nothing else** — for
example, snapshot `os.environ` around the flag-handling block and assert the only key
that changed is `PROD_ACCESS_MODE`. The same assertion covers `--live-read-only` and
`--shadow` in one test.

### MED-1 — the "READ_ONLY is checked first" ordering is unverifiable by the test named for it

Mutant **N5** swaps the read-only block below the `LIVE_BROKER_WRITES_AUTHORIZED` block.
**883/883 pass**, including `test_read_only_beats_live_broker_writes_authorized`. It
passes because that test sets `LIVE_BROKER_WRITES_AUTHORIZED = True`, so the demoted
first check does not fire, and the read-only message appears either way; and its
`assertNotIn` targets the substring `"LIVE_BROKER_WRITES_AUTHORIZED n'est pas"`, which is
absent in both orderings.

Not a safety defect *today*: both blocks `raise`, so the outcome is identical and only
the operator-facing message differs. But the ordering is stated as a load-bearing
invariant in the commit message and in `docs/prod-access-modes.md`, and it is untested.

**Fix**: set `LIVE_BROKER_WRITES_AUTHORIZED = False` **and** `PROD_ACCESS_MODE=READ_ONLY`
together, then assert the message is the read-only one. That fails if and only if the
order is wrong.

### MED-2 — a `READ_ONLY` process may run with `LIVE_BROKER_WRITES_AUTHORIZED` already armed

Nothing refuses `PROD_ACCESS_MODE=READ_ONLY` together with
`LIVE_BROKER_WRITES_AUTHORIZED=true`. In that configuration `prod_is_read_only()` is the
**sole** prohibition between a running process and a production mutation.

`PROD_ACCESS_MODE` is re-read from `os.environ` on every call (correctly — mutant N4,
caching it, is killed), while `CFG.LIVE_BROKER_WRITES_AUTHORIZED` is a class attribute
frozen at import. Demonstrated by execution, with the transport recording rather than
sending:

```
CFG.LIVE_BROKER_WRITES_AUTHORIZED at import = True
  boot: READ_ONLY            -> REFUSED;                    POSTs = 0
  runtime flip -> CAPITAL    -> reached transport;          POSTs = 4 (1 + 3 retries)
```

with **no** `LIVE_TRADING`, **no** `LIVE_TRADING_CONFIRMED`, **no**
`MODEL_APPROVED_FOR_LIVE`, no restart and no gatekeeper re-check — a single
`os.environ["PROD_ACCESS_MODE"] = "CAPITAL"` anywhere in the process is sufficient.

This needs in-process code execution, so it is not an external attack path, and it is
partly pre-existing (startup gates were never re-evaluated at write time). But it is
avoidable, and avoiding it restores the two-independent-gates property that §3 relies on.

**Fix**: refuse startup when `PROD_ACCESS_MODE=READ_ONLY` and
`LIVE_BROKER_WRITES_AUTHORIZED` is true (or force it false for the process). A
`READ_ONLY` run has no legitimate use for an armed write authorization.

### MED-3 — three tests can authenticate to the real production account on a credentialed machine

`StartupMatrix._start` runs the real entrypoint via `subprocess` with `--loop`, passing
through `os.environ` minus seven keys — `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` are **not**
among the seven. `prod_credentials_config()` accepts any non-empty key id plus any
well-formed RSA PEM. Nothing else stands between the mode logic and
`ExecutionEngine.cycle()`.

Three tests take that path: `test_read_only_starts_with_model_approved_false`,
`test_cli_flag_implies_read_only_without_any_trading_flag`,
`test_shadow_does_not_upgrade_read_only_to_capital`.

I confirmed by execution (§6) that this boot reaches
`api.elections.kalshi.com` and issues **authenticated** `GET /portfolio/balance` and
`GET /portfolio/positions`. On a machine with production credentials exported,
`pytest tests/` would poll the real production account for up to the 180 s subprocess
timeout, three times.

Reads only — `LIVE_BROKER_WRITES_AUTHORIZED` is absent and `READ_ONLY` holds, so no
capital is at risk. But "no broker contact during tests" is currently an accident of the
CI environment having no credentials, not a property of the tests. The `CAPITAL` variants
are safe by construction (the gatekeeper refuses because the harness strips
`NO_LIVE_PROMOTION` and `MODEL_APPROVED_FOR_LIVE`).

**Fix**: add `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY` to the keys `_start` strips, and
drop `--loop` (a single cycle is enough to observe the startup log). The credential gate
then stops the boot deterministically, which is what the docstring already claims
happens.

### MED-4 — the engine and reconciliation environment tests are unpinned

Mutants **N1** and **N2** change `self.client.env != "demo"` to `self.client.env ==
"prod"` in `execution_engine._execute_decision` and
`order_manager.reconcile_startup`. **883/883 pass** for both.

With any env string other than `"prod"`/`"demo"` — `"PROD"`, `"production"`, `"live"`,
`""` — both read-only branches would be skipped. The client guard still refuses (its
exemption is the exact string `"demo"`, and I verified `PROD`, `production`, `live`, `""`,
`sandbox`, `Demo`, `DEMO`, `"demo "` are all guarded), so **no mutation results** — but the
"shadow does not try then fail" and "reconciliation does not repair" layers would both be
silently gone. Not reachable today: the entrypoint constructs `KalshiClient(env)` with
`env` computed as exactly `"demo"` or `"prod"`.

### MED-5 — the set of strings meaning `CAPITAL` is not pinned

Mutant **N9** makes `prod_access_mode()` also return `CAPITAL` for `PROD`, `PRODUCTION`
and `LIVE`. **883/883 pass.** `test_absent_blank_and_malformed_are_never_capital`
enumerates 13 hostile values; none of them is a production-sounding word, and nothing
asserts the *only* accepted spelling is `CAPITAL`.

**Fix**: assert the equivalence directly — for a corpus of candidate strings,
`prod_access_mode(s) == CAPITAL` **iff** `s.strip().upper() == "CAPITAL"`.

### MED-6 — the production-client-builder pin covers only the repository root

`_modules_building_prod_clients()` uses `pathlib.Path(_ROOT).glob("*.py")`, not
`rglob`/`**`. `tools/` (7 modules), `research/` (4) and any future package are outside
it. The test's current finding is accurate — I independently confirmed the three call
sites — but the guarantee it advertises ("the set of prod client builders stays a known
list") does not extend to subdirectories.

Related, and the more serious version of the same theme: mutant **N15** changes the
client's demo exemption from `if self.env == "demo": return` to
`if self.env != "prod": return`, which would make **every** write guard — RC-3's *and*
RC-2's — skippable for any env string that is not literally `"prod"`. **883/883 pass.**
This gap is inherited from RC-2 (its `_live_client()` hardcodes `env = "prod"`), not
introduced by RC-3, but RC-3 now depends on it too.

### LOW-1 — `test_read_only_is_expressed_as_NOT_capital` filters docstrings by delimiter line only

`"\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#") and '"""' not
in ln)` removes only the lines carrying the triple-quote, leaving the docstring body in
the string it asserts on. Non-vacuous today; fragile. Prefer the `ast`-based
`_executable_source` helper already defined in the same file, or assert behaviourally
(as §1 above does).

### LOW-2 — `would_submit` is absent from every operator-facing summary

`report["would_submit"]` reaches `cycle_report.json` (via `**report`) but is **not** in
the `[CYCLE-SUMMARY]` log line's key list, not in `status.py`'s printed keys, and not in
the dashboard payload. The telemetry the mode exists to produce is visible only in the
per-decision `TRADE` log line and the raw JSON file.

### LOW-3 — `DEMO_TRADING=1` is not treated like `--demo` for mode conflicts

`--live-read-only`/`--live-capital` are refused when combined with `--demo`, but the
check tests `args.demo` only. With `DEMO_TRADING=1` in the environment, `--live-capital`
is accepted, `PROD_ACCESS_MODE=CAPITAL` is exported, and `env` resolves to `"demo"`
anyway. No capital risk (demo), but the operator is told nothing.

### LOW-4 — the secondary-entrypoint write check is a substring scan

See §9 item 6. Does not cover `session.request`, `requests.post`, or an aliased client
attribute. An AST-based check (as I used in §6) is both simpler and stricter.

### LOW-5 — `--scan-only` / `--rank-only` skip the mode validation entirely

The whole block is guarded by `if env == "prod" and not (args.scan_only or
args.rank_only)`. A production `--scan-only` run therefore requires neither
`KALSHI_ENV_CONFIRM` nor a valid `PROD_ACCESS_MODE`. **Pre-existing and unchanged by
RC-3**, and safe (no mode ⇒ read-only at the client, and no `ExecutionEngine` is built).
Noted only because `docs/prod-access-modes.md` states without qualification that an
invalid value "refuses production startup", which is not true on those two paths.

---

## Effect on RC-1 and RC-2

Nothing in RC-3 breaks or weakens them, on the evidence I gathered:

* **RC-2** (`LIVE_BROKER_WRITES_AUTHORIZED`) — still enforced at both the method and
  transport layers. Verified by execution: `CAPITAL` + `LIVE_BROKER_WRITES_AUTHORIZED`
  false is refused with a message naming that flag, with zero mutating HTTP. Mutants
  **N18** (delete the method-level guard) and **N3** (move the transport guard after the
  request) are both killed, so both layers remain independently pinned. Its tests were
  retargeted, not weakened (§8). RC-3 adds a prohibition **above** it and removes none.
  The one caveat is MED-6/N15, a gap RC-3 inherits rather than creates.
* **RC-1** (test truth under both runners, breaker on the money path) — both runners
  agree at 883, `collected == unittest 878 + module-level 5`, `test_runner_parity` and
  `test_money_path_kill_switch` pass, restart harness 17/17. The read-only branch returns
  **before** `claim_half_open_attempt`, so it cannot consume the circuit breaker's single
  half-open attempt.
* `model_gatekeeper.py` is byte-identical; `git diff a16bebe..e377df6 -- model_gatekeeper.py`
  is empty.

## Conditions

`RC3_MERGE_READY=YES` is conditional on the two HIGH items, neither of which is a defect
in shipped behaviour:

1. **HIGH-1** — replace the AST/source-order assertions in
   `ShadowDoesNotInvokeTheWriteLayer` (and `ReconciliationIsReadOnly`) with executable
   ones carrying a `CAPITAL` anti-vacuity control. Until then the branch's central
   §4 claim rests on prose and on my §4 run, not on the suite.
2. **HIGH-2** — assert that `--live-capital` sets `PROD_ACCESS_MODE` and nothing else.

MED-2 and MED-3 should be closed before anyone points this branch at a real production
account: MED-3 because `pytest` should never be able to authenticate to a live broker,
MED-2 because a `READ_ONLY` run should not carry an armed write authorization.

## What this review does not establish

It does not establish that the system is LIVE-ready in either mode. No production
credential was used or installed, no Kalshi endpoint was contacted, `MODEL_APPROVED` and
`DAILY_ORACLE_APPROVED` were not set, LIVE reconciliation has never run against a real
account, and no out-of-sample edge was assessed — none of which is in this review's
scope. It establishes only that, on the code at `e377df6`, I could not make
`PROD_ACCESS_MODE=READ_ONLY` mutate Kalshi state.
