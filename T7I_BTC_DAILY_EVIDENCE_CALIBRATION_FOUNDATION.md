# T7-I — BTC Daily Evidence & Calibration Foundation

**Date:** 2026-08-15 · **Scope:** instrumentation only. No scanner horizon, no
`SCANNER_LOOKAHEAD_HOURS`, no `SCANNER_GENERAL_CRAWL`, no `target_series()`, no
liquidity rule, no gate, no `MIN_MODEL_CONFIDENCE`, no edge/EV threshold, no
risk limit, no `MAX_TRADES_CYCLE`, no `MAX_OPEN_POSITIONS`, no new category, no
sports/election provider, no strategy or momentum mathematics, no probability
or confidence output, no position sizing. No order placed. No Railway variable
touched. No deploy. No merge.

**Provenance labels are never merged.** `MEASURED` = produced in this session by
executing repository code. `ATTESTED` = operator-side Railway output or a
repository document. `INFERRED` = reasoned from those, always labelled.
`UNKNOWN` = not determinable here; never converted into a conclusion.

---

## 1. Executive verdict

> ### EVIDENCE FOUNDATION PARTIALLY READY

The observability gap is **proven, not suspected**, and it is **ACCIDENTAL in
consequence**. A minimal, strictly additive patch is implemented on the feature
branch, with all ten invariants tested and the full regression suite showing a
**byte-identical failure set** before and after.

It is *partially* ready, not ready, for one honest reason: **the loop will
record nothing in the current DEMO state.** Evidence accrues only when a KXBTCD
market is both liquid and inside the 24 h scanner window, and T7-H measured
**zero** such markets — all 378 liquid KXBTCD markets sit in the `>14d` bucket.
The mechanism is complete and correct; the data flow is starved upstream by a
condition this phase is explicitly forbidden to change.

Two further items keep it from "READY": acceptance thresholds for the daily
model require operator approval (§9), and the repository's documented
validation procedure references code that does not exist (§10, F-3).

---

## 2. Phase 0 — Source and production identity

| Item | Value | Class |
|---|---|---|
| Repository | `ibengi/atlas-decision-engine` | MEASURED |
| Branch | `claude/market-universe-scanner-audit-4s50oq` | MEASURED |
| HEAD at start | `5e5b74f` | MEASURED |
| `origin/main` | `abef2c616436d1c42fae3e2855076bdf01db8f66` | MEASURED |
| P0 `92f14a9` | verified **ancestor** of `origin/main` | MEASURED |
| Working tree at start | clean | MEASURED |
| Railway deployed commit | **UNKNOWN** — not observable from this session | UNKNOWN |

T7-F/G/H artifacts are **present locally** (`T7F_…md`, `T7G_…md`, `T7H_…md`,
`t7g_universe_probe.py`, `t7h_kxbtcd_horizon_probe.py`) and **absent from
`origin/main`** — the three audit commits remain unpushed (§11). No phase of
T7-I depends on them being merged. No unexplained source drift.

---

## 3. Phase 1 — Evidence pipeline coverage matrix

MEASURED by tracing executable paths, not names.

| Stage | Source | Durable sink | Record id | btc15m | btc_daily |
|---|---|---|---|---|---|
| Discovery | `market_scanner.scan_cycle` | — (in-memory) | ticker | ✅ | ✅ |
| Scanner rejection | `market_scanner._excl` → `opportunity_pipeline._write_funnel_rejections` | `funnel_rejections.jsonl` | cycle_id + ticker | ✅ | ✅ |
| Classification | `market_classifier.classify` | via decision row | ticker | ✅ | ✅ |
| Model evaluation | `strategy_router._BtcAboveStrikeBase.evaluate` | via decision row | — | ✅ | ✅ |
| **Decision** | `opportunity_pipeline.run_cycle:309` | `decisions.jsonl` | `decision_id`, `cycle_id` | ✅ | ✅ |
| **Shadow prediction** | `execution_engine._shadow_observer` | `shadow_predictions.json` | ticker + `ts` | ✅ | ❌ **excluded** |
| Cycle evidence | `execution_engine._record_cycle_evidence` | `cycles.jsonl` | `cycle_id` | ✅ | ✅ |
| Order → fill | `order_manager` | `orders_state.json` | `order_id` | only if traded | only if traded |
| **Settlement (trades)** | `research_export._settled_rows` ← `TradeLogger` | `kalshi_trades.json` | `trade_id`, `settled_at` | only if traded | only if traded |
| **Settlement (predictions)** | `ShadowPredictionStore.settle_pending` | `shadow_predictions.json` | ticker | ✅ | ❌ **unreachable** |
| **Calibration** | `calibration.analyze` ← `ShadowPredictionStore.settled()` | — | — | ✅ | ❌ **no input** |

