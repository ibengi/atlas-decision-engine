# Safety review — PROD READ_ONLY full shadow observation

**Candidate** `054a2e1a8130b45447ae57ce48fab36413d7ee1c`
(branch `claude/railway-atlas-readonly-shadow-scan`, parent `d541753` = current `main`)
**Scope of the verdict** READ_ONLY SHADOW ONLY. Nothing here authorizes CAPITAL
mode, a model promotion, or any real order.
**Reviewed at** 2026-09-06, against the deployed production state
(deployment `eb1dfd55`, `PROD_ACCESS_MODE=READ_ONLY`, reconciliation MATCH
broker 0 / local 0, broker writes 0).

## INDEPENDENCE CAVEAT — READ THIS FIRST

This review was performed by the session that **authored** the candidate. It is
therefore **not structurally independent**, and under the model router charter
(the author of a change may never be its only reviewer) it does not on its own
satisfy `SECOND_REVIEW_REQUIRED=YES`. The precedent matters: on `8947da7` this
same session returned `MERGE_READY=YES` and the non-authoring reviewer then
found a real fourth false-green door. Everything below is reproducible from the
commands recorded in it; none of it should be accepted on this session's word.

## Method

Nothing was taken from the commit message, the docstrings or the candidate's own
tests. Every claim below was re-derived by executing code in a detached worktree
at the candidate SHA.

## 1. The diff

Two files, 605 insertions, 12 deletions. `execution_engine.py` and one new test
file. No other production module is touched — not `config.py`, not
`kalshi_client.py`, not `risk_manager.py`, not `kalshi_alpha_bot.py`. No
threshold constant, gate flag or credential appears on a changed production
line. No PnL reset, no state deletion, no historical-trade edit, no fabricated
balance.

The relaxation is a single predicate:

```python
def _guard_is_observation_only(self, guard) -> bool:
    if guard not in CAPITAL_RISK_GUARDS:
        return False
    return self.client.env != "demo" and prod_is_read_only()
```

Both conditions are required. Where it returns `False` the `else` branch is
byte-identical to the pre-change code, so **CAPITAL and DEMO execute the exact
original control flow**.

### Guard vocabulary — measured, not asserted

Every branch of the real `_post_balance_gates()` was driven to enumerate the
guard strings it can actually return:

| reachable guard | relaxed in PROD READ_ONLY? |
|---|---|
| `equity_drawdown` | yes |
| `max_open_positions` | yes |
| `daily_loss_stop` | yes |
| `consecutive_loss_breaker` | yes |
| `max_trades_cycle` | yes |
| `open_risk_budget` | yes |
| `persistence_failure` | **no — still stops the cycle** |
| `contract_cap_invalid` | **no** |
| `reconciliation_mismatch` | **no** |
| `reconciliation_unknown` | **no** |
| `reconciliation_broker_unavailable` | **no** |
| `risk_can_trade_unclassified` | **no** |

The relaxed set equals `CAPITAL_RISK_GUARDS` exactly: no declared entry is
unreachable, and no reachable guard is relaxed by accident. `kill_switch` and
`balance_gate` are evaluated *earlier* than the relaxation point and were
proven, by running the real `_cycle_sequential` and `_cycle_parallel`, to still
return 0 before any scan.

### Adversarial mode matrix

The predicate was evaluated under eleven `PROD_ACCESS_MODE` readings —
`READ_ONLY`, `read_only`, `ReAd_OnLy`, absent, empty, whitespace, `READ0NLY`,
arbitrary garbage, `CAPITAL`, `capital`, `  CAPITAL  `. In all eleven:

* DEMO was never relaxed;
* an integrity guard was never relaxed;
* the predicate agreed with `prod_is_read_only()` in every case, so both
  spellings of CAPITAL correctly switch the relaxation **off**.

Unknown, blank, `None` and wrong-case guard names (`"EQUITY_DRAWDOWN"`) all fail
closed.

## 2. Write boundary, at the real transport

`_req` was **not** stubbed. A real `KalshiClient` in `env="prod"` was driven with
`ALLOW_ORDER_SUBMISSION`, `LIVE_BROKER_WRITES_AUTHORIZED`, `LIVE_TRADING`,
`LIVE_TRADING_CONFIRMED`, `MODEL_APPROVED` and `DAILY_RESEARCH_ORACLE_APPROVED`
all set true, with the socket layer replaced by a recorder that fails the probe
if it is ever handed a request.

Refused with `BrokerWriteForbidden` in READ_ONLY, mode-absent, `READ0NLY`,
empty, `read_only` and `Read_Only` — 21 operations × 6 modes = 126 attempts,
126 refusals, **zero requests reached the socket layer**:

* `create_order`, `cancel_order`
* `POST`, `PUT`, `PATCH`, `DELETE`, and their lowercase and whitespace-padded forms
* `b"POST"`, `b"DELETE"`, `bytearray(b"PATCH")` — the bytes-verb bypass
* `None`, `7`, `""`, `"FROBNICATE"` — unclassifiable verbs, all treated as writes
* `/orders/{id}/amend`, `/orders/batched`, `/positions`, `/orders/{id}/decrease`

Anti-vacuity control: under `PROD_ACCESS_MODE=CAPITAL` the *same* 21 operations
stop raising `BrokerWriteForbidden` and instead fail later, at the RSA-key
check. The read-only refusals are therefore the policy gate, not an artefact of
the harness.

