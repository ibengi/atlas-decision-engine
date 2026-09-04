# ATLAS — LIVE convergence board

Living record of every defect discovered on the road from the safe DEMO
deployment to a validated LIVE system. An issue reaches **CLOSED** only when
runtime or evidence verification confirms it, never on passing tests alone.

Baseline at the opening of this program: `9b906e8` deployed as `2a3da86f`,
DEMO, `ALLOW_ORDER_SUBMISSION=false`, `MODEL_APPROVED=false`.

Statuses: `DISCOVERED` → `PATCHING` → `REVIEW` → `MERGE_READY` → `DEPLOYED` →
`VERIFIED` → `CLOSED`.

---

## RC-1 — Workstreams A–D

| ID | Sev | Discovered by | Affected SHA | Owner | Status |
|---|---|---|---|---|---|
| [A-1](#a-1) | HIGH | delta review (F1) | `9b906e8` | Opus 5 | REVIEW |
| [A-2](#a-2) | LOW | delta review (F2) | `9b906e8` | Opus 5 | REVIEW |
| [B-1](#b-1) | HIGH | go/no-go audit | `9b906e8` | Opus 5 | REVIEW |
| [B-2](#b-2) | MEDIUM | this program | `9b906e8` | Opus 5 | REVIEW |
| [C-1](#c-1) | HIGH | this program | `9b906e8` | Opus 5 | REVIEW |
| [C-2](#c-2) | MEDIUM | this program | `9b906e8` | Opus 5 | REVIEW |
| [C-3](#c-3) | MEDIUM | this program | `9b906e8` | Opus 5 | REVIEW |
| [C-4](#c-4) | LOW | **self-audit** | RC-1 | Opus 5 | REVIEW |
| [D-1](#d-1) | MEDIUM | go/no-go audit | `9b906e8` | Opus 5 | REVIEW |

### A-1
**The build's test report did not describe the whole suite.**
`run_tests.py` collected 794; pytest collected 799. The five it could not see
were `tests/test_calibration.py`'s — Brier, ECE, calibration curve — i.e. the
statistics the LIVE promotion rule turns on.

- **Root cause** — `unittest` collects only `TestCase` subclasses; pytest also
  collects module-level `test_*` functions. The suite uses both styles.
- **Patch** — `tests/_collect.py` adds the missing collection rule (no total is
  hard-coded); `run_tests.py` uses it and refuses to write a report when
  anything is collectable only by pytest.
- **Test evidence** — `tests/test_runner_parity.py` (7 tests) compares against
  the REAL pytest rather than a re-implementation, and asserts the collector
  actually adds something.
- **Mutation** — breaking `test_brier_and_ece_known_values`:
  at `9b906e8` → `EXIT=0`, report `794/0/0` **green — the mutation survived and
  the image would have shipped**. On RC-1 → `EXIT=1`, mutation killed.
- **Counts** — `CANONICAL=828 PYTEST=828 BUILD=828 COUNTS_MATCH=YES`;
  828 unique ids, no duplicates.
- **Deployment evidence** — pending merge.

### A-2
**A test asserted source-string order and broke on an unrelated refactor.**
`assertLess(src.index("_gates"), src.index("loader.discover"))` could be
satisfied by a comment, and failed the moment collection moved behind a helper.

- **Patch** — replaced with a behavioural check (importing `run_tests` in a
  scrubbed subprocess must already have applied the gate defaults) plus an
  independent AST check that the import is at module scope and precedes `main`.
- **Test evidence** — `tests/test_daily_quarantine.py`, 55 tests pass.

### B-1
**The kill switch did not cover the money path.**
`CFG.KILL_SWITCH` was read once per cycle in `execution_engine`, upstream of
`place_and_track`. Any caller reaching the money path by another route never
consulted it, and once a cycle began nothing re-read it.

- **Patch** — re-read in `place_and_track`, in the required order:
  global submission guard → **kill switch** → ticker well-formedness → daily
  quarantine → cooldown → pending intent → dedup → broker write.
- **Test evidence** — `tests/test_money_path_kill_switch.py`, incl. a control
  proving the same order succeeds with the switch off, per-order re-read, and
  both gate-precedence assertions.
- **Residual** — with `ALLOW_ORDER_SUBMISSION=false` in production the
  submission guard wins first, so the money-path kill switch has **zero runtime
  exercise today**. It cannot be VERIFIED from production logs until submission
  is enabled in DEMO.

### B-2
**A direct broker-write path bypassed the gate ladder.**
`kalshi_demo_execution_check.py` called `client.create_order` directly —
consulting no submission gate, no kill switch and no daily quarantine — and its
candidate list included the quarantined `KXBTCD` family.

- **Inventory** — `BROKER_WRITE_PATHS_TOTAL=5` call sites of 2 client methods:
  `order_manager` ×3 (gated), `kalshi_demo_execution_check` ×2 (ungated).
- **Patch** — a **client-layer backstop** in `KalshiClient.create_order`: the
  submission gate, kill switch and daily quarantine are re-checked at the one
  choke point no caller can route around, and no network request is emitted on
  refusal. `cancel_order` is deliberately NOT gated — cancelling reduces
  exposure, and a breaker that blocked it would trap an open order.
  `KXBTCD` removed from the probe's candidate series.
- **Result** — `PROTECTED_PATHS=5  UNPROTECTED_PATHS=0`.
- **Note on method** — the first version of the inventory test banned direct
  `create_order` calls outright. That is a proxy, not the property: the hazard
  is bypassing the *gates*, not calling the method. The test now asserts the
  property, and separately asserts the backstop still exists, so the sanctioned
  list cannot quietly become a list of unprotected paths.

### C-1
**`ALLOW_FRESH_STATE` was fail-open.** Read by the permissive `_env_b`,
`ALLOW_FRESH_STATE=flase` evaluated **True** — writing a fresh state marker on a
wiped volume, so the engine resumed on an empty ledger believing it was
continuous. This is the ledger-continuity gate.
- **Patch** — `_env_gate`, fail-closed.

### C-2
**`ALLOW_FALLBACK_CAPITAL` was fail-open.** Garbage evaluated True, sizing on a
configured rather than observed balance. DEMO-scoped (production refuses
outright), but it contradicts "never replace absent data with an invented
estimate."
- **Patch** — `_env_gate`, fail-closed.

### C-3
**`KILL_SWITCH` was read by the permissive parser** — and must NOT be migrated
naively. Its polarity is inverted: it *forbids*. `_env_gate(default=False)`
alone would have made a mistyped breaker fail **open**.
- **Patch** — `_env_gate(..., on_invalid=True)`: absent → not engaged (normal
  operation cannot require its presence); unreadable → **engaged**.

**Classification of every safety-relevant boolean**

| Variable | Before | Class | After |
|---|---|---|---|
| `ALLOW_ORDER_SUBMISSION` | `_env_gate` | FAIL_CLOSED | unchanged |
| `DAILY_RESEARCH_ORACLE_APPROVED` | `_env_gate` | FAIL_CLOSED | unchanged |
| `ALLOW_FRESH_STATE` | `_env_b` | **FAIL_OPEN_RISK** | `_env_gate` |
| `ALLOW_FALLBACK_CAPITAL` | `_env_b` | **FAIL_OPEN_RISK** | `_env_gate` |
| `KILL_SWITCH` | `_env_b` | FAIL_CLOSED *by polarity accident* | `_env_gate(on_invalid=True)` |
| `REQUIRE_PERSISTENT_STATE` | `_env_b` | FAIL_CLOSED (garbage → stricter) | unchanged |
| `SHADOW_MODE`, `DRY_RUN` | `_env_b` | FAIL_CLOSED (garbage → more restrictive) | unchanged |
| `CANCEL_UNFILLED_ORDERS`, `ONE_TRADE_PER_MARKET` | `_env_b` | FAIL_CLOSED | unchanged |
| `KELLY_ENABLED` | `_env_b` | NON_SAFETY — bounded by `min(MAX_POS_PCT, KELLY_MAX_POS_PCT)`, cannot exceed the 1 % hard cap | unchanged |
| cache/parallel/dashboard/alert flags | `_env_b` | NON_SAFETY | unchanged |

Ordinary preference parsing was deliberately left alone.

### C-4
**Self-audit finding: one input changed meaning in the permissive direction.**
`KILL_SWITCH=off` was **True** (engaged) under `_env_b`, because `"off"` was not
in its false-word list. Under `_env_gate` it is a recognised false word →
**False** (disengaged).

The new reading matches operator intent — the old behaviour engaged a breaker on
a word meaning "disengaged" — but it is the one input in this migration that
moves toward permissive, so it is recorded rather than buried. `KILL_SWITCH` is
absent from the production environment, so no deployed behaviour changes.
**Reviewer decision requested.**

### D-1
**The production build relied on RAILPACK *detecting* the Dockerfile.**
Detection is not a guarantee; nothing pinned it. A change that made another
builder look applicable could have produced an image with no test stage.
- **Patch** — `railway.json` pins `builder: DOCKERFILE`; `tests/test_build_pinning.py`
  also asserts the Dockerfile still runs the suite and still copies both
  gatekeeper artifacts.
- **Residual** — Railway dashboard settings may take precedence over
  `railway.json`. **Cannot be VERIFIED until a build runs post-merge**; the
  build log must be re-read for `BUILDER / DOCKERFILE_USED / TEST_STAGE_USED /
  TEST_REPORT_COPIED`.

---

## Open, not addressed in RC-1

| ID | Sev | Summary | Status |
|---|---|---|---|
| F-3 | LOW | `run_tests.py`, `tests/`, `_bootstrap.py` ship into the runtime image; no execution path reaches them | DISCOVERED |
| F-4 | LOW | `tools/restart_harness.py` runs in no CI step and neither Docker stage | DISCOVERED |
| H-1 | MEDIUM | `model_validation.json` is stale — generated 2026-09-01 at `acb9c01`; shadow settlements read 108 there vs **383** at runtime, and OOS Brier reads "non mesure" though the tooling and a negative result now exist. Verdict `approved:false` stays correct and conservative, but the artifact the LIVE gate reads is not a current picture | DISCOVERED |
| E-1 | BLOCKER | No PROD credentials exist; none may be created by the agent | BLOCKED — operator |
| E-2 | HIGH | No **env-keyed** read-only enforcement. The RC-1 backstop gates `create_order` on policy flags, but nothing refuses writes *because the environment is production*. Required before any LIVE credential is installed | DISCOVERED |

---

## Levels

| Level | State | Gate |
|---|---|---|
| 1 · Platform live-ready | **YES** (RC-1 pending review) | — |
| 2 · LIVE read-only ready | **NO** | E-1, E-2 |
| 3 · LIVE shadow ready | **NO** | Level 2; also the gatekeeper refuses prod start while `approved:false`, so LIVE shadow is unreachable without approving the model |
| 4 · LIVE canary tech ready | **NO** | Levels 2–3, B-1 runtime verification |
| 5 · Capital live | **NO** | all scientific gates + explicit operator authorization |

**Level 3 structural note.** `kalshi_alpha_bot.py` runs `check_live_allowed()`
for any production run that is not `--scan-only`/`--rank-only` and exits 1 on
failure. `--shadow` does not bypass it. So "LIVE shadow with decisions and risk
results" cannot be reached without `MODEL_APPROVED=true` — which is forbidden.
Either the gatekeeper grows a genuine read-only mode it can pass while
unapproved, or LIVE shadow waits for the model. **This is a design decision for
the operator, not something to route around.**
