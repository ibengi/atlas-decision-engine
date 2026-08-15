# T7-K — BTC Daily Pre-Liquidity Shadow Evidence

**Date:** 2026-08-15 · **Scope:** evidence collection only. No liquidity
criterion, scanner horizon, target series, general crawl, model mathematics,
probability or confidence output, edge/EV threshold, risk threshold, position
sizing, trade limit or position limit was changed. No order placed. Not
deployed. Not merged.

`MEASURED` = executed here · `ATTESTED` = operator/Railway output ·
`INFERRED` = reasoned, labelled · `UNKNOWN` = not determinable here.

---

## 1. Verdict

> ### SHADOW EVIDENCE READY FOR REVIEW
>
> Implemented on the feature branch, **off by default**
> (`BTC_DAILY_SHADOW_ENABLED`). Not deployed, not merged.

Execution isolation is structural and tested. The deduplication contract is
pre-registered and defensible. All ten trading invariants hold, and the full
regression suite's failure set is **byte-identical** to before the change.

One number in §9 should govern how this evidence is read: the population
yields roughly **1–2 independent outcomes per day**, not 80, and no threshold
written for the 15-minute model transfers to it.

---

## 2. Phase 0 — Source

| Item | Value |
|---|---|
| Branch | `claude/market-universe-scanner-audit-4s50oq` |
| HEAD at start | `33b9026` (rebased onto main) |
| `origin/main` | `d452b03` |
| T7-I ancestry | `dd1b0f8` verified **in** main |
| Working tree | clean |
| Deployed commit | `T7I_MODULE=/app/btc_daily_evidence.py`, `T7I_STORE=True` (ATTESTED) |

No drift.

---

## 3. Phase 1 — The liquidity boundary, and what survives it

**MEASURED path.** `market_scanner.scan_cycle` runs
status → time window → **liquidity** → classification → cycle cap, and returns
only survivors in `scan["markets"]`. A market failing `_has_liquidity`
(`market_scanner.py:432-461`) is `continue`d, never appended to `kept`, and so
never reaches `opportunity_pipeline`'s evaluation loop, never becomes a
`Decision`, and never reaches risk or the order manager.

**The enabling fact — MEASURED.** Introspecting
`_BtcAboveStrikeBase.evaluate`, the only line mentioning `book` is the
signature itself:

```
lines mentioning book in evaluate():
    def evaluate(self, market, book, minutes_remaining) -> ModelOutput:
=> the model reads NO order-book field; book is accepted and ignored
```

The strike comes from the market (`floor_strike` / ticker), spot and
volatility from the BTC context. **An empty book blocks execution without
blocking prediction.**

**Model inputs available at the rejection point — MEASURED:**

| Input | Source at rejection | Available |
|---|---|---|
| ticker, market metadata | market dict | ✅ |
| strike | `floor_strike` / ticker parse | ✅ |
| spot, volatility, momentum | `btc_context` (independent of the market) | ✅ |
| minutes remaining | `m["_minutes_remaining"]`, set at the time-window stage | ✅ |
| model version | `ModelOutput.features` | ✅ |
| strategy identity | `router.route("btc_above_strike_daily")` | ✅ |
| classification | `classify(m)` — deterministic, no network | ✅ (computed here; the scanner classifies one stage later) |
| order book | absent — **and not needed** | n/a |

**Smallest insertion point:** the liquidity loop itself. Rejected markets are
appended to scanner instance state (`_shadow_population`), which is
deliberately **not** placed in the report — the report is serialised to disk
for research consumers, and this list is neither evidence nor a funnel figure.

---

## 4. Phases 2–3 — Population and hard isolation

**Architecture, as required:** scanner rejection → shadow adapter → evidence
store. The normal decision path is not involved at any point; nothing is
"flagged non-tradeable after the fact".

All seven Phase-2 conditions are enforced, and status and illiquidity are
**re-verified inside the evaluator** rather than trusted from the caller, so
the population cannot silently widen to something tradeable.

**Isolation proofs — MEASURED, each a test:**

