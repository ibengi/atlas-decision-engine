# Independent critical review — RC-2 (`a16bebe`), LIVE broker write authorization

**Scope reviewed:** the delta `e2d9d41..a16bebe` only (5 files, +620/-2).
RC-1 (`826d12e..e2d9d41`) was read only to check that RC-2 does not break it.
**Reviewer independence:** no authoring, editing, testing, designing or
reviewing history for `151860a`, `5757c09`, `e0e2f83`, `5d6e994`, `826d12e`,
`e2d9d41`, `a16bebe`. `REVIEWER_NOT_INDEPENDENT=FALSE`.
**Evidence policy:** nothing in the commit message, code comments,
`docs/live-write-authorization.md` or `docs/LIVE_CONVERGENCE_BOARD.md` was
taken as evidence. Every claim below was re-derived from the source or from a
run recorded here. Nothing was merged, deployed, or promoted; no credential was
installed; no Railway variable was read or written; no production code was left
modified (`git status` clean).

**Environment.** The repository's system `cryptography` (41.0.7) aborts with a
`pyo3_runtime.PanicException` on import in this container, which makes
`run_tests.py` fail at collection for reasons unrelated to the delta. All runs
below therefore used a clean venv with `cryptography 50.0.1` and
`requests`/`pytest` per `requirements.txt`. This is an environment artifact, not
a finding against RC-2.

---

## Verdict

```
BLOCKERS= 0
HIGH= 0
MEDIUM= 3
LOW= 5

WRITE_PATHS_INVENTORIED= 2 client write methods / 2 mutating transport calls / 5 call sites
UNPROTECTED_PATHS= 0

LIVE_CLIENT_WRITE_GUARD= PASS
STRICT_FAIL_CLOSED= PASS
GUARD_INDEPENDENT_OF_OTHER_FLAGS= PASS
LIVE_READS_UNCHANGED= PASS
DEMO_BEHAVIOR_UNCHANGED= PASS

UNITTEST= 849/849 pass, 0 failures, 0 errors, 0 skipped (run_tests.py)
PYTEST=   849 passed, 153 subtests passed
(restart harness: 17/17)

MUTATIONS_KILLED= 14 of 18 distinct mutations
SURVIVING_MUTATIONS= 4 (all "correct behaviour, unpinned"; none is a live bypass)
VACUOUS_TESTS_FOUND= 1 partially vacuous (LOW-1), plus 2 self-referential oracles (LOW-2)

RC2_MERGE_READY= YES
```

**The claimed invariant is VERIFIED, not refuted.**

> "A broker write in PRODUCTION requires explicit client-boundary
> authorization. LIVE observation in read-only mode requires none."

Across 38 adversarial probes plus 18 mutations I could not get a mutating HTTP
request to the network in a production environment with
`LIVE_BROKER_WRITES_AUTHORIZED` unset, blank, whitespace, malformed or false,
through any path that exists in the repository today. No read path is blocked.
DEMO is unchanged. RC-1's guards are preserved verbatim and the new guard is
placed *before* them.

`RC2_MERGE_READY=YES` is unconditional on correctness. Two follow-ups are
strongly recommended and are each a few lines: the MEDIUM-1 one-line
normalization, and four one-line tests to pin the four surviving mutations.
They are not merge blockers because in every case the shipped code already
behaves correctly; what is missing is the test that keeps it that way.

---

## 1. Inventory — redone from scratch

Method: `grep`/AST sweep over all 122 tracked `.py` files including
subdirectories (`research/`, `tools/`), not just the repository root.

**Mutating transport calls — 2, both in `kalshi_client.py`:**

| # | Method | Verb + path | Guarded by |
|---|---|---|---|
| 1 | `KalshiClient.create_order` | `POST /portfolio/events/orders` (`kalshi_client.py:362`) | method guard (`:334`) + transport guard (`:190`) |
| 2 | `KalshiClient.cancel_order` | `DELETE /portfolio/events/orders/{id}` (`kalshi_client.py:419`) | method guard (`:418`) + transport guard (`:190`) |

**All 9 `_req` call sites** carry an exact-case string literal verb: 7 `GET`,
1 `POST`, 1 `DELETE`. There is no `_req(<variable>, …)` anywhere.

**Call sites of the two write methods — 5, exactly as claimed:**
`order_manager.py:784` (create), `order_manager.py:938` and `:1058` (cancel),
`kalshi_demo_execution_check.py:113` (create) and `:176` (cancel).
`tools/restart_harness.py` touches `create_order` only on `MagicMock` objects.