### The precise gap

`decisions.jsonl` **already records every btc_daily prediction** — ticker,
strategy, `decision_id`, confidence, probability, edges, and `model_output`
with `model_version`. The gap is not "daily has no prediction record".

The gap is: **the only sink with a settlement loop excludes btc_daily, and the
sink that includes btc_daily has no settlement loop.** `decisions.jsonl` is
never joined to any outcome, and the settlement surface
(`research_export.settlements`) is *settled trades only* — it requires an order
to have filled. For a strategy that never trades, the shadow store is the sole
path from prediction to outcome, and daily is locked out of it.

---

## 4. Phase 2 — Verifying the gap

### 4.1 What is recorded, and why daily is not

`execution_engine._shadow_observer` returned early unless
`dec.strategy.startswith("btc15m")`. `btc_daily_above_strike` fails that test,
so **zero settled daily predictions have ever existed**. MEASURED.

**Classification: ACCIDENTAL in consequence — with a real protective
side-effect that must be preserved.**

Evidence for ACCIDENTAL:
- The function's own docstring claimed it logged *"CHAQUE candidat BTC evalue"*
  — every BTC candidate — while the filter admitted one family. An internal
  contradiction in the same function. MEASURED.
- The shadow record schema is fully satisfied by daily output: both strategies
  share `_BtcAboveStrikeBase.evaluate` and emit an identical `features` dict
  (`spot`, `strike`, `sigma_1m`, `minutes_remaining`, `ret_5m`). Nothing about
  the schema excludes daily. MEASURED.
- P6.2 later invested in extending the daily model to 14 days while leaving it
  unobservable — inconsistent with a deliberate decision never to observe it.
  INFERRED.

Evidence the *mechanism* is protective, and why the fix is **not** to widen the
filter: `backtest_btc15m.py` consumes the same store, applies **no strategy
filter**, and buckets by `minutes_remaining` with 15-minute edges
(`0-5`, `5-10`, `10-15+`). Daily rows dropped in would land silently in the last
bucket and contaminate a 15-minute analysis. MEASURED.

**Could admitting daily mutate a trading decision?** No. MEASURED:
`self.observer(...)` is invoked at `opportunity_pipeline.py:295-299`, *after*
`price_and_gate` has finalised `dec`, and the call is wrapped in
`try/except Exception` that logs at debug. The observer receives a snapshot and
a finished decision; it returns nothing the pipeline reads. Proven by test
(§7, INVARIANTS 1–5, 10).

**Fields that would be recorded** are enumerated in §5 and implemented in
`build_prediction_record`.

### 4.2 Why the T7-H dry-run printed `{}`

Not accepted silently. MEASURED, three independent confirmations:

1. `dry_run` builds `S = Counter()` and its **first** loop statement is
   `S["considered"] += 1` (`t7h_kxbtcd_horizon_probe.py:216`). An empty
   `Counter` renders as `{}`. So `{}` ⟺ **the loop body never executed** ⟺ the
   input population was empty.
2. Population arithmetic from the ATTESTED buckets:
   - A/current = in-window **and** liquid → the only visible bucket is `6-24h`
     with `liquid=0` → **0 rows**.
   - B/counterfactual = within declared horizon (≤20160 min) **and** liquid →
     all 378 liquid markets are in `>14d`, i.e. **beyond** the declared
     horizon → **0 rows**.
   - recovered-only ⊆ B → **0 rows**.
3. Consistent with the ATTESTED headline `order_candidates(B) − order_candidates(A) = 0`.

**Therefore `{}` means zero markets in the population — not a probe defect, not
a model invocation failure, not missing context.** The model was never called
because there was nothing to call it on.

**Consequential corollary:** all 378 liquid KXBTCD markets lie **beyond the
strategy's own 14-day declared horizon**. They are not merely outside the
scanner window — they are outside the model's contract. Reaching them would
require extending the model, not the scanner. This retires the hypothesis
raised in T7-G §5 and independently reconfirms T7-H's refusal.

---

## 5. Phase 3 — The BTC daily evidence contract

Implemented in `btc_daily_evidence.build_prediction_record`, schema version 1.
Absent values are recorded as **explicit null** — a missing field never becomes
a zero (tested).

