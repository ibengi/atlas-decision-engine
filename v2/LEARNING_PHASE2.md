# Atlas V2 — qualified learning protocol

Version: PHASE2-20260926-1. The canonical JSON and SHA-256 are in TRAINING_PROTOCOL.json. The live append-only L_TRAINING_PROTOCOL event binds that same document to the exact deployed source and must be recorded before 2026-09-27T00:00:00Z. A changed protocol or source is refused. Existing historical observations cannot enter this experiment.

Financial state: CAPITAL=OFF, BROKER_WRITES=0, REAL_ORDERS_SUBMITTED=0. There is no promotion function, broker adapter, credential or real-order path in this service.

## Frozen procedure

| Stage | Complete UTC dates | Minimum distinct underlying events |
| --- | --- | --- |
| TRAIN | September 27–October 3 | 30 each day; 210 per family |
| CALIBRATION | October 4–6 | 30 each day; 90 per family |
| VALIDATION | October 7–13 | 30 each day; 210 per family |
| Future OOS | Seven complete UTC days starting the midnight after the actual persisted challenger lock | 30 each day; 210 per family |

A single registered batch runs at or after October 14, 01:00 UTC, using the fixed knowledge cutoff of that time. Five families are five hypotheses, never five independent observations. Missing labels by that cutoff, insufficient periods or failed provenance are data failures. All qualified decisions in the windows must be accounted for; no winner-only settled subset. Days are statistical blocks under an explicit independence assumption, not proof of independence. Independent review remains necessary.

The existing five formulas are fixed references. Each challenger only recalibrates its reference probability with logistic(a logit(p)+b+c). The grid for a and b is selected by TRAIN Brier score, and c by a separate CALIBRATION Brier score. Grids and deterministic tie-breaking are frozen in JSON. Validation outcomes never choose parameters. Both validation and prospective OOS must improve Brier/log loss over midpoint and ask, and Brier over the fixed family reference. Each baseline comparison requires a paired day sign-flip p-value <= 0.01; this reserves 0.05 across the five families. The independence assumption and economic evidence prevent these calculations alone from becoming model approval.

Prospective OOS predictions are appended in the original decision transaction, before market close and within the same fresh-quote deadline. OOS replay cannot generate missing predictions. Training and OOS member hashes remain linked to the candidate; later outcome conflicts or disqualifications revoke the effective evaluation status without rewriting history or retraining.

## Evidence and economics

The service reconstructs observations from native HTTP bodies and complete paginated scans, reconstructs predecision features from bound supplementary receipts, and replays authoritative final settlement receipts. Imported JSON, caller probabilities, self-certified flags and synthetic fixtures cannot enter the live coordinator. Tests use synthetic fixtures solely to verify software behavior.

Decisions preserve ticker/domain, times, source/candidate versions, complete feature hash, probabilities, original/refreshed quotes, spread, displayed liquidity, fee evidence, slippage policy, intended/accepted size and reasons. Outcome events link decision, authoritative receipt, label availability, Brier and market comparison. Gross/net PnL, costs, drawdown and reward stay null for rejected decisions. A per-decision prediction residual is recorded; calibration quality is the aggregate ten-bin ECE, not a single-trade claim.

The frozen economic reward uses U=$1 and a hypothetical $100 starting equity. It combines mean net PnL/U and market Brier advantage and positive-day consistency, minus ECE, drawdown fraction, sample variance, slippage risk surcharge, stale exposure and liquidity failure. Slippage is charged once in net PnL and explicitly penalized again as execution risk. Consistency is descriptive until independent-period review, and economic admission requires at least seven complete days. All weights are one. Control bypass disqualification cannot be offset by profits. An unresolved accepted position or undefined variance blocks aggregate reward. Diagnostic arithmetic never grants economic admission.

Current public evidence does not qualify fee class, slippage, account reconciliation, model approval or allocation. The service therefore retains rejected execution proposals and no accepted fill writer. Predictive learning can proceed when its frozen sample gates are met; economic learning and promotion remain blocked until independently bound evidence is supplied through a separately reviewed implementation. No placeholder zero is reported as realized PnL or reward.

No active research model exists at activation, so CHAMPION remains null. The five unchanged references are not silently promoted into a champion. A challenger must pass frozen training, validation, future OOS, market baseline, positive economics and independent review before a separately authorized manual replacement. No candidate can approve itself.

## Reports and finite stop

The service writes /data/atlas-v2/learning-reports/latest.json and immutable complete-day reports, with matching L_DAILY_LEARNING_REPORT ledger events. Daily counts use decision and label-availability times; cumulative metrics reapply current invalidation and disqualification state. Reports show qualified decisions/settlements, Brier versus both market baselines, calibration, reward/net PnL/drawdown or the explicit missing-evidence reason, candidate rejection and OOS status. The partial current day is labeled as such.

Only one batch and at most five challengers may be produced. No retraining occurs after a terminal state. Fully qualified evaluated failures produce NO_LEARNABLE_EDGE_DEMONSTRATED. Missing data produces DATA_QUALIFICATION_FAILED; interrupted training produces BATCH_INTERRUPTED. A predictive pass with unavailable economic proof produces BLOCKED_BY_EXTERNAL_ECONOMIC_EVIDENCE. October 25, 00:00 UTC is the finite deadline; incomplete OOS cannot extend it. Collection and reporting may continue, but the registered experiment does not restart.