| Claim | Proof |
|---|---|
| Cannot become an accepted `Decision` | `btc_daily_shadow.py` never constructs or imports `Decision`; asserted on the module source with comments stripped |
| Cannot enter risk / orders / positions | module imports no `risk_manager`, `order_manager`, `position_manager`, `PositionSizer`, `create_order`; asserted on source |
| Result type is not decision-shaped | `ShadowObservation` has no `accepted`, `side`, `entry_ask`, `taille`, `net_edge`, `net_ev` field |
| Cannot alter position state or trade counts | evaluator holds no reference to any state object; runs after `_finish_cycle` has placed every order |
| The two populations are disjoint | scanner test asserts `kept ∩ shadow_population == ∅` and that "shadow" appears nowhere in the serialised report |
| No synthetic liquidity | the model is passed `book=None`; asserted (`books_seen == [None]`). Fabricating a book would be inventing market data |
| Recorded rows are marked | every row carries `origin="shadow_pre_liquidity"`, `execution_eligible=False`, `decision_accepted=False`, `rejection_reason="no_liquidity"`, and null edge/EV |

---

## 5. Phase 4 — Deduplication contract

**Chosen: option C+D — one observation per (ticker, model_version,
pre-registered horizon checkpoint).**

Rejected alternatives: **A** (one per ticker per model version) collapses the
whole life of a market to a single point and cannot calibrate across
time-to-expiry; **B** (per horizon bucket) is the same idea with coarser,
post-hoc bins.

| Term | Definition |
|---|---|
| `observation_key` | `"{ticker}\|{model_version}\|{checkpoint_label}"` — **contains no timestamp** |
| `record_id` | `sha1("shadow\|" + observation_key)[:16]` — deterministic |
| dedup | in-memory set, seeded from the store at construction |
| model-version | a new `model_version` is a **new** observation; versions are never conflated |
| restart | the index is rebuilt from disk, so a redeploy or crash cannot re-record |

**MEASURED:** 80 markets, three consecutive cycles → 80 rows, then 0, then 0
(80 counted as `shadow_daily_deduplicated`). A fresh evaluator on the same
directory also records 0. One ticker walked through eight horizons produces
exactly five rows, one per checkpoint.

---

## 6. Phase 5 — Horizon sampling

Checkpoints are **pre-registered before any data exists**: 24h, 12h, 6h, 3h,
1h.

Chosen from the model's declared domain rather than by convention:
`BtcDailyStrategy.max_minutes = 1560` (26h) and the scanner admits 5–1440
minutes, so **every checkpoint sits inside both** — each observation is
`horizon_mode="normal"`, free of the extended-horizon confidence cap, and
calibrates the model where it is actually used. A test asserts this
(`< 1560` and `<= 1440`) so the property cannot silently regress.

Deliberately excluded: anything above 1440 (unreachable through the scanner)
and anything below 60 (inside the 5-minute floor's noise; KXBTCD is not a
minute-scale instrument).

Assignment is the **smallest checkpoint ≥ minutes_remaining**, so a market
crossing 24h → 12h → 6h → 3h → 1h fires each exactly once. A cycle running
every 60 s does not resample.

---

## 7. Phase 6 — Settlement

Reuses the T7-I store unchanged: predictions in
`btc_daily_predictions.jsonl`, settlements in `btc_daily_settlements.jsonl`,
joined on `record_id`, with `ticker`, `result`, `settled_at`,
`model_version`, `strategy_name`, `horizon_bucket` and the checkpoint carried
across. **MEASURED:** outcomes come only from the API `result` field (a
response without yes/no settles nothing); markets are polled only after
`market_close_time` has passed; a second settle pass writes zero rows.

---

## 8. Phase 8 — Behavioural equivalence

Same fixture, one cycle each with the shadow path off and on. **All ten
invariants MEASURED as holding:** every funnel counter, the rejection ledger,
accepted-candidate count, side, entry price, edges, EV, `taille` (sizing),
`risk_passed`, `orders_submitted`, `fills`.

Specifically: `after_liquidity = 1` and `no_liquidity = 5` with the shadow
path both off and on — the liquidity gate rejects exactly what it rejected
before — while `shadow_daily_predicted = 5`.

New counters are namespaced and asserted disjoint from production names:
`shadow_daily_considered`, `_predicted`, `_deduplicated`, `_invalid`,
`_out_of_scope`, `_no_checkpoint`, `_write_failed`, `_capped`.

### Regression

```
before T7-K : 299 tests, 7 failures, 21 errors
after  T7-K : 333 tests, 7 failures, 21 errors   (+34 new, all passing)
failing-test-name set diff: IDENTICAL — no new failures introduced
```

The 28 pre-existing failures (missing `pytest`, `get_fills(strict=)` fixture
drift, absent DEMO env flags) are untouched and remain a separate ticket.

### Files

