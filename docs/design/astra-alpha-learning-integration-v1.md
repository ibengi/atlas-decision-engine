# Astra Alpha Learning v1 — pre-wire readiness

Status: **SHADOW ONLY**. This document applies only to `alpha/astra-learning-v1`. It does not authorize broker writes, CAPITAL, or any change to the audited execution candidate, `main`, or the production engine.

## What is already proven

- Alpha Learning unit tests pass.
- The safety-boundary check proves the learning/readiness modules import no execution or broker modules.
- `alpha_learning_report.json` is published atomically and the append-only prediction ledger is not rewritten.
- The memory CLI emits only prior resolved cases and emits no side, size, order, or execution instruction.
- Settlement ingestion is append-only: matching duplicates are idempotent, unknown predictions are rejected, and conflicting outcomes are surfaced without overwriting the first resolution.
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

### R2 — Complete immutable candidate source — BLOCKER

The currently observed production decision/shadow records are not sufficient to build an `atlas-alpha-v2` snapshot without inventing facts. The readiness preflight intentionally refuses those records when they lack directly persisted fields such as contract metadata, resolution rules/source, exchange volume/open interest, absolute close/resolution times, or the full contemporaneous order book.

Required before wiring:

- supply every field required by `alpha_snapshot.build_snapshot` directly from a producer/exchange record,
- keep a stable producer record identity so the Alpha prediction can later be linked to settlement evidence,
- do not infer a full book from `entry_ask + spread`,
- do not derive close/expiry from `recorded_at/ts + minutes_remaining`,
- do not turn ticker parsing into canonical contract text or resolution rules,
- do not relabel a generic liquidity/ranker score as exchange volume/open interest.

`tools/alpha_feed_readiness.py` is the release gate for this requirement. Wiring is not ready unless the real sampled feed returns `all_records_ready=true`.

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
