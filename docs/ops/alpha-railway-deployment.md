# Deploying the Alpha Shadow Service on Railway

Operator runbook. Nothing here is applied by code — every step is a
deliberate action in the Railway dashboard.

**Do not remove the Kalshi credentials from the Engine service.** The engine
needs them. What moves is the three AI keys, and only after Alpha is proven.

## Current state that this fixes

The AI keys are on `ibengi/atlas-decision-engine` — the ENGINE service,
which also holds `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY`,
`ALLOW_ORDER_SUBMISSION` and `KALSHI_ENV_CONFIRM`. Alpha will refuse to
start there, by design (§27). One of them is also misnamed:
`Gemini API Key` (with spaces) cannot be read as an environment variable by
any process; it must be `GEMINI_API_KEY`.

## Target

| | ATLAS ENGINE (exists) | ATLAS ALPHA SHADOW (to create) |
|---|---|---|
| repo | `ibengi/atlas-decision-engine` | same |
| start command | unchanged | `python tools/alpha_service_run.py` |
| volume | existing, `/data` | new, `/data` |
| holds | `KALSHI_*`, execution/risk config, `RESEARCH_API_TOKEN` | the three AI keys, Alpha config |

## Migration checklist

1. **Create the Alpha service** from the same repository and branch.
   Set its start command to `python tools/alpha_service_run.py`
   (the `alpha` process type in the `Procfile`). Do **not** change the
   Engine's start command, and do **not** change the Dockerfile `CMD` —
   that stays the engine, so an Engine redeploy can never start Alpha.

2. `XAI_API_KEY` — add to the Alpha service.

3. `GEMINI_API_KEY` — add to the Alpha service, **spelled exactly that
   way**. Not `Gemini API Key`.

4. `OPENAI_API_KEY` — add to the Alpha service.

5. **Verify ZERO broker credentials on Alpha.** Its variable list must
   contain none of: `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY`,
   `KALSHI_DEMO_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY`, `KALSHI_PROD_KEY_ID`,
   `KALSHI_PROD_PRIVATE_KEY`, `ALLOW_ORDER_SUBMISSION`, `KALSHI_ENV_CONFIRM`,
   `LIVE_TRADING`, `LIVE_TRADING_CONFIRMED`, `LIVE_BROKER_WRITES_AUTHORIZED`,
   `DEMO_TRADING`, `MODEL_APPROVED_FOR_LIVE`, `ALLOW_FALLBACK_CAPITAL`,
   `PROD_ACCESS_MODE=CAPITAL`, `EXECUTION_MODE=live`.
   The service enforces this itself: it exits 78 and logs
   `ALPHA_STARTUP_REFUSED_BROKER_CREDENTIALS` with the offending NAMES.
   A crash-loop on that line is the guard working, not a bug.

   Also set on Alpha:
   ```
   DATA_DIR=/data
   ALPHA_GATEWAY_ENABLED=true         # off by default; enabling starts paid calls
   ALPHA_FEED_TRANSPORT=http
   ALPHA_RESEARCH_FEED_URL=http://<engine-private-domain>:8080
   ALPHA_RESEARCH_API_TOKEN=<same value as the Engine's RESEARCH_API_TOKEN>
   ```
   On the Engine, `RESEARCH_FEED_ENABLED=true` so it emits candidates at all.

6. **Run the real smoke tests from inside the Alpha service**, not from a
   development environment:
   ```
   python tools/alpha_smoke_test.py
   python tools/alpha_smoke_test.py --json      # for the record
   ```
   One bounded call per provider against a fixture market that does not
   exist, charged to the same daily budget. It also prints the observed
   `ALPHA_XAI_COST_TICKS_PER_USD` — set it afterwards so xAI's billed cost
   can be converted; until then the raw tick count is kept unconverted.

7. **Verify all three report PASS.** A PASS requires a real vendor
   response; nothing synthetic can produce one. Any FAIL, `REFUSED_*` or
   `REACHABLE_BUT_INVALID` means that provider is EXCLUDED — do not
   proceed on two out of three, and do not substitute a different model.
   Check first that Railway's egress reaches `api.x.ai`,
   `generativelanguage.googleapis.com` and `api.openai.com`.

8. **Start the bounded automatic shadow** (`python tools/alpha_service_run.py`,
   which is already the start command). Do not raise the cost caps for the
   first session: $0.25 per analysis, $2.00 per provider-hour, $20.00 per
   day.

9. **Remove `XAI_API_KEY`, `GEMINI_API_KEY` / `Gemini API Key` and
   `OPENAI_API_KEY` from the Engine service** — only now, once Alpha is
   proven working. Doing it earlier leaves no working configuration if
   step 7 fails.

10. **Verify the Engine still operates normally**: it redeploys on the
    variable change, and nothing in the engine reads those keys.

11. **Verify Alpha still has no broker authority**: `health` reports
    `broker_credentials_present: false`, `capital_authority: false`,
    `execution_imports: 0`.

## What Alpha may and may not write

Alpha owns, under its own `DATA_DIR`: `alpha_processed.jsonl`,
`alpha_calibration_ledger.jsonl`, `alpha_cost_ledger.jsonl`,
`alpha_observations.jsonl`, `alpha_telemetry.json`.

Alpha must never write `orders_state`, `positions_state`, the risk or equity
ledgers, execution state, broker reconciliation state, or CAPITAL state. It
has no code path to any of them, and with the HTTP feed transport it has no
write path to the engine's volume either.

## Rollback

Delete the Alpha service. The engine is unaffected: it publishes candidates
and never learns whether anything read them.
