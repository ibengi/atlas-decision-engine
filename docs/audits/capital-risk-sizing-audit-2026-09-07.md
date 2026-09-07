# Capital / risk / position-sizing audit — PROD READ_ONLY, 2026-09-07

Scope: why Atlas sizes 0 contracts with ~$9.84 of effective capital; unit audit;
risk invariants; drawdown accounting; deposit effect; READ_ONLY, daily-oracle
and model-gate invariants; PR #64 interaction. Audited tree: `main` `d0995a1`
(deployed as `471b446f`). Nothing was merged, deployed, or changed on Railway.

## Production facts used

```
13:34:28Z [RAW:balance] {"balance": 3, "balance_dollars": "0.0396", ...}   -> capital 0.04$
18:01:19Z [CAPITAL] solde=0.04$   Drawdown 1222.0% (0.48$) >= 20% -- trading coupe.   cycle=264
18:02:20Z [CAPITAL] solde=9.84$   (deposit; no trade between the two lines)
18:02:25Z [CYCLE_EVIDENCE] blocking_global_guard=None scan_executed=True cycle=0
18:03:27Z [CANDIDAT] KXBTC15M-26SEP071415-15 NO @ 57c edge_net=+0.083 ev_net=+0.093 conf=9
18:03:30Z [REJECT]   KXBTC15M-26SEP071415-15: risk_blocked (taille=0)
          rejections_by_reason: risk_blocked=1, daily_oracle_unapproved=2  orders_submitted=0 fills=0
Railway service config: startCommand = "python read_only_dashboard_bootstrap.py"
Config (repo defaults; Railway variable NAMES only were read):
  MAX_POSITION_PCT=1.0  RISK_BUDGET_PCT=5.0  DD_THROTTLE_PCT=10.0  MAX_EQUITY_DRAWDOWN_PCT=20.0
  MAX_DAILY_LOSS_PCT=5.0  KELLY_ENABLED=False  FEE_RATE=0.07  SLIPPAGE_BUFFER_CENTS=1
```

## The complete calculation, one real candidate

`KXBTC15M-26SEP071415-15`, side NO, ask **57c**, confidence **9**, effective
capital **$9.84**, rolling drawdown **$0.4839**, open risk **$0**.

| step | code | value |
|---|---|---|
| broker balance | `KalshiClient._fetch_balance`: `balance_dollars` preferred, else `balance`(cents)/100 | $9.84 |
| effective capital | `_balance_gate`: `min(configured_capital=500, bal)` → `self.capital`, `self.risk.capital` | $9.84 |
| pre-scan gates | `_post_balance_gates`: dd% = 100·0.4839/9.84 = 4.92 < 20 → pass | pass |
| candidate gates | `price_and_gate`: gross = p_model − 0.57; fee = ceil(0.07·0.57·0.43·100)/100 = **$0.02**; slip = $0.01; net_edge = gross − 0.02 − 0.01 − 0.01 | +0.083 |
| taille | `dec.taille = "1%" if confidence >= 8 else "0.5%"` | "1%" |
| entry price | `_execute_decision`: `entry = int(book["no_ask"])` (cents, 1–99) | 57 |
| emergency / concentration | `portfolio_check(…, 0.0)`: daily stop = min(50, 9.84·5%) = $0.49, pnl today 0 → pass; category 3% and single-market 1% on $0 open → pass | pass |
| per-position % | `_legacy`: `pct = min(1.0, MAX_POS_PCT=1.0)`; conf 9 > 4 → no halving; dd/cap = 4.92% < 10 → no halving | 1.0 % |
| risk budget left | `9.84 × 5% − 0` | $0.4920 |
| allocation | `min(9.84 × 1%, 0.4920)` | **$0.0984** |
| raw contracts | `0.0984 / 0.57` | 0.1726 |
| integer floor | `int(0.1726)` | **0** |
| drawdown factor | `drawdown_size_factor()` = 1.0 (PORTFOLIO_DRAWDOWN_THROTTLE_PCT=0) | 0 |
| cap | `min(MAX_CONTRACTS_PER_ORDER, 0)` | 0 |
| outcome | `count <= 0` → `[REJECT] risk_blocked (taille=0)`, `rejections.risk_blocked += 1`, return 0 — **before** the WOULD_SUBMIT branch | size 0 |