| File | Change |
|---|---|
| `btc_daily_shadow.py` | **new** — checkpoints, evaluator, `ShadowObservation` |
| `tests/test_btc_daily_shadow.py` | **new** — 34 tests |
| `btc_daily_evidence.py` | +60/−6 — optional `record_id` / `extra`, `observation_keys()`, `statistics()` |
| `execution_engine.py` | +25 — evaluator construction, one call after order execution |
| `market_scanner.py` | +14 — retain the rejected population; `shadow_population()` |

Production diff: **99 insertions, 6 deletions** across three files. No filter,
threshold or counter was altered.

**Rollback:** unset `BTC_DAILY_SHADOW_ENABLED` — the path goes inert with no
redeploy. To remove entirely, revert the commit; the evidence files are
append-only and orphaned harmlessly.

---

## 9. Phase 7 — Counting, and the number that governs everything

`statistics()` reports raw and independent counts separately, and never
conflates them:

`prediction_rows` · `settled_prediction_rows` · `unique_tickers` ·
`unique_expiries` · `settled_unique_tickers` · **`independent_outcomes`** ·
`by_checkpoint` · `settled_by_checkpoint`.

**`independent_outcomes` counts distinct settled `market_close_time` values,
not rows.** Every KXBTCD strike sharing a close time resolves from one
underlying price at one instant: if BTC lands above the whole ladder, all 80
strikes resolve YES together. Their outcomes are perfectly rank-correlated.
Different strikes do probe different regions of the predicted distribution, so
their information is not zero — but it is not independent either, and this is
the conservative count. **No effective-N formula is invented.** MEASURED by
test: two strikes on one expiry plus one on another → 3 settled rows, 3 unique
tickers, **2** independent outcomes.

**The consequence, and it is large.** 80 in-window markets × 5 checkpoints ≈
**400 rows per market generation**, against roughly **1–2 distinct expiries per
day** (INFERRED from KXBTCD being a daily series with a strike ladder; the
exact ratio is UNKNOWN until real data arrives and `statistics()` reports it).
Raw N therefore overstates independent evidence by **roughly two orders of
magnitude**. Judging this model on row counts would be a serious error.

---

## 10. Phase 9 — Pagination (design only, NOT implemented)

Kept strictly separate, as required. This branch contains **no** pagination
change.

**T7-KB, smallest safe patch, in two steps:**

1. **Observability first, zero behaviour change.** Emit `series_counts` and a
   `series_truncated` flag when a series returns exactly `SCANNER_PAGE_LIMIT`
   rows. This alone would have surfaced the defect months ago.
2. **Then follow the cursor** in `kalshi_client.get_markets`, bounded by
   `SCANNER_PRIORITY_MAX_PAGES` — the knob that already exists and is
   currently dead code.

**Behavioural equivalence for the in-window trading population — INFERRED from
the T7-H census, and testable before merge.** Pagination can only *add*
markets, and every KXBTCD market beyond page 1 is >24h out (`3-7d`=50,
`>14d`=906). Those are rejected at the time-window stage, **before** liquidity.
So `after_time_window` stays 80, `after_liquidity` stays 0, and no new order
candidate appears. What changes is that `scanned_raw` (201 → ~1047) and
`outside_time_window` (120 → ~966) become *accurate* — a reported-metric
change that must be announced, not a trading change.

**The two patches are independent.** The shadow population is drawn after the
time-window stage, so the markets pagination adds never reach it: T7-KA's
population stays at 80 whether or not T7-KB lands. Neither blocks the other.

---

## 11. Phase 11 — First prospective validation study

**Institutional thresholds, cited and status-labelled:**

| Source | Threshold | Status |
|---|---|---|
| `MODEL_VALIDATION_GUIDE.md` | `GATE_MIN_PREDICTIONS=300`, `GATE_MIN_TRADES=100` | **NON-EXECUTABLE** — neither identifier exists in any `.py` file (MEASURED) |
| `MODEL_VALIDATION_GUIDE.md` | calibration only after ≥150 settlements; no bin refinement while n<500 | **NON-EXECUTABLE** as code; written for **btc15m** |
| `MODEL_VALIDATION_GUIDE.md` | **model Brier must beat the market baseline out-of-sample** | **EXECUTABLE and adopted** — the baseline is recorded per row as `market_implied_probability` |
| `model_gatekeeper.check_live_allowed()` | live blocked by default | **EXECUTABLE**, unchanged |

**Why the btc15m sample sizes must not be reused.** They were written for ~96
independent 15-minute windows per day. Here, 300 rows would accumulate in
under a day and represent **~1–2 independent outcomes**. Transferring the
number would produce overconfidence of roughly 100×.

