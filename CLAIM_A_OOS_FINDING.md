# CLAIM A — out-of-sample edge: FAIL

Counter-auditor finding, 2026-09-26. Records a measurement, changes no
release artifact and approves nothing.

## The claim under test

> The decisive claim is not "Atlas predicts reasonably well." It is: does
> the model beat the market baseline out-of-sample on qualified
> observations? If not, fail this gate.

`MODEL_VALIDATION_GUIDE.md` states the same rule: the model's Brier MUST
beat the market baseline (`yes_ask/100`) out of sample, "sinon le marche
predit mieux que le modele et il n'y a aucun edge a exploiter."

Until today the rule had been stated for weeks and never computed. It has
now been computed.

## Result

| quantity | value |
|---|---|
| settled predictions scored | 13,490 |
| TEST slice (chronological last 20%) | 2,698 |
| `brier_model - brier_market_baseline` on TEST | **+0.0205** |
| gate `model_beats_market_baseline_out_of_sample` | **false** |
| verdict | **FAIL** |

The sign is what decides. Positive means the model's squared error is
LARGER than the market's: on out-of-sample observations the ask price
predicts the outcome better than the model does.

## The sample is sound, which is what makes the failure count

A failure is only worth acting on if it is a failure of the model rather
than of the measurement. Four things had to be ruled out, and were.

**It is not sample collapse.** `model_validation.json` records 108 settled
predictions; the engine logs a running total in the thousands. The census
resolves the gap: of 13,526 records, 13,519 are settled and **all 13,519
are usable**. The only attrition is 7 rows still pending. Zero rows were
dropped for a missing model probability or a missing market ask, so the
"the quotes were never journalled" explanation is dead.

**It is not quarantine contamination.** The measured series is KXBTC15M —
13,526 records, the whole sample. This is not the KXBTCD daily series with
its documented label corruption (62 events settled `result="no"` while BTC
traded above strike). The failure is on the 15-minute series the engine
actually trades.

**It is not a rigged baseline.** The baseline is scored on `yes_ask/100`,
the ASK — the price a buyer pays, systematically above mid by roughly half
the spread. That handicaps the market, not the model. The realised yes rate
is 0.4967 against a mean market-implied 0.5077, the ~1.1-point gap the ask
side predicts. The model loses to a deliberately handicapped baseline.

**It is not noise.** +0.0205 against a ~0.25 Brier scale is an 8.2%
relative degradation on 2,698 observations. Clustering matters here — 25
settlement dates, up to 746 rows sharing one — so the effective sample is
far below 2,698 and a *favourable* result of this size would not have been
believable. But clustering cannot rescue a failure: the gate requires the
model to beat the baseline, and it does not.

## Independently verified

Reproduced from the tools' own arithmetic rather than taken on trust:

* `split_chronological(13490, (.6,.2,.2))` → train 8,094 / val 2,698 /
  **test 2,698**, matching the reported slice exactly. The number came
  through the real code path.
* Census attrition closes: 13,526 − 13,519 = 7, and settled − usable = 0.
* Both totals sit on the settlement timeline read directly from the
  Railway deploy logs of deployment `f15db78b-8061-4403-971c-045afec8418a`:
  `[SHADOW] ... total regle: 13490` (~16:0x) and `... 13519` (16:53Z), the
  latter matching the census's last usable timestamp 16:53:58Z.

Not verified: the Brier values themselves, which need the raw file. Two
caveats are recorded rather than waved past — the two reports describe
snapshots ~50 minutes apart (13,490 vs 13,519 settled), so the census's
hash does not bind the Brier numbers; and the quoted `dataset_sha256` is 65
hex characters where SHA-256 is 64, so it is a transcription, not a usable
binding. Neither changes the sign of the result.

## A second finding, surfaced by the census

The measured sample carries `model_version = btc15m-v1.0-ref`. That is what
the engine stamps at runtime (`btc_probability_model.py:26` →
`strategy_router.py:343`).

`model_validation.json` — the artifact `model_gatekeeper.py` reads to decide
promotion — declares `model_version: "btc15m-baseline-0.1"` at commit
`acb9c01b`.

**The release-bound validation artifact does not describe the model in
production.** This is adversarial check 14 (candidate/model lineage
mismatch) and 15 (stale model approval artifact), and it is independent of
CLAIM A: even had the Brier result been favourable, it would have been
favourable for a model the approval artifact does not name. The artifact
also still records `shadow_predictions_settled: 108` and
`brier_beats_market_baseline_out_of_sample: "non mesure"`, both now stale.

Correcting it is implementation work, not the auditor's. It must not be
corrected by setting `approved: true` — the measurement says the opposite.

## Consequence

Execution costs (fees, slippage, crossing the spread) can only subtract
from predictive edge. Predictive edge is negative before any of them are
charged. A LIVE canary on this model would therefore have negative expected
value by measurement, not by suspicion.

CLAIM A = **FAIL**. Blocker 1 moves from "never measured" to "measured and
negative", which is a stronger result: the earlier verdict rested on absent
evidence, this one rests on evidence.

Verdict unchanged: **STOP_AND_PARK_ATLAS**.

## What this does not say

It does not say the model is unfixable, that no edge exists on Kalshi, or
that the engineering is wasted — the safety machinery held throughout and
the shadow instrumentation is exactly what produced this answer. It says
`btc15m-v1.0-ref` has no measurable out-of-sample edge on KXBTC15M over
2026-09-01 → 2026-09-26, and must not be given capital.