Every observed candidate that day (48, 53, 55, 57, 59, 60, 62, 77c, all conf 9)
gives the same result. One contract at 1 % needs `capital ≥ price_cents / 1.0`,
i.e. **$48–$77** for these prices; at 57c, **$57.00** (measured: first capital
producing 1 contract at 57c is exactly $57.00).

## Test matrix (real `PositionSizer.contracts`, legacy path, taille 2 %→capped 1 %, conf 8, dd $0.4839)

Final contract count by capital × price. `-` = 0, reason `risk_blocked (taille=0)`.

| capital | 1c | 5c | 10c | 25c | 50c | 60c | 84c | 95c | 99c | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| $0.03 | - | - | - | - | - | - | - | - | - | dd 1613 % → guard blocks before scan; pct halved |
| $0.50 | - | - | - | - | - | - | - | - | - | dd 97 % → blocked; pct halved |
| $1.00 | - | - | - | - | - | - | - | - | - | dd 48 % → blocked |
| $2.50 | 1 | - | - | - | - | - | - | - | - | dd 19.4 % → passes guard; ≥10 % → pct halved to 0.5 % |
| $5.00 | 1 | 1 | - | - | - | - | - | - | - | |
| $9.84 | 1 | 1 | - | - | - | - | - | - | - | **production** |
| $10.00 | 1 | 1 | 1 | - | - | - | - | - | - | boundary: 10c affordable at exactly $10.00 |
| $25 | 1 | 1 | 1 | 1 | - | - | - | - | - | |
| $50 | 1 | 1 | 1 | 1 | 1 | - | - | - | - | |
| $100 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | |
| $500 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | capped by MAX_CONTRACTS_PER_ORDER=1 |

Boundaries: at $9.83/9.84/9.85/9.99 → 9c:1, 10c:0; at $10.00/10.01 → 10c:1,
11c:0. The floor is exact and monotone. Kelly path (disabled in prod) gives 0 at
$9.84 for every price because `KELLY_MIN_BET=$1` requires `budget_left ≥ $1`
and the budget is $0.49.

Estimated taker fee for 1 contract: 1c→$0.01, 5c→$0.01, 10c→$0.01, 25c→$0.02,
50c→$0.02, 60c→$0.02, 84c→$0.01, 95c→$0.01, 99c→$0.01.

## Findings

### F1 — BLOCKER (scoped to PR #64) — `fix/read-only-drawdown-observation` fails the release gate and cannot deploy
- **File/function:** `tests/test_read_only_drawdown_observation.py` (5 pytest-style functions taking `monkeypatch`); `read_only_dashboard_bootstrap.py` monkeypatches `ExecutionEngine._post_balance_gates` / `_finish_cycle`.
- **Exact defect:** the five tests declare fixture parameters, which `tests/_collect.py` refuses (`declares parameters`). `run_tests.py` exits 1 before running anything; the Dockerfile's `RUN python run_tests.py` therefore fails, so the branch is not buildable. Additionally: (a) the patched gate returns `(True, None)`, so the durable `cycles.jsonl` row records `blocking_global_guard=None`, indistinguishable from a clean cycle — the CAPITAL-would-block fact survives only in `dashboard_state.json` and the in-memory report; (b) `self._read_only_capital_guard` is set in the gate and cleared only in `_finish_cycle`'s `finally`, so an exception between the two leaks it into the next cycle; (c) the wrapper's `_finish_cycle(self, n, res, execution_path="sequential")` is arity-incompatible with `0040e2b`'s required five-argument signature — merging both would raise `TypeError` on every completed cycle in production; (d) the tests exercise `SimpleNamespace` stubs with the originals monkeypatched away, so the real cycle path is never run.
- **Sizing/risk interaction:** none that relaxes risk. `drawdown_size_factor()` and `_legacy`'s `DD_THROTTLE_PCT` halving still apply; at $0.04 the sizer still returns 0. Today the guard no longer fires, so the patch is inert.
- **Evidence:** `run_tests.py` on the branch → `RUNNER_EXIT=1`, 5 × `declares parameters`; `pytest tests/test_read_only_drawdown_observation.py` → 5 passed (pytest-only green = exactly the runner-parity defect class closed earlier).
- **Correction:** convert the tests to `unittest.TestCase` (the repository convention the collector enforces) and run the real cycle; carry the guard into the durable row (`0040e2b` does this via `would_block_capital`); do not merge both approaches.