The new exposure — `_execute_decision` is now reached on a drawdown-blown cycle,
which never happened before — terminates at the `WOULD_SUBMIT` branch, which
returns **before** `claim_half_open_attempt`, so no circuit-breaker state is
consumed either. Newly reachable side effects in `_finish_cycle` are local JSON
writes and log lines only.

## 3. Tests

| runner | tests | failures | errors |
|---|---|---|---|
| `run_tests.py` (release gate, writes `test_report.json`) | 970 | 0 | 0 |
| `pytest` | 970 (+228 subtests) | 0 | 0 |
| dedicated `test_readonly_observation_scan.py`, unittest | 21 | 0 | 0 |
| dedicated, pytest | 21 | 0 | 0 |

Counts agree between runners; neither is hard-coded.

## 4. Mutations — 14 killed of 16

Each mutation was applied to a fresh copy and run against the **full release
runner**. Killed: dropping `equity_drawdown` from the relaxed set (anti-vacuity);
adding `kill_switch`, `reconciliation_mismatch` or `risk_can_trade_unclassified`
to it; dropping the guard-membership test; dropping the environment test;
inverting the mode test so CAPITAL scans through a cut; returning `True`
unconditionally; removing the blocking `else` on each cycle path; hard-coding
the evidence field to `None`; removing the `WOULD_BLOCK_CAPITAL` report;
inverting and deleting the `WOULD_SUBMIT` branch.

**Two survived**, both non-safety:

* **MED-1 — `_finish_cycle` drops `would_block_capital=`.** All 970 tests stay
  green while the durable evidence row silently loses the record that CAPITAL
  would have refused the cycle. The wiring is *correct* on the candidate — the
  shipping recorder was run and does write the field — but nothing pins the
  seam: `TheEvidenceRowCarriesTheReportedGuard` tests the recorder in isolation
  and `TheCycleObservesInsteadOfStopping` stubs `_finish_cycle`. The operator's
  "CALCULATED and REPORTED" requirement is met by the code and undefended by the
  suite. The `WOULD_BLOCK_CAPITAL` log line is separately pinned, so the fact is
  not lost twice over.
* **LOW-1 — `!= "demo"` rewritten as `== "prod"`.** The docstring claims the
  predicate uses "the exact formulation the write boundary already uses, so the
  two cannot drift"; no test pins that. The mutation is the *safer* direction
  (an unrecognised environment stops being relaxed) and is unreachable today,
  since the entrypoint sets `env` to exactly `"demo"` or `"prod"`.

## 5. Other findings

* **MED-2 — `WOULD_SUBMIT` will read zero in production at the current balance,
  and that is not a regression.** With the shipped defaults the sizer returns 0
  contracts below roughly $50 of capital (measured: $0.04 → 0, $10 → 0, $50 → 1
  at 43c). On the live $0.04 account a candidate therefore dies at
  `count <= 0 → risk_blocked` *before* the `WOULD_SUBMIT` branch, so that
  counter stays at 0 after deploy. The **scientific** shadow stream is
  unaffected and is fully restored, because `_shadow_observer` is called from
  inside `opportunity_pipeline.run_cycle` for every evaluated BTC candidate —
  before sizing and independent of it. Expect after deploy:
  `scan_executed=true`, funnel counters, model probabilities, edge/EV,
  `shadow_predictions.json` and the daily evidence store all populated;
  `would_submit` still 0. Phase 10 has its data; the execution-level line does
  not move until capital does.
* **LOW-2 — the mode is read at two moments.** `_guard_is_observation_only` and
  the `WOULD_SUBMIT` branch each call `prod_is_read_only()` independently. A
  flip from READ_ONLY to CAPITAL *between* them would let a cycle that was
  relaxed past a capital guard reach the write path. Confirmed unreachable: the
  only production writer of `PROD_ACCESS_MODE` is in `main()` at startup, before
  the loop. A one-line re-assertion in `_finish_cycle` — refuse to execute any
  decision when `would_block_capital is not None` — would make the invariant
  local rather than temporal, and would also kill MED-1's mutation.

## Verdict

```
DIFF_SAFE=YES
READONLY_MODE_ISOLATED=YES
DEMO_UNCHANGED=YES
CAPITAL_UNCHANGED=YES
WRITE_BOUNDARY_HARD=YES

CREATE_ORDER_IMPOSSIBLE=YES
CANCEL_ORDER_IMPOSSIBLE=YES
OTHER_MUTATIONS_IMPOSSIBLE=YES

PERSISTENCE_FAILURE_FAIL_CLOSED=YES
RECONCILIATION_FAILURE_FAIL_CLOSED=YES
KILL_SWITCH_FAIL_CLOSED=YES
UNKNOWN_GUARD_FAIL_CLOSED=YES

SEQUENTIAL_PATH=PASS
PARALLEL_PATH=PASS
FULL_TESTS=970
FAILURES=0
MUTATIONS_KILLED=14/16

BLOCKERS=0
HIGH=0
MEDIUM=2
```

`054A2E1_APPROVED_FOR_PROD_READONLY_SHADOW`

Approval means READ_ONLY SHADOW ONLY. It does not authorize CAPITAL mode or any
real order. It is also **subject to the independence caveat at the top**: this
verdict comes from the change's author and should be corroborated by the
non-authoring reviewer before merge.
