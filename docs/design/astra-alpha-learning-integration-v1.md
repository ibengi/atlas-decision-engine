# Astra Alpha Learning v1 — pre-wire readiness

Status: SHADOW ONLY. This document is for the `alpha/astra-learning-v1` branch and does not authorize broker writes, CAPITAL, or any change to the audited execution candidate.

## What is already proven

- Alpha Learning unit tests pass.
- The safety-boundary check proves the learning modules import no execution/broker modules.
- The durable report path writes `alpha_learning_report.json` atomically and leaves the append-only prediction ledger untouched.
- The memory CLI emits prior resolved cases only and emits no side, size, order, or execution instruction.
- The branch is additive relative to `claude/exciting-fermat-fmcoix`; existing Alpha Gateway/Service files are not modified.

## Requirements before wiring into `atlas-alpha-shadow`

### R1 — Durable Alpha state on Railway (BLOCKER)

The current `atlas-alpha-shadow` Railway service has no volume mount. `AlphaLedger`, costs, resolutions, learning memory, and the derived report are file-backed under `DATA_DIR`; without durable storage they can be lost on restart/redeploy.

Required before integration:

- attach a dedicated persistent volume to the Alpha service (do not share the execution engine's authoritative state volume),
- mount it at a stable path such as `/data`,
- point `DATA_DIR` to that mount,
- verify restart persistence with a write/restart/read shadow-only probe.

### R2 — A real Astra signal identity (BLOCKER)

`default_providers()` currently contains only `grok`, `gemini`, `openai`, and `atlas_quant`. Alpha Learning selects Astra rows by model name (`ASTRA_MODEL_SELECTOR`, default `astra`). Until a provider/model produces a validated `per_model` key matching that selector, Astra sample count will remain zero.

Required before integration:

- define the automated Astra forecasting source,
- ensure its validated model identity is stable and contains/matches the configured selector,
- keep the response on the existing probability-only schema and through the same validator,
- keep Astra completely outside the broker/execution process.

Do not silently relabel another model as Astra; model provenance must remain auditable.

### R3 — Automatic trusted resolution feed (BLOCKER FOR LEARNING)

The Alpha service can append an outcome with `attach_outcome()` / the `resolve` CLI, but the current learning loop does not automatically ingest market settlements. Without trusted resolutions, predictions accumulate but calibration, error memory, Brier/log-loss, and ROI do not learn.

Required:

- add a read-only settlement resolver or settlement feed,
- append exactly one immutable `RESOLUTION` row per prediction,
- make resolution idempotent and auditable by source,
- never update the original prediction row in place.

### R4 — Learning report refresh trigger

The report CLI is standalone. After R1-R3, generate the derived report after new resolution rows (or with a separate shadow-only periodic job). Report generation must never be in the execution critical path.

### R5 — Memory injection must be an explicit A/B arm

`memory_context()` is ready, but `build_prompt()` currently does not consume it. When memory is introduced, treat the memory-enhanced Astra forecaster as a distinct experimental arm/version so historical comparisons remain interpretable. Do not retroactively combine pre-memory and post-memory performance as if they were the same model.

### R6 — Baseline selector

The real quantitative provider identity is `atlas_quant`. Runtime/CLI defaults on this branch are pinned to `atlas_quant` so the Astra-vs-Quant ROI report does not accidentally compare against an empty baseline.

## Safe integration order

1. R1 persistent Alpha-only storage.
2. Restart-persistence proof.
3. R2 Astra forecasting source in SHADOW ONLY.
4. Confirm validated Astra predictions enter the immutable ledger.
5. R3 automatic resolution ingestion.
6. Generate learning report after resolutions.
7. Accumulate a statistically meaningful sample before allowing learned weights to influence the ensemble.
8. Introduce memory-enhanced Astra as a separate A/B model version.

At no point does this plan grant broker credentials or execution authority to the Alpha service.