| Field | Source | Availability |
|---|---|---|
| `record_id` | sha1(cycle_id\|ticker\|observed_at\|model_version)[:16] | **deterministic** |
| `decision_id`, `cycle_id` | `Decision.decision_id`; cycle prefix | available |
| `ticker`, `series`, `market_type`, `strategy_name` | decision / ticker | available |
| `strategy_version` | — | **null** — no strategy exposes a version today |
| `model_version` | `features.model_version` | available |
| `model_hash` | — | **null** — not exposed by the model (see F-3) |
| `observed_at`, `market_close_time`, `expiration_time` | cycle clock; market | available |
| `minutes_remaining`, `horizon_bucket` | snapshot | available |
| `underlying_price`, `strike`, `strike_source` | `features` | available |
| `market_yes_bid`, `market_yes_ask`, `market_implied_probability` | normalised book (`yes_mid/100`) | available |
| `predicted_probability`, `confidence` | `ModelOutput` | available |
| `edge`, `gross_edge`, `expected_value` | `Decision` | available |
| `data_quality`, `sigma_1m`, `sigma_effective`, `horizon_mode`, `ret_5m` | `features` | available |
| `model_valid`, `model_reason`, `decision_accepted`, `rejection_reason` | model + gate | available |

`horizon_mode` and `sigma_effective` are deliberately carried: they mark rows
the model itself flagged as extended-horizon, so T7-H's momentum and
vol-anchor concerns become **queryable** rather than argued.

---

## 6. Phase 4 — Settlement joinability

**Existing state, MEASURED:** `ShadowPredictionStore` joins on **ticker only**,
via `get_market(ticker).result`. It has no record id, no strategy field, and no
model version outside the nested `features`. It also rewrites the entire JSON
file on every `record()` (O(n²) in records) and polls **every** distinct pending
ticker on **every** cycle regardless of maturity — an unbounded per-cycle API
cost as the pending set grows. Those properties are why daily evidence was
given its own store rather than being appended to that one.

**New state, MEASURED and tested:**

- **Join key:** `record_id`, deterministic from record content. Prediction rows
  live in `btc_daily_predictions.jsonl`; settlement rows in
  `btc_daily_settlements.jsonl`. Append-only — a prediction row is never
  mutated.
- **Replay-safe:** identical decision → identical `record_id`, so duplicates
  are detectable rather than silently doubled.
- **Collision tested:** two tickers in one cycle produce distinct ids and settle
  to their own outcomes (`test_two_tickers_do_not_collide`).
- **Duplicate-settlement tested:** a second `settle()` pass writes zero rows and
  yields one calibration sample, not two.
- **Bounded cost:** a market is polled only once `market_close_time` has passed.
  An unmatured prediction costs **zero** API calls (`test_immature_market_is_never_polled`).
  A record with no close time is never polled at all.
- **Model identity survives the join:** `model_version`, `strategy_name` and
  `horizon_bucket` are copied onto the settlement row as well as the prediction.

No part of the settlement subsystem was redesigned; nothing existing was
touched.

---

## 7. Phases 6–8 — Patch and invariants

### Files

| File | Change | Lines |
|---|---|---|
| `btc_daily_evidence.py` | **new** — schema, append-only store, maturity-gated settlement, calibration surface | +330 |
| `execution_engine.py` | **only production file touched** — store construction, observer branch, settlement call, docstring corrected | **+41 / −2** |
| `tests/test_btc_daily_evidence.py` | **new** — 23 tests, all ten invariants | +330 |

The two deletions are the corrected docstring. The btc15m path is untouched.

### Invariant results — MEASURED, all passing

| # | Invariant | Test | Result |
|---|---|---|---|
| 1 | Trading decisions identical | `test_invariant_1_decisions_identical` | ✅ |
| 2 | Order-candidate count identical | `test_invariant_2_candidate_count_identical` | ✅ |
| 3 | Order-submission count identical | `test_invariant_3_order_submission_count_identical` | ✅ |
| 4 | Position sizing identical | `test_invariant_4_sizing_identical` | ✅ |
| 5 | Risk decisions identical | `test_invariant_5_risk_decisions_identical` | ✅ |
| 6 | btc_daily produces durable evidence | `test_invariant_6_daily_evidence_is_durable` | ✅ |
| 7 | btc15m behaviour intact | `test_invariant_7_btc15m_still_goes_to_shadow_store_only` | ✅ |
| 8 | Deterministic settlement join | `test_invariant_8_settlement_joins_on_record_id` | ✅ |
| 9 | Unsettled never enter metrics | `test_invariant_9_unsettled_never_enter_metrics` | ✅ |
| 10 | Evidence failure cannot increase trading | `test_invariant_10_failing_evidence_leaves_the_funnel_unchanged` | ✅ |

