# Astra Alpha Learning v1 — pre-wire readiness

Status: **SHADOW ONLY**. This document applies only to `alpha/astra-learning-v1`. It does not authorize broker writes, CAPITAL, or any change to the audited execution candidate, `main`, or the production engine.

## What is already proven

- Alpha Learning unit tests pass.
- The safety-boundary check proves the learning/readiness modules import no execution or broker modules.
- `alpha_learning_report.json` is published atomically and the append-only prediction ledger is not rewritten.
- The memory CLI emits only prior resolved cases and emits no side, size, order, or execution instruction.
- Settlement ingestion is append-only: matching duplicates are idempotent, unknown predictions are rejected, and conflicting outcomes are surfaced without overwriting the first resolution.
- The candidate producer is fail-closed at the source: a market fact the exchange did not publish is refused and counted, never substituted, and every emitted fact carries the source key it was read from.
- Feed readiness is fail-closed: missing immutable market facts are reported rather than reconstructed from ticker text, spread arithmetic, a relative horizon, settlement time, or a generic liquidity score.

## Requirements before wiring into `atlas-alpha-shadow`

### R1 — Durable Alpha-only storage on Railway — INFRASTRUCTURE PRESENT, RESTART PROOF OUTSTANDING

The `atlas-alpha-shadow` service now has a dedicated persistent volume mounted at `/data`, with no staged volume change. This removes the original infrastructure blocker and keeps Alpha state separate from the execution engine's authoritative volume.

Still required before declaring persistence proven:

- confirm `DATA_DIR` continues to resolve to `/data` after a service restart/redeploy,
- persist an Alpha-owned test artifact or real shadow ledger row,
- restart/redeploy the Alpha service,
- prove the exact row/report is still present and readable afterward.

A telemetry file being writable on `/data` proves the mount is usable; it is not by itself proof that the learning ledger survives a restart.

### R2 — Complete immutable candidate source — CODE PATH CLOSED, LIVE SAMPLE OUTSTANDING

The original finding stands and is unchanged: `/api/research/v1/decisions` cannot
produce an `atlas-alpha-v2` snapshot without inventing facts, because a decision
record is what the engine *concluded*, not what it *saw*.

What was wrong was the assumed remedy. A neutral candidate boundary already
existed — `research_feed.py` (producer, engine side), a bounded spool,
`/api/research/v1/candidates`, and `alpha_consumer.py` (consumer, Alpha side).
It was not missing. It was **untruthful**: both ends substituted values for facts
the source had never recorded.

| substitution that used to happen | what it actually meant |
| --- | --- |
| `resolution_source` defaulted to the literal `"kalshi"` | the venue is not the settlement authority |
| `volume` / `open_interest` fell back to `0.0` | "not reported" was recorded as "zero" |
| `question` fell back to the ticker | a symbol is not the contract question |
| `expected_resolution_time_utc` fell back to `close_time` | a close is not a settlement |
| `resolution_rules` fell back to `""` | no rules is not empty rules |

Each of those turns "the exchange did not tell us" into a fact Alpha would later
calibrate against, and a model scored on invented premises looks better than it
is. That is the one error a calibration subsystem cannot absorb.

**The rule now enforced in code.** Every field is one of:

1. directly observed and persisted, carrying the source key it was read from;
2. explicitly listed in `unavailable_fields`;
3. omitted because it is optional (`event_id`, catalyst).

There is no fourth branch. In particular there is no "derive it, it is
mathematically plausible" branch: a full book is never inferred from
`entry_ask + spread`, an expiry is never derived from `recorded_at +
minutes_remaining`, a missing book side is never invented, market metadata is
never manufactured, and ticker text is never promoted to canonical contract text
or resolution rules.

**How it is enforced.**

- Feed schema `atlas-research-candidate-v2`. `v1` records are **refused, not
  migrated** — a v1 record was permitted to carry the substitutions above, so it
  cannot be relabelled truthful and must be re-observed.
- `REQUIRED_FIELDS` is the full snapshot-minting set. A record missing any of it
  never reaches the spool: the producer refuses and counts the refusal
  (`refused_incomplete`), because a refusal at ingest is much harder to trace
  back to the market that caused it and, on a bounded spool, displaces a record
  that was complete.