### F2 — HIGH — Drawdown and every %-of-capital limit use current broker cash as the denominator, so deposits and withdrawals change the risk state with no trading
- **File/function:** `risk_manager.py` `rolling_drawdown_pct()` = `100 · rolling_drawdown() / self.capital`; `effective_daily_stop()` = `min(MAX_DAILY_LOSS, capital · 5 %)`; `_legacy` throttle `drawdown / capital`; `execution_engine._balance_gate` sets `capital = min(configured, live balance)` every cycle.
- **Exact defect:** `rolling_drawdown()` is a peak-to-trough on *cumulative settled net PnL* (correct, in dollars) but is normalised by *today's cash*, which includes external flows. There is no deposit/withdrawal ledger anywhere in the code (grep: none). Consequently the same trading history reads as 1222 % drawdown at $0.04 and 4.92 % at $9.84.
- **Evidence:** production at 18:02:20Z; probe with the ledger held fixed at −$0.4839: guard BLOCKs up to $2.41, passes from **$2.42** (= 0.4839/0.20); size throttle halves up to $4.84; daily stop is **$0.49** at $9.84 (one 50c contract loss trips it), $2.50 at $50.
- **Financial impact:** a deposit un-trips a loss guard that fired on real losses; a withdrawal can trip it with no losses; at small cash the daily-stop and consecutive-loss regime collapses to "first loss halts". Direction today: the deposit *relaxed* a guard — that is the unsafe direction.
- **Correction (design, not a patch — do not reset history):** see "Drawdown accounting design" below.

### F3 — MEDIUM — Every completed cycle is recorded as `cycle=0` in the durable evidence
- **File/function:** `execution_engine.py` `_finish_cycle`, funnel-conversion loop `for name in (...): n = int(report.get(name) or 0)` rebinds the method parameter `n`; the last key is `"fills"`, so `n` ends as the fill count (0).
- **Exact defect:** `summary["cycle"] = n`, `JsonStore.save(cycle_report.json, {"cycle": n, ...})`, `dashboard_state.json["cycle"] = n`, `pipeline_stats.json["cycle"] = n` and `_record_cycle_evidence(n, ...)` all receive the clobbered value. Blocked cycles (which return before this loop) number correctly (…262, 263, 264); completed ones are all 0 — exactly what production shows since 18:02Z. Once real fills exist, "cycle" would equal the number of fills.
- **Evidence:** reproducer — real `_finish_cycle(eng, 123, res, "sequential")` → evidence row `cycle=0`, `[CYCLE-SUMMARY] "cycle": 0`. Never visible before today because no PROD cycle had ever completed.
- **Financial impact:** none directly; evidence rows lose their unique key, and the dashboard cycle counter is wrong. Undermines the durable record the READ_ONLY shadow exists to produce.
- **Correction:** rename the loop variable (diff below). **Required test:** run the real `_finish_cycle` with `n=123` and assert the durable row, `cycle_report.json` and the summary all carry 123.