Invariants 1–5 use behavioural-equivalence testing: the same cycle is run twice
on the same fixture, once with no observer and once with the evidence observer,
and every funnel counter, the rejection ledger and each accepted decision are
compared. The full report dict is deliberately *not* compared — it embeds
wall-clock-derived `minutes_remaining` floats that differ by microseconds
between two sequential runs, which would make it a clock test rather than an
equivalence test.

Invariant 10 is tested three ways: an unwritable store returns `None` and
records nothing; a broken store leaves every funnel counter unchanged; and an
observer that raises cannot reach the cycle.

### Failure semantics

Every public method of the store swallows its own errors and returns a falsy
result. Losing evidence can never create, prevent, or alter an order. A torn
JSONL line is skipped rather than killing the read.

### Regression suite — MEASURED

```
pre-patch : 276 tests, 7 failures, 21 errors
post-patch: 299 tests, 7 failures, 21 errors   (+23 new, all passing)
failing-test-name set diff: IDENTICAL — no new failures, none fixed
```

The 28 pre-existing failures are unrelated to this work (missing `pytest`,
fixture drift on `get_fills(strict=)`, absent DEMO env flags) and were present
before T7-I. They are **not** fixed here; that is a separate ticket.

**Not deployed. Not merged.** Feature branch only.

---

## 8. Findings

### F-1 · HIGH · The daily model had no path from prediction to outcome — FIXED on branch
Proven in §4.1; patched and tested. ACCIDENTAL in consequence.

### F-2 · HIGH · The loop will record nothing in the current DEMO state
MEASURED from the ATTESTED buckets: evidence requires a KXBTCD market that is
liquid **and** in-window, and there are **zero**. All 378 liquid markets are
`>14d`, beyond even the model's declared horizon. **The operator should not
expect calibration data to accrue from this patch under current conditions.**
The patch is still correct and worth landing — it removes the blocker
permanently, so evidence begins the moment in-window liquidity appears — but it
is not, by itself, a source of data today.

### F-3 · MEDIUM · The documented validation procedure references code that does not exist
MEASURED: `MODEL_VALIDATION_GUIDE.md` instructs the operator to call
`ShadowPredictionStore().as_calibration_obs()` and
`model_gatekeeper.model_hash()`, and cites `GATE_MIN_PREDICTIONS=300` /
`GATE_MIN_TRADES=100`. A repo-wide grep finds **none** of these four
identifiers in any `.py` file. The documented path to validation is partly
non-executable as written. Not fixed here — out of T7-I scope, but it blocks
Phase 9 in practice.

### F-4 · MEDIUM · `ShadowPredictionStore` polls every pending ticker every cycle
MEASURED (§6). Unbounded per-cycle API cost as the pending set grows, plus a
full-file rewrite per record. Untouched by this patch — the new store avoids
both — but it is a latent cost on the btc15m path. Flagged, not fixed.

### F-5 · INFO · `decisions.jsonl` already carries daily predictions
MEASURED. Any historical `decisions.jsonl` retained on the Railway volume
already contains daily predictions with `model_version` and `minutes_remaining`
— they simply have no outcome. If those rows are still retained, a **backfill**
joining them to settled market results could seed calibration without waiting
for new evidence. Worth checking before assuming N=0 forever. Not implemented.

---

## 9. Phase 9 — Calibration acceptance protocol (designed, NOT executed)

**Institutional thresholds that already exist**, cited from
`MODEL_VALIDATION_GUIDE.md` — written for **btc15m**, and their transfer to
daily horizons requires explicit operator approval:

- `GATE_MIN_PREDICTIONS = 300` settled predictions; `GATE_MIN_TRADES = 100`
- calibration only after **≥150** settlements; do not refine binning while n < 500
- **model Brier must beat the market baseline (`yes_ask/100`) out-of-sample** —
  "sinon le marche predit mieux que le modele et il n'y a aucun edge"
- positive PnL on < 100 trades is **not** conclusive
- current model status: `btc15m-baseline-0.1`, **NOT VALIDATED**, shadow only

**Thresholds requiring explicit operator approval before any daily judgement:**

| Item | Status |
|---|---|
| Minimum settled sample, daily overall | **REQUIRES OPERATOR APPROVAL** |
| Minimum sample **per horizon bucket** | **REQUIRES OPERATOR APPROVAL** |
| Brier / ECE acceptance levels | **REQUIRES OPERATOR APPROVAL** |
| Temporal-holdout split | reuse `backtest_btc15m.split_chronological` 60/20/20 |

