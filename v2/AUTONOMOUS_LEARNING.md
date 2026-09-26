# Atlas V2 autonomous learning authorization — 2026-09-26

The operator authorizes `LIVE_MARKET_LEARNING`: continuous public live market
observations and full shadow proposals, with `CAPITAL=OFF`, `BROKER_WRITES=0`,
`REAL_ORDERS_SUBMITTED=0`, and `PROD_ACCESS_MODE=READ_ONLY`.
This authorization does not grant financial execution, candidate self-approval,
or self-promotion. No risk limit or frozen Alpha Lab formula changes.

## Runtime

Opt in on the existing dedicated V2 collector only:

```
ATLAS_V2_MODE=LIVE_MARKET_LEARNING
ATLAS_V2_QUALIFICATION_ON_START=1
CAPITAL=OFF
BROKER_WRITES=0
REAL_ORDERS_SUBMITTED=0
PROD_ACCESS_MODE=READ_ONLY
```

The existing `/data/atlas-v2/observations.sqlite` remains unchanged in schema.
Qualification receipts use `qualification.sqlite`; prospective decisions use
`learning.sqlite` on the same dedicated persistent V2 volume. The process has
public GET readers and no financial mutation adapter or account credentials.
Startup rejects inconsistent authority flags. Release source hashes and the
exact deployed SHA remain bound by the existing image gate.

Scope is the five unchanged registered KXBTC15M hypotheses and their fixed
240–300 second cohort. There is no approved active model or champion today.
Unapproved hypotheses generate diagnostic proposals. Every such proposal is
rejected for guarded hypothetical execution until model approval, reconciliation,
allocation and cost evidence are independently satisfied. A candidate lock alone
is not approval. This release cannot turn a proposal into an accepted trade.

Every prospective proposal records timestamp, ticker/event/domain, source and
formula version, model and market probability, decision and refreshed quotes,
spread, fee/slippage assumptions, proposed and accepted size, rejection reasons,
and explicit settlement/PnL/excursion fields. Missing data is null with a reason;
zero is never substituted. Settlements and diagnostics append separately rather
than mutate the original decision. Historical rows are not relabelled prospective.

## Reward contract

The primary economic component is selected-side hypothetical settled payout
minus frozen entry cost, fees and the declared slippage stress, in dollars.
Losses are negative. Costs are deducted once. Proper probability scores and
improvement over the paired market baseline are separate diagnostic components;
Brier scores are not silently added to dollars. The current conservative
slippage assumption adds a tick and adverse decision-to-refresh movement on top
of refreshed ask; this is a stress charge, not measured execution slippage.

Drawdown, PnL variance, calibration, stale prices, liquidity failures, slippage,
and control violations remain visible components or eligibility failures.
Consistency requires independent-period evidence; calendar labels alone do not
prove independence. No arbitrary composite weights are fitted after outcomes.
The offline economic calculator is diagnostic: hashes bind its inputs but do not
authenticate their authority or qualify costs. It is not an approval mechanism.

Rejected proposals receive no trading PnL or economic reward. Predictions may
receive outcome-bound diagnostic scores after authoritative settlement, including
rejected predictions, to avoid selecting only apparent winners. An unavailable or
conflicting final label prevents scoring/admission. Settlement receipt arrival is
the knowledge time; economic settlement time cannot backdate a reward.

Minute polling cannot establish true intratrade extrema. Observable quote-path
coverage is labelled sampled. Without an accepted hypothetical fill, execution
MAE/MFE remain null. There is no fabricated fill at midpoint.

Attempts to bypass reconciliation, price/spread/depth limits, duplicate controls,
drawdown or model approval permanently disqualify that candidate identity.
Ordinary guard rejection is not evidence of a bypass attempt. Disqualification
persists across restarts and cannot be erased by a later winning outcome.

## Offline learning and promotion

The authorized lifecycle is:

OBSERVE → SETTLE → REWARD → DATASET → periodic offline retraining request
→ challenger → validation → prospective OOS → independent promotion decision.

Complete-day dataset checkpoints and retraining-readiness records are periodic,
never per-trade changes to an active candidate. At this release, requests remain
blocked by missing qualified economic/training data, independently defined
train/validation splits and a frozen training recipe. Training itself does not
require an existing champion or completed prospective OOS; those are later
promotion-stage concerns. No trainable estimator or authorized retraining recipe exists in the
five fixed-formula experiments; this release does not pretend that a blocked
request trained a new model. The existing preregistered splits, multiplicity,
minimum duration and fresh prospective OOS requirements remain in force.

A challenger must be a new immutable artifact with frozen data, features,
parameters, reward policy and training cutoff; validation and OOS must remain
unseen during fitting. No runtime method promotes or approves a candidate.
An independent approver must authenticate exact artifact bindings outside the
research process before any future champion replacement. V1, main, broker
credentials and historical financial ledgers are outside this change.

## Verification

```
PYTHONPATH=v2 python -m unittest discover -s v2/tests
python v2/mutate.py
```

Synthetic tests establish software behavior, not market profitability. Runtime
readiness must be established separately from native deployment/source identity,
learning ledger anchors, prospective decision counts and settlement diagnostics.