### F4 — LOW — The 1 % per-position cap is applied to premium only; the taker fee can push max loss above the cap at exact-affordability boundaries
- **File/function:** `position_sizer._legacy` (`alloc / price`), `execution_engine._execute_decision` (`proposed_risk = count·entry/100`; `est_fee_total` computed *after* the risk gates and used for logging only).
- **Evidence:** $10.00 / 10c → 1 contract, premium $0.10 = cap, fee $0.01 → max loss **$0.11 > $0.10**; $9.84/9c → $0.10 > $0.0984; $25/25c → $0.27 > $0.25; $50/50c → $0.52 > $0.50. Overshoot ≤ $0.02 per contract (1–4 bp of capital). Above the boundary the slack absorbs it.
- **Correction (policy decision):** include the estimated fee in affordability: size against `alloc − fee(1, price)` or check `count·price/100 + fee(count, price) ≤ cap`. Not applied here because it is a policy tightening, and with `MAX_CONTRACTS_PER_ORDER=1` the exposure is bounded by cents.

### F5 — LOW — `PositionSizer.full_kelly` silently reinterprets a probability > 1 as a percentage
- `if p_yes > 1.0: p_yes /= 100.0`. A caller passing 62 (meaning 62 %) or a corrupt 1.5 gets a silent rescale instead of a refusal. Kelly is disabled in production; the legacy path does not use probability. Recommend: return 0 for any value outside [0, 1].

### F6 — LOW — The test suite is sensitive to an operator's shell environment
- With `MAX_CONTRACTS_PER_ORDER=1` exported, 3 tests fail under both runners (`test_kelly_sizing` ×2, `test_pipeline_integration.test_partial_fill_counts_real_quantity`); unset, 949/949 pass. `tests/_gates.py` pins gate defaults but not this variable. Not a production defect; it can make a local run disagree with the Docker build. Recommend pinning it in `tests/_gates.py`.

### F7 — LOW — Runtime entrypoint is defined outside the repository
- Railway `startCommand = python read_only_dashboard_bootstrap.py`; the repo's `Dockerfile` CMD and `Procfile` still say `kalshi_alpha_bot.py --loop --live-read-only`. The service also reports builder RAILPACK while `railway.json` pins DOCKERFILE (config-as-code wins at build time, established earlier). PR #64 relies on the wrapper being the entrypoint; nothing in the repo guarantees it. Recommend the repo own the entrypoint.

### F8 — INFO — Sequential vs parallel paths
- Identical sizing arithmetic and gates. The parallel path sizes on a balance fetched *before* the scan (a few seconds staler). Production runs `path=sequential`. No divergence found between shadow and CAPITAL sizing: the WOULD_SUBMIT branch is *after* sizing, so shadow reports the size CAPITAL would have used.

## Verified correct (evidence, not absence of failure)

- **Units.** Balance: `balance_dollars` string preferred ("0.0396"), else `balance` int cents / 100 — the raw response shows `"balance": 3` (truncated cents) alongside `"balance_dollars": "0.0396"`, so preferring the dollars field is right. Prices: `read_price` reads cents first and only treats `*_dollars` as dollars; `normalize_book` clamps to 1–99. Sizing divides by `price/100`. Fees: `ceil(0.07·p·(1−p)·100)/100` in dollars, once, in `price_and_gate` (EV/edge) and once for logging after sizing — never subtracted twice. Slippage: 1c as $0.01, EV only. `open_risk` = Σ count·avg_price/100 in dollars. No cents/dollars or 0–1/0–100 mismatch on the money path.
- **No double application of a risk %.** Per-position (`min(taille, MAX_POS_PCT)`) and portfolio budget (`RISK_BUDGET_PCT − open_risk`) are two different limits combined by `min`; category/single-market/portfolio checks are on *open* risk, not on the new allocation twice.
- **Configured vs broker capital.** `min(500, balance)`; `test_sizing_small_account` pins that 500 never wins.
- **Zero size is fail-closed and observable:** `[REJECT] … risk_blocked (taille=0)` + `rejections.risk_blocked` counter + `risk_passed=0`; no order path reached. `test_zero_when_unaffordable` asserts this behaviour explicitly ("honnetement (on ne force pas le trade)").
- **READ_ONLY isolation** re-measured on `d0995a1` with the real transport: 21 write operations × 6 read-only-ish mode readings, every trading flag armed → 126 `BrokerWriteForbidden`, **0 requests at the socket**; CAPITAL control progresses past the policy gate to the key check.
- **Daily oracle gate:** `DAILY_RESEARCH_ORACLE_APPROVED` read by strict `_env_gate` (unreadable → closed); production logs show `[DAILY_QUARANTINE] … aucun ordre` on every KXBTCD candidate. Not recommending enablement: `model_validation.json` criteria remain unmet.
- **Model gate independent of sizing:** `model_validation.json` `approved=false`; `check_live_allowed` needs `MODEL_APPROVED_FOR_LIVE=YES` and a fresh approved artifact; no sizing code reads either.
- **Contract cap fail-closed:** invalid/absent `MAX_CONTRACTS_PER_ORDER` → `contract_cap_invalid` global guard *and* sizer returns 0.