**Nothing missed in the categories asked about:**
- No `replace_order`, `amend_order`, `decrease_order`, batch create/cancel, or
  close-position helper exists anywhere in the repository.
- No websockets at all (`ws://`, `wss://`, `websocket`, `aiohttp`, `httpx` —
  zero hits repo-wide). No shell-issued HTTP, no `urlopen`, no `http.client`.
- Redirects are not a hole: `requests` preserves the method on 307/308 and
  downgrades 301/302/303 to `GET`, and in every case the guard has already run
  before the first request is issued.
- Retries do not re-enter `_req`; they loop *inside* it, after the guard.
- The library issues no requests of its own beyond the redirect chain.

**Two HTTP calls exist outside `_req`. Neither is a broker write:**
- `alert_notifier.py:100` — `requests.post` to a configured alerting webhook.
  Not a Kalshi endpoint; the module imports nothing holding Kalshi credentials.
- `health_monitor.py:88` — `requests.get(base + "/markets")`, a bounded
  unauthenticated probe that **does hit the Kalshi host without passing through
  `_req`**. It is a read with a hard-coded `GET` and no method parameter, so it
  cannot become a write without an edit. See LOW-5: the shipped inventory does
  not disclose it.

`UNPROTECTED_PATHS = 0`. The author's count of 2 methods / 5 call sites is
correct.

---

## 2. The guard itself — attempts to defeat it

`_assert_broker_write_allowed` returns early only on `self.env == "demo"`;
everything else requires `CFG.LIVE_BROKER_WRITES_AUTHORIZED`. `_req` re-checks
for any verb in `MUTATING_HTTP_METHODS` as its **first statement**, before the
URL is built, before the RSA-key check, before any request.

I built a production client and ran 38 probes, counting mutating calls that
reached the transport mock. Results:

| Attack | Result |
|---|---|
| `create_order` / `cancel_order`, gate unset | refused, **0** transport calls |
| `_req` direct with `POST`/`PUT`/`PATCH`/`DELETE` | all refused, 0 calls |
| Lowercase / mixed case: `post`, `Post`, `pOsT`, `delete`, `dElEtE` | all refused (`.upper()` normalizes) |
| `env` = `'PROD'`, `'Prod'`, `'production'`, `'live'`, `''`, `None`, `'DEMO'`, `'Demo'`, `'demo '`, `' demo'` | **all refused** — only exact `"demo"` opens; every unknown string is treated as production. Fail-closed. |
| Client built via `__new__` with `env` attribute absent | `AttributeError`, 0 calls |
| `CFG` gate attribute genuinely deleted from the class | `AttributeError`, 0 calls |
| `CFG` gate set to `0`, `''`, `None`, `[]`, `{}` | all refused |

**Ordering verified directly, not inferred.** On a client built through the real
`KalshiClient("prod")` `__init__` with no PROD key (`_pk is None`),
`_req("POST", "/portfolio/events/orders")` returns `BrokerWriteForbidden`, *not*
the pre-existing "cle RSA non chargee" `KalshiAPIError`. The guard demonstrably
runs before the key check.

**This also disposes of a fixture concern.** Every one of the 21 shipped tests
uses a hand-built `_live_client()` fixture (`KalshiClient.__new__`, fabricated
`_pk = object()`, stubbed `_sign_headers`), and no test anywhere in the suite
builds a production client through `__init__`. I did, and it behaves
identically on all four write paths and both read paths. The fixture is not
producing the result.

**Two escapes found. See MEDIUM-1 and LOW-3.** Neither is reachable from any
code path that exists today.

---

## 3. Strictness and independence

`LIVE_BROKER_WRITES_AUTHORIZED = _env_gate("LIVE_BROKER_WRITES_AUTHORIZED",
default=False)`. `_env_gate` returns `default` when absent, `on_invalid`
(default `False`) for anything it does not recognise, and logs
`[CONFIG_GATE_INVALID]` loudly.

I verified the parse **end to end through the real environment → config →
client path** in fresh subprocesses (not by monkeypatching the attribute):

| `LIVE_BROKER_WRITES_AUTHORIZED` | gate | `create_order` | transport |
|---|---|---|---|
| `true`, `1`, `yes`, `on` | `true` | proceeds | `['POST']` |
| `false`, `maybe`, absent | `false` | `BrokerWriteForbidden` | `[]` |