**Pre-registered study design (thresholds requiring operator approval before
any data is examined):**

- **Unit of evidence:** distinct settled expiry, never a row.
- **Required independent outcomes:** operator-set. For scale: 30 distinct
  expiries ≈ **1 month** of continuous collection at 1–2/day.
- **Per-checkpoint requirement:** each of 24h/12h/6h/3h/1h analysed
  separately; a checkpoint below the agreed minimum is reported as
  insufficient, never pooled upward.
- **Metrics:** Brier, ECE, reliability curve via `calibration.py`, plus
  predicted mean vs realised rate and the confidence histogram — all already
  emitted by `calibration_report()`.
- **Comparator:** market baseline (`market_implied_probability`). If the model
  cannot beat it out-of-sample, there is no edge — the guide's rule, adopted.
- **Temporal holdout:** chronological split (`backtest_btc15m.split_chronological`,
  60/20/20), calibration fitted on TRAIN only.
- **Model-version isolation:** structural — `calibration_report(model_version=…,
  strategy=…, bucket=…)` stamps the segment into the report; versions cannot
  be pooled silently.
- **No threshold in this document was chosen after seeing results. N = 0.**

---

## 12. Phase 12 — Why NFL becomes the next market-access project

ATTESTED `by_series` evidence, now supplied:

| Series | Open | Atlas-liquid | Liquidity rate | Gate |
|---|---|---|---|---|
| `KXNFLSPREAD` | 575 | **575** | **100%** | `no_probability_provider` |
| `KXNFLTOTAL` | 437 | **437** | **100%** | `no_probability_provider` |
| `KXBTCD` | 1,036 | 378 | 36.5% | `REACHES_MODEL` |

Against a **truncated** census (80,000 sampled, 7,125 liquid, capped at 400
pages — every count a floor).

**Three measured contrasts:**

1. **Liquidity.** 1,012 NFL markets are liquid at a 100% rate. KXBTCD's 378
   liquid markets are all `>14d`, outside the model's own declared horizon —
   so Atlas's only supported series has, in practice, **zero** liquid
   evaluable markets while NFL has over a thousand.
2. **Independent outcomes.** Each NFL game is one independent event. A single
   week yields ~16 independent outcomes; KXBTCD yields ~1–2 per **day**, and
   its 80 strikes collapse to one realisation. NFL would produce trustworthy
   calibration evidence roughly an order of magnitude faster.
3. **Execution readiness.** The classifier is correct, router entries are
   live, and `has_probability_source()` widens the scanner scope automatically
   once a real provider is injected — **no scanner or classifier change**.

**But not now, and the ordering matters.** NFL is blocked on exactly one
thing — a calibrated probability provider — and Atlas has never once completed
a prediction → outcome → calibration loop for *any* model. Building market
access before the feedback loop is trustworthy would mean granting a new,
unvalidated model more access than the existing one has earned. T7-K exists to
close that loop first. **No sports trading is enabled, and none is
recommended until a model has demonstrably beaten its market baseline
out-of-sample.**

---

## 13. Limitations

1. **N = 0.** Nothing has been collected; the path is off by default and
   undeployed. Every calibration figure remains UNKNOWN.
2. **No Kalshi or BTC price-source access** from this session, so the path is
   proven by unit and equivalence tests, not by a live cycle. In particular
   the real `btc_context` has never been exercised through it — the
   `shadow_daily_invalid` counter exists precisely to make that visible.
3. **The expiries-per-day ratio is INFERRED**, not measured. `statistics()`
   reports it the moment real data exists.
4. **28 pre-existing test failures** remain, unchanged and unrelated.
5. **Pagination is not fixed** — designed only (§10).
6. Four audit commits plus this one remain **unpushed** (`git push` → 403;
   GitHub App → `403 Resource not accessible by integration`).

---

## 14. Recommended next action

1. **Review the branch.** 99 production insertions across three files, off by
   default.
2. **Deploy with `BTC_DAILY_SHADOW_ENABLED=1`** when satisfied, and watch
   `[SHADOW_DAILY]` counters plus `statistics()`. First rows should appear in
   the first cycle; first settlements within ~24h.
3. **Confirm the expiries-per-day ratio** from `statistics()` before choosing
   any sample-size threshold.
4. **Land T7-KB (pagination) separately**, observability step first.
5. **Do not start NFL provider work** until one model has beaten its market
   baseline out-of-sample.