## Drawdown accounting design (F2)

Keep the settled-trade ledger untouched. Add one persisted ledger and derive four quantities from it:

```
broker_cash            live balance each cycle (as today)
external_flow_t        broker_cash_t − broker_cash_{t−1} − Σ net_pnl settled in (t−1, t]
                       classified as deposit (>0) / withdrawal (<0) when |·| > ε, else 0
strategy_equity_t      strategy_equity_{t−1} + Σ net_pnl settled in (t−1, t]     (deposits/withdrawals never touch it)
                       seeded once = first observed broker_cash − pending open risk
risk_equity_reference  running max of strategy_equity (high-water mark)
drawdown_pct           100 · (risk_equity_reference − strategy_equity) / risk_equity_reference
```

- Equity guard, size throttle, consecutive-loss and daily-stop *percentages* use `strategy_equity` / `risk_equity_reference`, never `broker_cash`.
- Affordability (per-position $ allocation, budget $) keeps using `min(configured, broker_cash)` — money you do not have cannot be allocated — but a deposit cannot lower drawdown and a withdrawal cannot create it.
- Persist `equity_ledger.json` rows `{ts, broker_cash, settled_pnl_delta, external_flow, strategy_equity, hwm}` under `DATA_DIR`; a gap in observation is recorded as `unclassified` and fails closed (guard stays blocked) until an operator classifies it.
- Migration: seed `strategy_equity` from the existing ledger so that today's −$0.4839 remains a drawdown against the *original* stake, not against $9.84.

## Minimal safe patch (confirmed defect only)

Only F3 is a confirmed software defect on the sizing/evidence path that a code change should fix now. No risk percentage is raised.

```diff
--- a/execution_engine.py
+++ b/execution_engine.py
@@ def _finish_cycle(self, n: int, res: dict,
         conv = report.get("funnel_conversion") or {}
         prev = None
         for name in ("scanned_raw", "open_cached", "liquid", "supported",
                      "model_evaluated", "positive_edge", "positive_net_ev",
                      "risk_passed", "orders_submitted", "fills"):
-            n = int(report.get(name) or 0)
-            conv[name] = {"n": n,
-                          "pct_of_prev": round(100.0 * n / prev, 2)
-                          if prev else (100.0 if n else 0.0)}
-            prev = n if n else prev
+            stage_n = int(report.get(name) or 0)
+            conv[name] = {"n": stage_n,
+                          "pct_of_prev": round(100.0 * stage_n / prev, 2)
+                          if prev else (100.0 if stage_n else 0.0)}
+            prev = stage_n if stage_n else prev
         report["funnel_conversion"] = conv
```

Required test (real method, durable sink):