Combined with the shipped subprocess matrix (which I re-ran and which
independently covers `""`, `" "`, `"\t"`, `"\n"`, `ture`, `TRUE!`, `2`, `-1`,
`yes;drop`, `authorized`, `FALSE`, `" No "`, `TRUE`, `" Yes "`):
absent / blank / whitespace / malformed all read FALSE, and only the five
explicit true words arm it. **`STRICT_FAIL_CLOSED = PASS`.**

**Independence — verified more strongly than the shipped test does.** I set
`ALLOW_ORDER_SUBMISSION=true`, `DAILY_RESEARCH_ORACLE_APPROVED=true`,
`KILL_SWITCH=false`, `LIVE_TRADING=1`, `LIVE_TRADING_CONFIRMED=YES`,
`KALSHI_ENV_CONFIRM=LIVE`, `MODEL_APPROVED_FOR_LIVE=YES`, `NO_LIVE_PROMOTION=0`
in the **environment of a fresh subprocess, before `config` is imported** — the
only way any of them can take effect. `CFG.ALLOW_ORDER_SUBMISSION` came back
`true`, `CFG.KILL_SWITCH` `false`, and both `create_order` and `cancel_order`
were still refused with `BrokerWriteForbidden` and **0** transport calls.

The gate is also structurally independent: it is declared from its own variable
name with the strict parser, and the guard body reads only `self.env` and
`CFG.LIVE_BROKER_WRITES_AUTHORIZED`. Making `ALLOW_ORDER_SUBMISSION` bypass the
guard is killed by 14 failures, including
`GuardIsIndependentOfEveryOtherFlag::test_the_guard_consults_only_env_and_its_own_gate`.
**`GUARD_INDEPENDENT_OF_OTHER_FLAGS = PASS`** — though see LOW-1: the shipped
test that is *cited* as this evidence does not actually establish it.

---

## 4. Reads

No read path is blocked. On a real `__init__`-built production client with the
gate false, `get_markets` reaches the transport with `GET`; `get_positions`
(and the other `/portfolio` reads) are stopped only by the **pre-existing** RSA
key check, which is unchanged by this delta. All 7 read methods issue `GET`.

