# Atlas Intelligence Network — Phase 1: SHADOW ONLY

Status: **implementation scaffold, no trade authority**.

This subsystem exists to measure whether external AI models add predictive
information to Atlas. It is deliberately outside the money path.

## Non-negotiable boundary

Phase 1 has no route to `RiskManager`, `PositionSizer`, `ExecutionEngine`,
`OrderManager` or any broker-write API. Provider output is research evidence
only.

The phase-1 process refuses to construct its policy if:

- `AI_MODE` is anything other than `SHADOW`, or
- `AI_CAN_INFLUENCE_TRADE=1`, or
- `AI_CAN_SIZE_POSITION=1`, or
- `AI_CAN_SUBMIT_ORDER=1`.

A future advisory/ensemble phase requires a separate reviewed change. It cannot
be enabled by flipping an environment variable in this implementation.

## Intended provider roles

- **Astra** — expensive deep challenger and difficult-contract analyst.
- **Gemini** — high-throughput screening and extraction.
- **Grok** — event/news/X context and freshness radar.
- **Claude** — engineering/audit by default; runtime adapter is optional later.
- **Atlas Quant** — remains the deterministic quantitative core.

No provider may publish an executable `BUY`, `SELL`, quantity, position size or
order payload through the normalized schema.

## Data flow

```text
market candidate
      |
      +--> provider adapters (async/cache later)
      |         |
      |         +--> normalized probability/confidence/abstention
      |
      +--> append-only ai_shadow_observations.jsonl

Atlas trading pipeline continues independently and unchanged.
```

The router is fail-soft: provider timeout, malformed output, budget exhaustion
or identity mismatch produces **missing intelligence**, not a trading-engine
failure.

## Normalized observation

Each provider opinion records at least:

- contract id
- provider/model id
- probability or explicit abstention
- confidence
- ambiguity flag
- evidence freshness
- latency
- estimated/actual API cost
- timestamp
- short rationale summary

Execution-shaped fields are rejected recursively from provider metadata.

## Cache and latency

The intelligence cache returns only observations inside their provider TTL.
Stale opinions remain inspectable for telemetry but are not returned as fresh.
The objective is to prepare context before a time-critical Atlas decision so AI
latency does not sit in the broker path.

Required production metrics once adapters exist:

- provider latency p50/p95
- cache hit ratio
- candidate age at analysis
- price/edge movement between candidate detection and provider completion
- provider cost per candidate

## Budget controls

`DailyBudgetManager` reserves estimated spend before a call and settles to actual
cost afterwards. Global and per-provider daily caps prevent runaway calls.
Budget exhaustion skips that provider without stopping Atlas.

Suggested initial operator limits (not a profitability claim):

```text
AI_DAILY_BUDGET_USD=25
ASTRA_DAILY_BUDGET_USD=10
GEMINI_DAILY_BUDGET_USD=5
GROK_DAILY_BUDGET_USD=5
```

Provider billing remains authoritative; Atlas budget counters are an operational
safety guard.

## Durable evidence

`IntelligenceEvidenceStore` appends provider predictions to
`DATA_DIR/ai_shadow_observations.jsonl`. This file is separate from trade,
position, risk and order state. Rows include an evidence hash and are never used
to authorize a trade in phase 1.

After contract resolution a later scorer will join predictions to outcomes and
compute:

- Brier score
- log loss
- calibration by probability bucket
- disagreement with Atlas and market-implied probability
- hypothetical incremental EV/PnL
- cost-adjusted AI value
- latency-adjusted AI value

## Promotion rule

No model gains trade influence because its prose appears convincing. Promotion
from SHADOW to any advisory/ensemble role requires an independently reviewed
change plus empirical evidence that the model improves out-of-sample decision
quality after latency and API cost.

The target sequence is:

```text
SHADOW -> measured challenger -> advisory proposal -> independently reviewed
ensemble experiment -> only then possible CAPITAL influence
```

Broker writes remain exclusively owned by Atlas's existing deterministic write
boundary throughout.