- `MARKET_SOURCES` / `BOOK_SOURCES` list, per field, only the source keys that
  are genuine aliases *for the same fact*. A key naming a different fact is a
  derivation, and a derivation presented as an observation is the failure mode
  this boundary exists to prevent.
- Each record carries `field_provenance` (field → the source key it was read
  from) and `unavailable_fields`. Both are inside `record_sha256`, so a record
  re-attributed after the fact fails its own checksum.
- The consumer supplies **no default for any market fact** and refuses a record
  whose provenance does not name a real source key for every required field.
- `alpha_feed_readiness.assess_record` honours the producer's own provenance:
  an unattributed required field is reported missing and logged as a prohibited
  inference, whatever value happens to sit in the record.

**Direction is unchanged and asserted, not described.** ENGINE OBSERVATION →
immutable read-only research evidence → Alpha Shadow → prediction ledger → later
resolution. `tests/test_research_feed_boundary.py` pins the producer's import
list with an allow-list, so the boundary cannot widen by accident; the engine may
know the neutral producer, and may never know Alpha.

**What remains outstanding for this requirement** is no longer a code path but a
live sample: the exchange must actually publish `rules_primary` and
`settlement_sources` for the markets being scanned. Where it does not, those
markets are correctly refused and Alpha receives nothing for them — which is the
intended behaviour, not a regression. `tools/alpha_feed_readiness.py` remains the
release gate: wiring is not ready until a real sampled feed returns
`all_records_ready=true`.

### R3 — A real Astra forecasting identity — BLOCKER

`default_providers()` currently contains `grok`, `gemini`, `openai`, and `atlas_quant`. Alpha Learning selects Astra rows by model name (`ASTRA_MODEL_SELECTOR`, default `astra`). Until a genuine automated forecasting source produces a validated model identity matching that selector, Astra sample count remains zero.

Required before integration:

- define the automated Astra forecasting source,
- keep its validated model identity stable and auditable,
- keep the response on the existing probability-only schema and through the same validator,
- keep Astra completely outside the broker/execution process.

Do not silently relabel another model as Astra.

### R4 — Trusted automatic resolution source with deterministic prediction binding — BLOCKER

`alpha_resolution_ingest.py` and `tools/alpha_resolution_ingest.py` already provide the append-only integrity boundary. They require `prediction_id`, `outcome`, `source`, and optionally `resolved_at`.

The engine's research settlement surface can expose authoritative settled-trade evidence, but Alpha still needs a deterministic, persisted link from that evidence to the exact Alpha prediction. Contract/ticker equality alone is not sufficient when multiple snapshots of the same contract can exist.

Required before automatic learning:

- preserve a stable producer/source record identifier on each Alpha prediction,
- define a deterministic join from trusted settlement evidence to the intended prediction(s),
- reject ambiguous joins rather than choosing one,
- convert only authoritative terminal YES/NO facts into the ingestion schema,
- operationally surface rejected/conflicting settlement facts,
- keep the original prediction row immutable.

### R5 — Learning report refresh trigger

The report CLI is standalone. After R1-R4 are proven, generate the derived report after new resolution rows or from a separate shadow-only periodic job. Report generation must never enter the execution critical path.

### R6 — Memory injection must be a distinct A/B arm

`memory_context()` is ready, but `build_prompt()` does not consume it. When memory is introduced, treat the memory-enhanced forecaster as a distinct experimental model/version so pre-memory and post-memory performance are not silently pooled.

### R7 — Baseline selector

The quantitative provider identity is `atlas_quant`. Runtime/CLI defaults are pinned to `atlas_quant` so Astra-vs-Quant value is not compared against an empty baseline.

## Safe integration order

1. Complete the R1 restart-persistence proof on the Alpha-only volume.
2. Make the real candidate source pass the fail-closed R2 readiness gate.
3. Add the genuine Astra forecasting source in SHADOW ONLY and verify its stable model identity.
4. Confirm validated Astra predictions enter the immutable ledger with a durable source-record identity.
5. Connect trusted read-only settlement evidence through the deterministic R4 binding and append-only resolution ingestion.
6. Generate the durable learning report after resolutions.
7. Accumulate a statistically meaningful sample before learned results influence any ensemble weighting.
8. Introduce memory-enhanced Astra only as a separately versioned A/B arm.

At no point does this plan grant broker credentials or execution authority to the Alpha service.