**Are the read tests vacuous because `_sign_headers` is stubbed?** No. Stubbing
the signature removes crypto that is orthogonal to the guard; the assertion
that carries the weight is "at least one request reached `session.request` and
every verb was `GET`", which the stub does not affect. The `except Exception:
pass` swallows only response-shape errors from the mock, and the non-empty-verbs
assertion runs afterwards regardless.

I confirmed the read tests are load-bearing by mutation rather than by reading
them: adding `GET` to `MUTATING_HTTP_METHODS` — which would block every read —
produces **9 failures**, including
`LiveReadsRemainAvailable::test_reads_use_no_mutating_verb_anywhere_in_the_client`.
**`LIVE_READS_UNCHANGED = PASS`.**

---

## 5. DEMO regression

`DEMO_BEHAVIOR_UNCHANGED = PASS`, on three independent grounds:

1. The guard's first statement returns for `env == "demo"`. The only DEMO-side
   change is one function call that returns immediately.
2. `PROD_URL` and `DEMO_URL` are hard-coded literals in `config.py:150-151`,
   not environment-overridable. A DEMO client cannot be pointed at production,
   so the `env == "demo"` early return cannot be turned into a bypass by
   configuration.
3. **Baseline comparison.** I ran the full suite at RC-1 (`e2d9d41`): 828/828
   pass. At RC-2: 849/849 pass. The delta is exactly +21, which is exactly the
   number of tests in the new module. **No pre-existing test changed behaviour.**
   Separately, `test_live_write_authorization.py` is the *only* test in the
   entire suite that constructs a production-environment client — the other 828
   are all DEMO — so RC-2 had almost no surface on which to regress them.

---

## 6. Mutation testing

18 distinct mutations, each applied to a clean tree with the **full suite** run
under pytest and the tree restored afterwards. 14 killed, 4 survived.

### 6a. Reproducing the author's reported M3/M4 survival fix — CONFIRMED

The claim is that deleting either per-method guard once survived, and that
three added tests now pin both layers independently. Reproduced exactly:

| Mutation | Result | Named test that fails |
|---|---|---|
| delete `create_order`'s own guard | **KILLED** (1 failure) | `LiveWritesAreRefusedAtTheClientBoundary::test_create_order_refuses_WITHOUT_relying_on_the_transport_guard` |
| delete `cancel_order`'s own guard | **KILLED** (1 failure) | `…::test_cancel_order_refuses_WITHOUT_relying_on_the_transport_guard` |
| delete **both** per-method guards | **KILLED** (2 failures) | both of the above |
| delete the transport backstop | **KILLED** (6 failures) | `…::test_every_mutating_verb_is_refused_at_the_transport`, `…::test_transport_guard_holds_without_any_method_guard` |

Both layers are now genuinely and independently pinned. The author's account of
this is accurate.

### 6b. The author's other reported mutations — reproduced

| Author's | My equivalent | Result |
|---|---|---|
| M1 remove the guard | guard logs instead of raising | KILLED, 12 failures (author: 12) |
| M2 malformed treated as true | `on_invalid=True` on the gate | KILLED, 2 failures (author: 2) |
| M5 `ALLOW_ORDER_SUBMISSION` bypasses | early `return` if submission allowed | KILLED, 14 failures (author: 13) |

### 6c. Mutations the author did **not** try

| Mutation | Result |
|---|---|
| narrow `MUTATING_HTTP_METHODS` to `{"POST"}` | KILLED (2 failures) |
| narrow to `{"POST","DELETE"}` (drop the unused `PUT`/`PATCH`) | KILLED — the verb set's *breadth* is genuinely pinned |
| add `GET` to the set (would block reads) | KILLED (9 failures) |
| move the transport guard to *after* the network request | KILLED (2 failures) |
| `BrokerWriteForbidden = KalshiAPIError` (alias, not a distinct subclass) | KILLED (1 failure) |
| `default=True` on the config gate | KILLED (1 failure) |
| `_env_gate` → `_env_b` (permissive parser) | KILLED (38 errors) |
| `if self.env != "prod": return` | **SURVIVED** |
| `method in …` (drop `.upper()`) | **SURVIVED** |
| `getattr(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True)` | **SURVIVED** |
| `if "demo" in self.base_url: return` | **SURVIVED** |

The four survivors are analysed as MEDIUM-2. In every case the **shipped code
is correct** — I verified each behaviour directly in §2 — and what is missing is
a test that keeps it correct.

---

## 7. Findings

### MEDIUM-1 — a non-`str` method escapes the transport backstop, and reaches the network

`_req` guards on `method.upper() in MUTATING_HTTP_METHODS`, a `frozenset` of
`str`. `_req(b"POST", …)` gives `b"POST".upper() == b"POST"`, which is never in
a set of `str`, so the guard is skipped. I confirmed against a real local HTTP
server that `requests.Session().request(b"POST", url, json=…)` puts
`POST /x HTTP/1.1` on the wire — this is a **network-reaching** escape, not one
the HTTP library catches.

This matters because the transport layer's stated purpose, in the code comment,
the doc and the board, is precisely durability: *"a future write method is
covered the day it is written, even if its author forgets the guard."* That
holds only if the future author passes an exact-case `str`.

**Not reachable today:** all 9 `_req` call sites pass exact-case `str` literals,
there is no `_req(<variable>, …)` anywhere, and both current write methods carry
their own method-level guard as well.

**Fix (one line):** normalize once at the top of `_req` and use the normalized
verb for both the guard and `session.request` —
`verb = str(method).strip().upper()`. Pin it with
`_req(b"POST", …)` and `_req(" POST", …)` cases.

### MEDIUM-2 — four guard-weakening mutations survive the entire suite

Each is *correct behaviour that nothing pins*:

1. **`if self.env != "prod": return`.** This inverts the guard's polarity from
   *deny-unless-demo* to *allow-unless-prod*. Today `env` is only ever `"demo"`
   or `"prod"` (`kalshi_alpha_bot.py:227`), so it is behaviourally identical —
   but `KalshiClient("staging")` would get `PROD_URL` (because `env != "demo"`)
   **and** an open guard. I verified the shipped code refuses all ten odd `env`
   strings I tried; nothing in the suite says it must.
2. **`method in MUTATING_HTTP_METHODS`** (dropping `.upper()`) — same family as
   MEDIUM-1; `_req("post", …)` would then reach the network.
3. **`getattr(CFG, "LIVE_BROKER_WRITES_AUTHORIZED", True)`** — fail-**open** on
   a missing attribute. The shipped `CFG.X` raises `AttributeError` with zero
   transport calls (verified on a genuinely deleted class attribute); the
   mutation silently authorizes.
4. **`if "demo" in self.base_url: return`** — a plausible refactor that moves
   the trust anchor from `env` to a URL substring. Low real risk while the URLs
   are hard-coded constants, but it is a strictly weaker predicate.

**Fix:** four small tests — an unknown `env` string refuses; a lowercase verb
refuses; a missing gate attribute does not authorize; the guard keys on `env`
rather than on the URL.

### MEDIUM-3 — `cancel_order` on an unauthorized production account fails, and both recovery call sites swallow it

RC-2 deliberately refuses cancellation in unauthorized production, and I agree
with the direction ("read-only does not mean except when it suits us"). The
consequence is worth stating explicitly because no test covers it and no runbook
mentions it: `order_manager.py:938` and `:1058` both sit inside
`except KalshiAPIError`, and `BrokerWriteForbidden` subclasses `KalshiAPIError`,
so the refusal is caught and handled fail-closed — the order stays in
`orders_state.json` and is correctly never claimed cancelled. But if
authorization is ever granted, orders placed, and authorization then revoked,
the engine can *observe* the resting order forever and *never* cancel it, on
any cycle, until authorization is restored.

This is the right safety trade. It should be an explicit operator procedure
("revoking authorization strands open orders — flatten first"), not an emergent
property discovered during an incident.

### LOW-1 — the test cited as "THE critical case" proves less than it claims (partial vacuity)

`test_permissive_higher_level_flags_do_NOT_bypass_the_guard` sets
`LIVE_TRADING`, `LIVE_TRADING_CONFIRMED`, `KALSHI_ENV_CONFIRM`,
`MODEL_APPROVED_FOR_LIVE` and `NO_LIVE_PROMOTION` in `os.environ` — *after*
`config` has been imported and its class attributes already evaluated. None of
the five is read by anything the test exercises: `Config` read the environment
at import; `model_gatekeeper` reads them at call time but is never called here;
the guard never reads them.

**Verified by deletion:** I removed all five assignments and the module still
passes **21/21**. They are decorative. The test's non-vacuous content is
`CFG.ALLOW_ORDER_SUBMISSION=True` / `CFG.KILL_SWITCH=False` from
`_PERMISSIVE_EVERYTHING`, which is real.

Both `docs/live-write-authorization.md` and `docs/LIVE_CONVERGENCE_BOARD.md`
cite this test as the evidence for independence from those five flags. It is
not that evidence. **The property itself is true** — I established it separately
in §3 by setting all five in a subprocess environment before import — so this is
a documentation-and-test-quality defect, not a correctness one.

Secondary: the `finally` block `os.environ.pop`s the five unconditionally, so a
run in an environment that legitimately set them would have them destroyed for
every later test in the process.

### LOW-2 — two self-referential test oracles

`_mutating_calls()` and `test_every_mutating_verb_is_refused_at_the_transport`
both derive their expectations from the same `MUTATING_HTTP_METHODS` they are
testing, so shrinking the set weakens the guard and the assertions in lockstep.
Only `test_zero_mutating_calls_of_each_verb`'s hard-coded
`{"POST": 0, "PUT": 0, "PATCH": 0, "DELETE": 0}` pins the set — a single point
of failure for the whole verb-breadth property. It does currently hold (both
narrowing mutations were killed by it), but one literal is thin cover for the
delta's central durability claim.

### LOW-3 — truthiness rather than identity, and padded verbs

- `if not CFG.LIVE_BROKER_WRITES_AUTHORIZED` opens on any truthy value. I
  confirmed that `CFG.LIVE_BROKER_WRITES_AUTHORIZED = "false"` (or `"no"`,
  `"0"`, `"off"`) authorizes the write and a `POST` reaches the transport.
  **Not reachable today:** `_env_gate` only ever returns a `bool`, and the only
  runtime assignment to a `CFG` gate in *production* code is
  `kalshi_alpha_bot.py:216`'s `CFG.SHADOW_MODE = True`. But the pattern the
  weakness needs does exist in the repository: `tools/restart_harness.py`
  assigns six `CFG` attributes at runtime, one of them a **string**
  (`bot.CFG.MAX_CONTRACTS_PER_ORDER = "1"`, line 107). Nothing there touches the
  write gate, and the harness is not production — but "a string gets assigned to
  a config attribute" is evidently a thing that happens here. This is the same
  shape as the pre-existing `ALLOW_ORDER_SUBMISSION`/`KILL_SWITCH` checks, so it
  is a consistency issue rather than a regression — but on a gate whose premise
  is "no value the parser doesn't understand arms a write", `is not True` costs
  nothing.
- `_req(" POST", …)` escapes the guard (`" POST".upper()` is not in the set) and
  *is* passed to `session.request`. Verified against a local socket server that
  `urllib3` rejects it with `ValueError: Method cannot contain non-token
  characters` before any byte is written, so no request is emitted. Not
  exploitable; folded into the MEDIUM-1 `.strip().upper()` fix.

### LOW-4 — `create_order`'s docstring is now contradicted by the code beneath it

It still reads *"`cancel_order` n'est deliberement PAS garde de la meme facon"*.
RC-2 adds a guard to `cancel_order` eleven lines later. The intended meaning
(the *kill switch* does not block cancels) remains true, but the sentence as
written now describes something the file no longer does.

### LOW-5 — the shipped inventory omits a Kalshi HTTP call that bypasses `_req`

`health_monitor.check_api_connectivity` issues `requests.get(base + "/markets")`
directly against the client's own `base_url`. It is a read with a hard-coded
verb and cannot become a write without an edit, so `UNPROTECTED_PATHS` is still
0 — but `docs/live-write-authorization.md` presents the inventory as though
`_req` were the only route to Kalshi, and it is not. One line in the inventory
table would keep the next audit from re-deriving this.

---

## 8. Test runs

All runs on `a16bebe`, clean tree, venv with `cryptography 50.0.1`.

| Runner | Result |
|---|---|
| `python run_tests.py` (unittest discovery — the runner the Docker test stage and `model_gatekeeper` depend on) | **849 ran, 0 failures, 0 errors, 0 skipped**; `collected_by_unittest_discovery=844`, `collected_module_level=5` |
| `python -m pytest -q` | **849 passed, 153 subtests passed** |
| `tools/restart_harness.py` | **17/17 checks passed** |
| `tests/test_live_write_authorization.py` alone, pytest | 21 passed, 11 subtests passed |
| `tests/test_live_write_authorization.py` alone, unittest | 21 ran, OK |
| RC-1 baseline (`e2d9d41`), `run_tests.py` | 828 ran, 0 failures — the +21 delta is exactly the new module |

The author's claims of 849/849 and harness 17/17 are accurate under both runners.

---

## 9. Does RC-2 break RC-1?

No.

- RC-1's three `create_order` policy checks (`ALLOW_ORDER_SUBMISSION`,
  `KILL_SWITCH`, `daily_quarantine_blocks`) are present verbatim, and the new
  environment guard is placed *before* them, so the outermost refusal is the
  strictest one.
- All 828 RC-1 tests still pass. RC-1's own inventory guard,
  `test_money_path_kill_switch.py::BrokerWriteInventoryStaysClosed`, passes and
  is demonstrably still load-bearing (it fires under the
  `ALLOW_ORDER_SUBMISSION`-bypass mutation).
- RC-2 in fact *mitigates* a gap in that RC-1 test, which I noticed while
  reviewing it: `test_no_unsanctioned_direct_create_order_call` scans only
  `pathlib.Path(_ROOT).glob("*.py")` — repository root only, no
  subdirectories — and only for `.create_order(`, not `.cancel_order(`. A new
  caller under `research/` or `tools/` would not be flagged. That is an RC-1
  defect outside this review's scope; RC-2's transport backstop reduces its
  consequence. Recorded here so it is not lost.

---

## 10. Deployment state (verified, not assumed)

- `a16bebe` is **not** an ancestor of `origin/main` (`9b906e8`). RC-2 is not
  merged and not deployed. RC-1 is not merged either.
- `LIVE_BROKER_WRITES_AUTHORIZED` appears nowhere in `railway.json`, the
  `Dockerfile`, or any JSON/TOML/YAML in the repository. On deploy it would be
  absent, and therefore `False`.

---

## 11. What I did **not** verify

Stated plainly rather than inferred:

- **Railway environment variables were not read.** Whether
  `LIVE_BROKER_WRITES_AUTHORIZED` (or any sibling flag) is set in the deployed
  environment is outside what this review establishes. The repository does not
  set it; the live environment was not inspected.
- **No request was ever sent to Kalshi.** Every "reaches the transport" result
  is a mock or a local socket server. That a real Kalshi `POST` succeeds when
  authorized is not established here and was not in scope.
- **The Docker test stage was not built or run.** Suite results are from the
  host venv described above.
- **The system-`cryptography` crash was not root-caused.** It is an environment
  artifact; `run_tests.py` fails at collection with the container's 41.0.7 for
  reasons unrelated to this delta.
- **RC-1 was not re-reviewed.** It was read only far enough to check that RC-2
  preserves it, and to run its baseline.