```python
def test_completed_cycle_keeps_its_number(self):
    eng = _FullCycleEngine()                 # real _finish_cycle, real _record_cycle_evidence
    ExecutionEngine._finish_cycle(eng, 123, _FullPipeline().run_cycle(), "sequential")
    row = eng.cycles_jsonl.rows[-1]
    self.assertEqual(row["cycle"], 123)
    self.assertEqual(JsonStore.load(_p("cycle_report.json"), {})["cycle"], 123)
```

Optional, policy-dependent (F4): size against `alloc − FeeModel.trading_fee(1, price)` in `_legacy`; requires an explicit decision that the 1 % cap is fee-inclusive.

## Commands and results

```
python3 -m pytest -q -p no:randomly tests/test_sizing_small_account.py tests/test_kelly_sizing.py \
  tests/test_portfolio_limits.py tests/test_contract_cap.py tests/test_daily_quarantine.py \
  tests/test_money_path_kill_switch.py tests/test_prod_access_mode.py tests/test_pre_live_gate_matrix.py
  -> 156 passed, 40 subtests passed
python3 run_tests.py            (main d0995a1)  -> Discovered 949 (944+5); Ran 949; OK; exit 0
python3 -m pytest -q            (main d0995a1)  -> 949 passed, 228 subtests passed
python3 run_tests.py            (PR #64 aa34193) -> exit 1; CRITICAL: 5 tests collectable only by pytest
python3 -m pytest tests/test_read_only_drawdown_observation.py (PR #64) -> 5 passed
probes/p_writeboundary.py       (main d0995a1)  -> 126 refusals / 126, SOCKET LAYER REACHED: []
sizing matrix + boundaries + Kelly reference   -> table above (real PositionSizer, real FeeModel)
deposit-effect probe (fixed ledger −0.4839)    -> guard clears at $2.42, throttle at $4.84, daily stop $0.49 @ $9.84
cycle-number reproducer (n=123)                -> row cycle=0, summary cycle=0  (BUG CONFIRMED)
```

A note on method: an earlier run of the same suites with `MAX_CONTRACTS_PER_ORDER=1`
exported in the shell reported 3 failures; that was environment leakage (F6),
not `main`. The clean numbers above are authoritative.

## Verdict

**A. Root cause of size = 0.** `count = int(min(capital × min(taille, MAX_POSITION_PCT=1 %), capital × 5 % − open_risk) / price)`; with $9.84 the per-position allocation is $0.0984, below the premium of every observed candidate (48–77c), so the integer floor is 0. The configured 1 %-of-effective-capital policy cannot buy one contract priced above 9c until capital ≥ price/1 % ($48–$77 here).

**B. Expected size with $9.84 under the existing policy:** 0 for every candidate observed (48, 53, 55, 57, 59, 60, 62, 77c → 0; the two KXBTCD candidates are quarantined before sizing). 1 contract at 57c requires $57.00.

**C. BUG or POLICY:** **INTENDED POLICY.** The arithmetic is exact, unit-correct, tested (`test_zero_when_unaffordable`), and fail-closed. The bugs found (F1, F3, F4, F5, F6) are real but none of them produces the 0.

**D. Minimal safe patch:** the F3 diff above. Nothing else should change to "make a trade possible"; raising `MAX_POSITION_PCT` or funding the account is not a software correction.

**F. Capital safety verdict**

```
POSITION_SIZING_CORRECT=YES
MONEY_UNITS_CORRECT=YES
DRAWDOWN_ACCOUNTING_CORRECT=NO
READ_ONLY_ISOLATION_CORRECT=YES
SMALL_ACCOUNT_BEHAVIOR_CORRECT=YES
DAILY_ORACLE_GATE_CORRECT=YES
MODEL_GATE_PRESERVED=YES

SENIOR_AUDIT_VERDICT=FAIL
```

FAIL is driven by F2: the risk-accounting layer that the audit is about lets an
external deposit relax a guard that fired on real losses, demonstrated in
production today, and by F3, a confirmed defect in the durable evidence the
READ_ONLY shadow exists to produce. Capital itself is not at risk: the write
boundary is intact, the sizer is honest, and the daily oracle and model gates
hold.
