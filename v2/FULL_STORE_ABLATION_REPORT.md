# Full-store ablation report

Status: **BLOCKED_BY_OPERATOR_DATA_EXPORT**. No full-store empirical result is claimed.

The requested current V1 shadow store was not available in the workspace, retrieved artifacts, or repository. The reported approximately 13,519 settled usable observations and 25 settlement dates are the requested target, not independently recounted observations. Dataset SHA-256, actual counts, coverage, Brier, log loss, calibration, baseline deltas, momentum saturation, PnL, costs and drawdown are **unavailable** until the native export is provided. This is missing evidence, not model-family rejection.

## Exact artifact required

Supply a byte-exact immutable export of the complete active V1 `shadow_predictions.json` (expected `/data/state5/shadow_predictions.json`; confirm against the active DATA_DIR), without modifying the running store. Include a provider/operator-attributed manifest with export UTC time, service/deployment and release SHA, resolved store path, byte length, file SHA-256, total row count, settled usable count, sorted settlement dates and decision-time coverage. Preserve all rows and original fields, including missing values, costs, features, provider identifiers and settlement metadata. If current counts exceed 13,519, retain all rows rather than trimming to the historical estimate. Do not include broker credentials.

Also provide the model/calibration artifacts and their hashes, release binding, authoritative settlement receipts, fee/slippage policy and receipt bindings, and an independently evidenced allocation if percentage drawdown is required. Export a consistent frozen snapshot with an explicit cutoff; a dashboard summary or a selection of successful trades is insufficient. No V1 deployment, account change or ledger repair is requested or performed.

## Implemented reproducible diagnosis

`atlas_v2.model_diagnosis` consumes a bounded JSON-array store and hash/count manifest. One explicit decision interval `[start,end)` drives both forecasts and PnL. Every outside-interval or inadmissible row is counted; no silent filtering. The control must reproduce the legacy formula within 1e-6 or returns a lineage/data blocker.

Ablations: as recorded; no momentum; momentum caps 0.02, 0.05, 0.10, 0.25, 0.50; volatility scales 0.50, 0.75, 1.25, 1.50, 2.00. All twelve variants use identical admissible rows. The field-strike-only cohort also receives all twelve variants. Provider/quality, time-to-expiry and fixed volatility strata report as-recorded paired results, with unknown provenance kept explicit.

Metrics: Brier, log loss, ten-bin calibration/ECE, same-row market midpoint baseline and supplemental ask baseline, day-level Brier differences, equal-weight day/event-block bootstrap intervals (2,000 deterministic replications, 95%), and momentum saturation. Component contributions are one-at-a-time Brier contrasts; they are not causal or additive. Ticker is explicitly the fallback event cluster when an event identifier is missing; this can understate cross-market dependence and is not final qualification.

Shadow PnL uses the same forecast cohort/time interval, selected-side ask entry, one contract and the first recorded selected decision per ticker. Duplicate decisions, priced rows, fee/slippage coverage, daily decomposition, largest-day contribution and settlement-order drawdown are reported. No separate trade-row resplit occurs. Legacy estimates are labeled unqualified even if present. Missing costs never default to zero; incomplete cost coverage makes total PnL unknown. A zero slippage estimate is unqualified. Percentage drawdown requires explicit positive hypothetical allocation and uses equity high-water, not broker cash. Intratrade mark-to-market drawdown, executable fill availability and actual portfolio capital feasibility remain unproven. No profitability claim is permitted from this diagnostic.

The previous audit implementation at commit `6041679fd3a937d5463f7a9989a39f9aadedb053` defaulted missing estimated costs to zero and split selected trades separately from forecast rows. Those behaviors are excluded from this replacement diagnostic. Historical results are not rewritten.

## Reproduction

From the repository root:

```bash
PYTHONPATH=v2 python -m atlas_v2.model_diagnosis shadow_predictions.json manifest.json full_store_results.json --start <FIRST_DECISION_UTC> --end <FROZEN_CUTOFF_EXCLUSIVE_UTC>
```

Add `--allocation <EVIDENCED_HYPOTHETICAL_ALLOCATION>` only when justified. The output is exclusively created to avoid overwriting evidence. The manifest must contain `dataset_sha256`, `row_count`; a completeness claim additionally needs `complete_store: true`, `settled_usable_count`, `settlement_dates`. Hash equality establishes integrity, not authentic source authority.

All V1 observations remain consumed diagnostic evidence. None can qualify a new model as fresh prospective OOS. The synthetic tests in this change prove software behavior only.