**The independence problem — must be settled before any threshold is chosen.**
MEASURED/INFERRED: KXBTCD lists many strikes per expiry date, and every strike
sharing an expiry resolves from **one** underlying price path. 300 settled
daily predictions are therefore **not** 300 independent observations — they may
be a few dozen independent underlying outcomes observed at many strikes. Naive
application of the btc15m sample sizes would badly overstate confidence. The
acceptance protocol must count **independent settlement dates**, not rows, and
`calibration_records()` carries `market_close_time` so this can be computed.

**Leakage prevention:** calibration fitted on TRAIN only; chronological split;
no future observation used in any decision (`backtest_btc15m` already enforces
this and documents it as "test 13").

**Model-version isolation:** enforced structurally —
`calibration_report(model_version=…, strategy=…, bucket=…)` filters explicitly,
and the segment is stamped into the report's `label` and `segment` fields.
Versions and horizons cannot be pooled without it appearing in the output.

**Regime diversity:** UNKNOWN and unaddressed — no volatility-regime tagging
exists for daily evidence. Flagged as a gap, not solved.

**No threshold in this document was chosen after seeing results, because there
are no results.** N = 0.

---

## 10. Phase 10 — Autonomous engineering handoff (interface only)

The evidence layer exposes a read-only interface. **No autonomous
production-write authority is granted, designed, or implied.**

| Stage | Interface | Status |
|---|---|---|
| OBSERVE | `BtcDailyEvidenceStore.coverage()` → counts by horizon bucket and model version | **available** |
| DETECT | `calibration_report(strategy, model_version, bucket)` → Brier, ECE, reliability curve, confidence histogram, predicted mean vs realised rate | **available** |
| FORM HYPOTHESIS | segment comparison across buckets/versions on the same surface | **available** |
| PROPOSE EXPERIMENT | — | out of scope |
| TEST on historical evidence | `calibration_records()` emits the shape `backtest_btc15m` and `model_calibration` already consume | **available** |
| COMPARE vs baseline | market baseline = `market_implied_probability`, recorded per row; the guide's rule (model Brier must beat it) is directly computable | **available** |
| RECOMMEND patch | — | out of scope |
| REQUIRE GOVERNANCE | `model_gatekeeper.check_live_allowed()` — live blocked by default | **existing, unchanged** |
| DEPLOY | — | **never autonomous** |
| MONITOR | re-run `calibration_report` per segment over time | available |
| ROLLBACK | evidence files are append-only; the patch reverts with one commit | available |

**Contract for consumers:** read `btc_daily_predictions.jsonl` and
`btc_daily_settlements.jsonl`, join on `record_id`, honour `schema_version`,
never treat an unsettled prediction as an observation, and never pool model
versions or horizon buckets without labelling the aggregation.

---

## 11. Limitations

1. **N = 0.** No daily prediction has been recorded yet, and none will be under
   current DEMO conditions (F-2). Every calibration number remains UNKNOWN.
2. **No Kalshi or BTC price-source access** from this session (403 CONNECT), so
   the patch is proven by unit and equivalence tests, not by a live cycle.
3. **Not deployed, not merged**, and the Railway deployed commit is UNKNOWN.
4. **28 pre-existing test failures remain** — unchanged, unrelated, not fixed.
5. **Regime diversity is unaddressed** (§9).
6. **The independence problem is identified but unquantified** — it needs real
   settlement dates to measure.
7. Three prior audit commits plus this one remain **unpushed**; `git push`
   returns 403 and the GitHub App returns `403 Resource not accessible by
   integration`. Delivered as a bundle instead.

---

## 12. Recommended next action

1. **Review and authorize the patch** (branch only — do not deploy from this
   session). It is +41/−2 in one production file, with the btc15m path
   byte-identical and a byte-identical regression failure set.
2. **Check whether `decisions.jsonl` is still retained on the Railway volume**
   (F-5). If it is, historical daily predictions can be backfilled against
   settled market results and calibration could start immediately rather than
   waiting for in-window liquidity that may not come.
3. **Repair `MODEL_VALIDATION_GUIDE.md`** or implement the four identifiers it
   references (F-3) — otherwise Phase 9 cannot be executed as documented.
4. **Decide the independence rule** (§9) before any sample-size threshold is
   set for daily.
5. Only after settled daily evidence exists in at least one horizon bucket
   should T7-H's Phase 6 be revisited.

**No model receives additional market access from this phase, and none is
requested.**
