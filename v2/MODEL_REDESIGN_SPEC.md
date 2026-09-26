# Model reconstruction specification

Status: **PREREGISTERED_NOT_TRAINED**. Offline implementation only. No model artifact is fitted on real observations, no candidate is locked, no prospective clock has started, and no approval is granted.

The rejected V1 model is not preserved as an incumbent to beat. It is retained only in the consumed-data diagnosis. The deployed collector and frozen earlier learning protocol are unchanged. This module is not imported by the service and has no network, order, cancellation, transfer or financial-mutation path.

## Frozen protocol

Canonical registry hash: `ae644e7fa113b7d5177626d43408cd6ac4f6f3913d5a679a076ec1793a534074`.

The full registry in `atlas_v2/CHALLENGER_REGISTRY.json` is authoritative. Two active families were specified before any results: MR-STRUCTURAL-1 and MR-REGIME-1. The clean base is Phi(log(spot/strike)/(sigma_1m*sqrt(minutes_remaining))), zero log-return drift and no momentum. Zero log-return drift is not a claim of zero arithmetic GBM drift. Spot/settlement-index basis must qualify independently.

| Family | Economic mechanism | Fixed search |
|---|---|---|
| Structural | Venue/reference risk mismatch or slow quote adjustment may cause systematic probability miscalibration; a separate calibration stage tests whether correction beats contemporaneous market prices. | Sigma scales 0.75, 1, 1.25 |
| Regime | Dispersion can change faster than displayed probabilities; fixed low/high-volatility scaling tests that mechanism. | Same three scales in each of two regimes; sigma cutoff 0.001; nine combinations |

These are hypotheses, not established inefficiencies. Market participants may already incorporate the information; no forecast edge is presumed. Microstructure, cross-market and Sports are deferred for missing qualified evidence, without modifying their earlier hypotheses or R01–R12.

## Chronology and contamination firewall

| Stage | UTC start inclusive | UTC end exclusive | Minimum |
|---|---|---|---|
| TRAIN | 2026-09-27 00:00 | 2026-10-11 00:00 | 30 distinct events each day |
| CALIBRATION | 2026-10-11 00:00 | 2026-10-18 00:00 | 30 distinct events each day |
| VALIDATION | 2026-10-18 00:00 | 2026-11-01 00:00 | 30 distinct events each day |

TRAIN selects sigma scales by Brier, deterministic lexicographic ties. CALIBRATION alone selects Platt slope from {0.75,1,1.25} and intercept from {-0.25,0,0.25}. Features, cohort, grids, baseline and cost rules cannot be changed after seeing outcomes. Event overlap, consumed V1 rows and labels unavailable at fixed cutoff fail closed. Missing daily minima invalidate that stage; do not slide dates after inspecting results. If collection cannot meet these prospective windows, preserve this protocol as untestable and explicitly preregister a replacement before inspecting replacement outcomes.

Earliest possible validation completion is 2026-11-01 00:00 UTC, conditional on complete admissible data. Actual collection rate is unknown; no rate or completion guarantee is invented. OOS starts at the next UTC midnight strictly after an actual immutable candidate lock, for 28 complete days and at least 840 events, at least 30/day. No candidate lock exists now. The earliest theoretical OOS start would be 2026-11-02, with evaluation no earlier than 2026-11-30, assuming all previous gates pass and lock occurs November 1. This is a schedule, not a deployed collection promise.

## Native data and lineage

Features reconstruct from append-only Q_RAW receipt records behind a verified collector anchor: attributable official Kalshi BTC15M active binary greater-than market, authoritative positive field strike and rules, fresh quote/reference, and exactly 31 closed consecutive one-minute Coinbase candles yielding 30 returns. No spot-proxy strike, stale candles, inferred missing values or external probability is accepted as qualified input. Decision cohort is the first qualified decision per ticker 240–300 seconds before close. Native reference/quote age is bounded by the existing five-second BTC qualification rule; Sports remains 250 ms/one second and is untouched.

Forecast records bind full model artifact SHA-256, source git SHA, implementation SHA-256, feature schema/hash, ticker/event, decision and persistence timestamps, provider/quote/strike provenance, raw receipt hashes/anchor and fee/slippage policy version. Probabilities are recomputed internally. Source hashes prove content identity, not provider truth; provenance and receipt authenticity still require independent verification. Offline source SHA is explicitly supplied and must be verified against the reviewed checkout before use.

Settlement cannot exist at prediction time. A separate immutable label link binds the original prediction hash to a final Kalshi settlement receipt, event/ticker and publication time. Provisional, unreadable, conflicting or identity-mismatched settlements fail closed. No historical outcome is replaced.

Model uncertainty, calibration confidence, data quality and economic-edge confidence are separate fields. Receipt-complete data does not imply an approved model or qualified basis/costs.

## Execution and accounting

Fresh executable repricing calls the same existing V2 `reprice` guards: max entry, spread, gross edge, net edge, EV, refreshed fees, positive slippage, uncertainty buffer, age, close and displayed size. Fee amount is recomputed from the fresh price and rounded up per order. Slippage includes a positive policy bound plus adverse decision-to-refresh movement. Fee policies require separately authenticated, effective market-class receipts; hash-shaped inputs alone do not qualify authority. `would_submit` remains false.

Existing V2 reservation logic retains one trade/market, three-position maximum and 20% drawdown fence. Reconstruction does not invoke that financial path or alter limits. Settlement diagnostic PnL subtracts the entry/fee/slippage cost bound exactly once. Percentage drawdown uses explicit allocated equity/high-water semantics. Existing accounting separately maintains flow-adjusted allocation NAV; neither approach divides historical dollar losses by current broker cash.

The append-only store raises load, write and hash-chain errors; prediction writes verify the ledger and roll back on failure. No corrupt load becomes an empty successful store. External anchors are required to prove that historical tail data has not disappeared.

## Qualification gates and implementation boundary

Validation recomputes forecasts on identical native rows, compares Brier/log loss/calibration to market midpoint and reports day/event block intervals. Two active families use 97.5% intervals; both Brier and log-loss upper bounds must be below zero and ECE <=0.05. This is not sufficient for approval.

Final survival also requires net hypothetical PnL >0 with authenticated spread/fees/conservative slippage/liquidity, no single positive day contributing >25% of total PnL, independent periods, no leakage or post-outcome tuning, complete lineage, basis qualification and Claude independent reproduction. Better forecasts with failed economics are rejected. Positive selection PnL without forecast edge is SELECTION_WITHOUT_FORECAST_EDGE, never promotion.

Validation artifacts hash-bind candidate identity, model hash, git SHA, dataset SHA, feature schema, protocol and full results. Hashes establish integrity only; independently signed review is still required. Existing signed-review infrastructure is retained, but no review is fabricated or issued here.

Implemented: full-store diagnostic engine; receipt reconstruction; fixed-grid train/calibration; validation metrics/block uncertainty; internally recomputed prediction lineage; append-only labels; guarded fresh economic calculations; hash-bound result artifacts; regression/mutation tests.

Not claimed complete: actual V1 ablation, real-data fitting, independently authenticated cost/basis adapters, complete economic cohort evaluator, immutable candidate-lock/OOS orchestration, runtime integration/deployment, or Claude reproduction. Those are gated on evidence and reviewed follow-up implementation. This release deliberately has no candidate-promotion function. Current research remains NOT_TESTABLE, not REJECTED on fabricated metrics.

## External next actions

1. Provide the full-store native export specified in FULL_STORE_ABLATION_REPORT.md.
2. Provide immutable V2 qualification exports with raw market/reference/candle/final-settlement receipts and anchors, plus source/release lineage.
3. Supply attributable effective fee rules, real execution-cost receipts/positive slippage bounds, and settlement-index basis evidence; do not expose account secrets.
4. After real-data validation and completion of the remaining economic/OOS interfaces, independently review and authorize any new deployment. No deployment is requested by this artifact alone.

CAPITAL remains OFF; no broker writes or real orders are introduced. Main, PR79, V1 and historical ledgers are unchanged. PR80 remains draft and unmerged.
