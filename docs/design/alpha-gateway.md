# AI Alpha Gateway v1 — design

**Status: SHADOW ONLY. CAPITAL AUTHORITY: NONE. BROKER WRITE AUTHORITY: NONE.**

The question this subsystem exists to answer:

> Do independent AI and quantitative probability estimates create
> persistent, measurable and cost-adjusted alpha relative to Kalshi prices?

It does not exist to force trades, and in v1 it cannot produce one.

## 1. Pipeline

```
Market Scanner → Candidate Filter → Immutable Market Snapshot
  → Alpha Gateway → Parallel AI Analysis → Signal Validator
  → Meta Alpha Engine → Shadow Opportunity Record → Calibration Ledger
```

| Stage | Module |
|---|---|
| Immutable snapshot | `alpha_snapshot.py` |
| `atlas-alpha-v2` schema + validator | `alpha_schema.py` |
| Provider abstraction + 4 adapters | `alpha_providers.py` |
| Parallel dispatch, timeouts, market movement | `alpha_dispatcher.py` |
| Ensemble, disagreement, uncertainty, edge | `alpha_meta.py` |
| Cost ledger + calibration ledger + metrics | `alpha_ledger.py` |
| Orchestration and terminal states | `alpha_gateway.py` |
| Operator entry point | `tools/alpha_shadow_run.py` |

## 2. The hard safety boundary

`AlphaGateway` holds no broker client, no order manager, no risk manager
and no equity ledger. No `alpha_*` module imports `order_manager`,
`execution_engine`, `kalshi_client`, `position_manager`, `position_sizer`,
`risk_manager`, `equity_ledger` or `trade_logger`, and **no execution module
imports an `alpha_*` module either**. Both directions are enforced by AST
inspection in `tests/test_alpha_safety_boundary.py`, which discovers the
module list rather than hard-coding it, and which includes a test proving
the deny-list can actually fail.

That is why the gateway is **not wired into `ExecutionEngine`'s cycle**. The
strongest available form of "there is no execution path" is that the money
path does not import this subsystem at all. It runs out of band over a
candidate file and writes only its own append-only ledgers; two processes
sharing a directory, not one process sharing a call stack. Wiring it in is a
later decision that requires the section 21 evidence first.

**No terminal state means TRADE.** `EXECUTABLE_STATES` is empty and asserted
empty, so a future state cannot be quietly added as actionable.

## 3. The immutable snapshot

Four models are asked the same question about the same market. If any of
them can change the question, the four answers are not comparable, the
ensemble is meaningless, and the ledger records a prediction about a market
that never existed.

So the snapshot is a frozen dataclass whose **identity is derived from its
content**:

```
market_snapshot_id = "snap-" + sha256(canonical content)[:24]
```

A model that alters a field and echoes the old id fails on the id it quotes.
One that alters a field and recomputes the id fails on the id the gateway
expects. `verify()` re-derives the identity, so tampering is detectable
across a serialization boundary too, and the gateway calls it **before a
single token is spent**.

Timestamps are second-resolution, so the snapshot time is floored and the
analysis deadline ceiled. Without that the two can truncate into the same
second and the analysis budget silently becomes zero.

## 4. Latency classes and catalyst expiry

`FAST | MEDIUM | DEEP` is derived from time to expected resolution. The
boundaries and the deadlines are configuration (`CFG.ALPHA_*`), not
constants: a latency policy that needs a deploy to change is one nobody
tunes.

Effective validity is computed in exactly one place
(`MarketSnapshot.effective_valid_until`):

```
MIN(model valid_until, analysis deadline, next_catalyst - safety buffer)
```

A caller that forgets the catalyst bound cannot exist, because there is
nowhere else to ask. A catalyst inside the analysis window shortens the
window; a catalyst already inside the safety buffer means there is no
analysis window at all, and `build_snapshot` says so rather than dispatching
four providers against a deadline that has passed.

## 5. Parallel dispatch

All providers are submitted at once to a `ThreadPoolExecutor` — the same
concurrency primitive `market_scanner` and `execution_engine` already use.
The wall-clock cost of a cycle is the **analysis budget**, not the sum of
the providers and not even the slowest one.

The deadline is enforced **twice**, deliberately:

1. each adapter receives the remaining budget as its own request timeout;
2. the dispatcher stops collecting at that deadline regardless, and anything
   still running is excluded as `ANALYSIS_TIMEOUT`.

The second exists because relying on the adapter alone means any provider
that forgets a timeout — or an in-process estimator with no transport at all
— can hang the cycle. The pool is therefore *not* used as a context manager:
`__exit__` calls `shutdown(wait=True)`, which would wait for the slowest
worker whatever deadline we set.

**A missing model output is NO SIGNAL.** It is never 0.5. Rejected signals
carry `p_yes = None`, so there is no number for a caller to pick up by
accident — treating a failure as a midpoint would require inventing the
value.

## 6. Weighting

Permanent equal weighting is refused, and so is early performance-chasing.

| Term | Source |
|---|---|
| prior | equal across models, capped |
| earned | this model's Brier in this category, **only** above `ALPHA_CALIBRATION_MIN_SAMPLES` |
| quality | evidence × completeness |
| freshness | how much validity is left |
| latency | how much of its budget the model used |
| interval | narrower interval, more actionable weight (§11) |

Below the sample threshold the earned multiplier is exactly 1.0 — no credit
and no penalty. Twenty resolved markets is a mood, not a calibration.

Weights are then capped and floored by **water-filling**: a model that hits
a bound is pinned there and the remaining mass is redistributed among the
models still free. Clip-and-renormalise does not work — renormalising after
clipping re-inflates the clipped value, converging just *above* the cap and
violating the policy by a fraction of a percent. When the configured cap is
arithmetically infeasible (two models cannot both be ≤ 0.4), it is relaxed
to the midpoint between the equal share and 1.0, not to the equal share
itself: pinning everything at 1/n would force exactly the permanent
uniformity §8 forbids.

No provider name appears anywhere in `alpha_meta.py`; a test asserts it.
Specializations are learned from the ledger or they are not claimed.

## 7. Edge

Both numbers are reported, because the gap between them is the finding:

```
raw_edge         = P_META - ask                    (yes)
                 = (1 - P_META) - ask              (no)

shadow_net_edge  = raw_edge
                   - spread_cost - estimated_fees - estimated_slippage
                   - uncertainty_penalty - latency_penalty
                   - inference_cost_penalty
```

The side is chosen on **net** edge, not raw edge. An ensemble that beats the
market by two cents and costs three cents to produce has no alpha, and only
the second number says so.

## 8. Ledgers

`alpha_calibration_ledger.jsonl` and `alpha_cost_ledger.jsonl` are
append-only, fsynced, one JSON object per line.

A prediction is one immutable line; a resolution is a **second** line
referring to it; the resolved view is derived by replay. A prediction row
updated in place after the outcome is known cannot be distinguished from one
that was always right, and every calibration number derived from that file
becomes unfalsifiable. Scores are computed at read time, never stored — a
stored score can drift from the prediction it grades.

A corrupt row in the middle of the file is skipped and reported, never
treated as end-of-file: stopping there would silently shorten the history,
which is how a calibration number quietly improves.

### Costs are recorded, prices are not invented

Every invocation records provider, model, input/output tokens, cost and
latency — including the ones that failed, because asking costs something
too.

A model with no applicable rate is `cost_priced: false`, and `metrics()`
**withholds** the net-of-inference-cost figure and says why rather than
guessing a per-token price — that price would silently decide the one
question this subsystem exists to answer. Since phase 4 the shipped card
carries operator-supplied rates for the three live models (§20), so the
withholding path is now the exception rather than the default; it still
governs any model outside the card or outside its validity window.

## 9. Configuration

All under `CFG.ALPHA_*` in `config.py`. `ALPHA_GATEWAY_ENABLED` is read
through `_env_gate` (strict, fail-closed) and defaults to **false**:
enabling it starts paid inference calls against third-party APIs, which is
an operator decision, not a deployment side effect.

### Endpoints and model ids are unverified defaults

`ALPHA_GROK_BASE_URL`, `ALPHA_OPENAI_BASE_URL`, `ALPHA_GEMINI_BASE_URL` and
the matching `_MODEL` values default to the shapes these vendors are
documented to use, but **this repository cannot confirm them and they
change**. Verify each against the vendor's current API reference before
enabling the gateway anywhere, and correct them by configuration rather than
by editing the adapters. A wrong endpoint is not dangerous here — it
produces a provider failure, which is an excluded signal, never a
probability.

## 10. Secrets

`XAI_API_KEY`, `GEMINI_API_KEY` (or the longer `GOOGLE_GEMINI_API_KEY`),
`OPENAI_API_KEY` are read from the environment at call time and sent in one
header. They are never written to a source file, a log line, a telemetry
counter, an API response or a persisted row. Gemini's key goes in `x-goog-api-key`, not a
query string, because a key in a URL ends up in every access log on the
path. Error bodies are truncated and redacted before they can reach a log
line — vendors echo request headers into error payloads often enough that
printing one verbatim is a credible way to leak a key.

A response body is not the only route out. A transport, a proxy or a vendor
SDK can raise an **exception whose message quotes the request**, headers
included, and that message goes straight into `meta["error"]`, which is
returned, logged and printed by the smoke test. So `redact()` strips every
configured credential value — not just the current provider's, since a
shared session or a proxy error can quote another vendor's header — from
every string leaving the adapter on the failure path. Redaction is not
silence: the failure is still reported, with `<redacted:XAI_API_KEY>` where
the value was, so the diagnostic survives. A value shorter than 8 characters
is not treated as a secret, because redacting a short string would destroy
unrelated messages.

Tests assert no secret appears in a log, an exception, returned metadata, a
prompt, a persisted row, a telemetry counter or a smoke-test report, and
positive controls assert the redaction is what makes that true.

## 11. Terminal states (§16)

`NO_EDGE`, `POSITIVE_EDGE_LOW_CONFIDENCE`, `POSITIVE_EDGE_HIGH_CONFIDENCE`,
`STALE`, `INSUFFICIENT_DATA`, `MODEL_DISAGREEMENT`, `MARKET_MOVED`,
`ANALYSIS_TIMEOUT`.

Evaluated in order from "we could not analyse" through "we analysed but
cannot conclude" to "we concluded". Reporting an edge from a cycle that
timed out would be the same class of error as reporting a flat portfolio
from an unreadable broker response.

`MARKET_MOVED` requires a **measured** move: prices are read at dispatch
(T0) and completion (T1), and an unmeasured move is never reported as a
move. This is the direct measurement of latency decay §17 asks for.

## 12. Before any Alpha signal may influence execution (§21)

Not yet met, and not close. The sample threshold must come from quantitative
validation rather than an arbitrary count, and the evidence must cover model
and ensemble calibration, net hypothetical PnL, PnL after fees/slippage, PnL
after AI cost, performance by category, latency decay, maximum drawdown,
signal concentration, sample independence and confidence intervals.

Two things must be true before those numbers mean anything at all:

1. **Real token prices are set.** Every net-of-cost figure is withheld until
   then.
2. **A real quantitative model is wired into `AtlasQuantProvider`.** The
   default returns `INSUFFICIENT_EVIDENCE` on purpose — a placeholder
   returning the market price would manufacture agreement with the market
   and make the ensemble look calibrated while measuring nothing.

Until both hold, the gateway measures the LLM ensemble against Kalshi
prices, which is a narrower question than the one at the top of this
document.

---

# Phase 2 — automatic shadow operation

**Still SHADOW ONLY. CAPITAL AUTHORITY: NONE. BROKER WRITE AUTHORITY: NONE.**

Phase 1 required an operator to hand the gateway a candidate file. Phase 2
closes the loop: the scanner's own evaluation emits candidates, a separate
service consumes them, and the calibration ledger fills itself.

## 13. The producer/consumer boundary

```
ENGINE PROCESS                          ALPHA SHADOW SERVICE PROCESS
  scanner → pipeline observer             poll research_spool/
    → research_feed.emit_candidate()        → mint atlas-alpha-v2 snapshot
      → DATA_DIR/research_spool/*.json      → dedup by market_snapshot_id
                                            → dispatch / meta / ledger
                                            → alpha_processed.jsonl
```

What crosses is plain JSON on a filesystem. The consumer cannot call back.

**The engine does not import the Alpha subsystem.** It imports
`research_feed`, a module whose entire import list is
`{hashlib, json, logging, os, time, config}` — pinned by a test. Building
the snapshot on the producer side would have required the engine to import
`alpha_snapshot`, and the point of the isolation is that the money path has
*no* dependency on the research path, not a small one. Provenance survives
anyway: each record carries `record_sha256` over its own content.

**Authority is a rule about directories.** The producer owns the spool: it
writes and prunes. The consumer owns its own state file and never writes
into the spool — asserted by comparing the spool's bytes before and after a
full consume. The cost is that the consumer cannot delete what it processed,
so the producer prunes by age and count instead. That is the right trade: a
consumer that can delete producer files is a consumer that can destroy
evidence the engine has not finished writing.

**The feed cannot hurt the engine.** It is off by default;
`emit_candidate` never raises; it never touches `PersistenceSentinel` (a
failed research write is not a critical persistence failure and must not
block an order the risk engine approved, nor unblock one); it writes only
under its own subdirectory; and the spool is bounded by count and age,
because an unbounded research spool on a shared volume is a slow way to take
the engine down with ENOSPC.

**Deduplication is by snapshot identity.** Two records describing the same
market at the same prices at the same second mint the same content-derived
id; one that differs in any bound field is a genuinely new observation and
is analysed. The processed set is append-only, so a restart does not
re-analyse — and re-pay for — work already done.

## 14. The separate service

`tools/alpha_service_run.py` (`health` / `once` / `run` / `telemetry` /
`resolve`). Deploy it as its own service with `XAI_API_KEY`,
`GOOGLE_GEMINI_API_KEY`, `OPENAI_API_KEY` and **no broker credential**.

`assert_no_broker_credentials()` refuses to start if any broker key or write
authority is visible in the environment. The separation is the entire
argument for a separate process, so it is checked rather than assumed. The
check reports variable NAMES, never values.

The service holds no broker client, no order manager and no risk manager. It
receives read-only market data as a *quote function* — a callable returning
a price dict — so it never holds anything that could place an order even by
mistake.

## 15. Pricing and budgets

Rates live in `alpha_pricing.json` (`atlas-alpha-pricing-v2`) with a
`version`, and each entry carries its own provenance and validity window
(§20). Every cost row records tokens, tool calls, search queries, latency,
the rates applied, and which pricing version produced the figure — so a past
cycle can be re-costed with `recost()` when prices change, instead of
today's prices being silently baked into yesterday's conclusions.

The **raw usage counts are retained alongside the derived figure**, which is
what makes that recosting possible: a cost row is evidence, not just a
number.

A rate this repository has not been given is `null`, which means UNKNOWN,
not free. Verifying the shipped rates against each vendor's current pricing
page remains an operator action; this repository cannot verify vendor
pricing itself.

**An unpriced model is not called** (`pricing_unconfigured`). This is not
pedantry: an unpriced call is costed at zero, so every cap below would be
unenforceable against it and "daily limit" would silently mean "unlimited".
`ALPHA_ALLOW_UNPRICED_CALLS` exists for deliberate experiments and defaults
to off.

Caps: per analysis, per provider per hour, per day. Exhaustion means **no
provider call** and its own terminal state, `BUDGET_EXHAUSTED` — because
"we could not afford to ask" is a different fact from "the models had no
opinion", and reporting the second would blame the models for a billing
limit. Spend is recomputed from an append-only ledger on every check, so a
restart cannot reset the daily cap. An unreadable ledger, or a budget gate
that raises, counts as exhausted: we cannot prove we are under budget, so we
are not.

The budget guard and the adapters are bound to **one** pricing table per
process, so the pre-call estimate and the post-call actual are never
computed from different rates.

## 16. Catalyst invalidation

`sweep_catalysts()` runs every cycle. A prediction whose catalyst has passed
gets an `INVALIDATION` row — a new row, never an edit — and leaves the
actionable series. `metrics()` reports `ensemble` and `ensemble_actionable`
side by side: a large gap between them is itself the finding about latency
and event risk, and collapsing them would hide it.

## 17. Market movement

Prices are captured at dispatch, at the **first** response, at the **last
valid** response, at completion, and at configured intervals afterwards
(`ALPHA_OBSERVATION_INTERVALS_S`, default 60/300/900 s). The first/last
split is what attributes decay to a slow model rather than to the cycle as a
whole. An unavailable book is a missing sample, never a price of zero.

## 18. Provider configuration

| Provider | Base URL | Model | Surface |
|---|---|---|---|
| xAI | `https://api.x.ai/v1` | `grok-4.6` | **Responses API** (`/responses`) |
| Gemini | `https://generativelanguage.googleapis.com/v1beta` | `gemini-3.7-flash` | `generateContent` |
| OpenAI | `https://api.openai.com/v1` | `gpt-5.6-luna` | **Responses API** (`/responses`) |

All configurable; none permanently bound. `health_check()` reports
configured / priced / reachable per provider at startup. A provider that
fails is EXCLUDED — never silently substituted with another model — and the
others carry on, because no single provider is mandatory.

**These defaults remain unverified by this repository.** Model ids and API
shapes change; check them against each vendor's current reference before
enabling the service. A wrong endpoint or model produces a provider failure,
which is an excluded signal, never a probability.

## 19. AtlasQuant stays isolated

Still unwired, still `INSUFFICIENT_EVIDENCE`, still measured separately in
`by_model`. Its health report distinguishes *wired* from *reachable*: an
unwired quant model is healthy and produces no opinion, which is the honest
state rather than a failure.

---

# Phase 3 — real provider activation

Everything above still holds. This phase makes the three adapters point at
the real vendor surfaces, prices them, and adds a way to prove a provider is
usable **before** a shadow session is allowed to start. It changes nothing
about the safety boundary: Alpha still has no execution path, no broker
client, no broker credential and no capital authority.

## 20. Rate cards carry provenance and a validity window

`alpha_pricing.json` is `atlas-alpha-pricing-v2`. Each entry is a
`PricingEntry` and carries:

| field | why it is there |
|---|---|
| `provider`, `model` | what the rate applies to — looked up by the model id the adapter actually reports, not by the provider name |
| `input_per_mtok`, `cached_input_per_mtok`, `output_per_mtok` | cached input is cheaper and is charged at its own rate when the vendor reports it |
| `tool_call_usd`, `search_query_usd` | per-call surcharges, when the vendor bills them |
| `currency` | a number without a unit is not a price |
| `effective_from`, `effective_until` | *when* this was the rate |
| `source`, and the file's `version` | *who said so*, so a wrong figure is traceable to its origin |

The shipped rates, all `source: "operator-supplied 2026-09-09"`:

| provider | model | input /Mtok | cached input /Mtok | output /Mtok | window |
|---|---|---|---|---|---|
| xAI | `grok-4.6` | $2.00 | $0.50 | $6.00 | open |
| Gemini | `gemini-3.7-flash` | $0.75 | — | $3.75 | **through 2026-12-31** |
| OpenAI | `gpt-5.6-luna` | $0.20 | — | $1.20 | open |

### An expired window is not a current rate

Gemini's card is explicitly bounded. Past `2026-12-31T23:59:59Z` that entry
stops applying, the model becomes **unpriced**, and an unpriced model is not
called (§15). The alternative — carrying yesterday's rate forward silently —
would mean the daily cap was being enforced against numbers nobody had
checked since last year, which is indistinguishable from not enforcing it.
A published future rate is a *different entry with its own window*, never an
edit to this one.

A window bound that cannot be parsed is treated as unenforceable, which is
not the same as absent: it refuses too.

## 21. Billed cost and estimated cost are both kept

xAI reports `usage.cost_in_usd_ticks` — an authoritative billed figure.
Where a vendor supplies one, `reconcile_billed_cost()` records **both** it
and our rate-card estimate; neither silently overwrites the other, and a
disagreement beyond `ALPHA_COST_RECONCILE_TOLERANCE` is flagged rather than
averaged away. `budgeted_cost()` charges the budget the **vendor's** figure
when one exists, because that is the number that will appear on the invoice.

The tick denomination is **not** something this repository can verify, so
`ALPHA_XAI_COST_TICKS_PER_USD` defaults to `0.0` (unconfigured) and the raw
tick count is stored verbatim in `billed_cost_raw` until an operator sets
the scale. `tools/alpha_smoke_test.py` prints the observed
`ticks / estimated_usd` ratio from a real call, which is the only honest way
to learn it. An unconvertible tick count never becomes a USD number by
assumption.

## 22. The smoke test (`tools/alpha_smoke_test.py`)

Exactly **one** bounded call per provider against a fixed fixture —
`ATLAS-SMOKE-TEST-0001`, a coin flip on a market that does not exist, so no
vendor answer can be mistaken for a forecast and no real contract is
described to a third party. It reports per provider: credential present,
pricing configured, budget allowed, called, reachable, response parsable,
schema valid, usage captured, latency captured, cost captured.

It is charged against **the same** budget ledger the service uses, so it
cannot be used to get around the daily cap.

There is **no fake success**. Provider error, model unavailable, pricing
unavailable, malformed response and timeout each produce their own verdict —
`NOT_EXECUTED`, `REFUSED_*`, `FAIL`, `REACHABLE_BUT_INVALID` — and none of
them is ever converted into a probability, a `0.5`, or a fallback to another
model.

## 23. Cost protection is enforced before dispatch

`BudgetGuard.check(provider, model, prompt_chars=…)` estimates the
**worst case** for the call it is about to authorise — prompt characters
converted to tokens with headroom, plus a full `ALPHA_MAX_OUTPUT_TOKENS` of
output — and refuses before dispatch if that exceeds what remains. Checking
an *average* after the fact is not a cap; a single expensive call would
already have been made.

Caps: **$0.25** per analysis, **$2.00** per provider per hour, **$20.00**
per day (`ALPHA_MAX_COST_PER_*`).

## 24. Telemetry is per provider

`Telemetry.snapshot()["by_provider"]` reports, for each of grok / gemini /
openai independently: calls, success, timeouts, invalid, stale,
budget-refused, unpriced-refused, input/output tokens, estimated cost,
billed cost, charged cost, average latency and success rate. An aggregate
would hide one vendor timing out while another answers, which is exactly the
fact this phase exists to surface. No counter holds a secret.

## 25. Activation is gated on the smoke test

An automatic real-provider shadow session starts only once all three
providers pass. A provider that has not been proven usable is EXCLUDED, not
assumed working. AtlasQuant remains unwired and
`INSUFFICIENT_EVIDENCE`, and carries **no** ensemble weight — an absent
model contributes nothing rather than contributing a neutral opinion.

---

# Phase 4 — two services, one repository

Alpha and the engine now deploy as SEPARATE services from the same
repository, with different start commands and different authority. Nothing
about the safety boundary changes; what changes is that the separation is
now a deployment fact rather than only a code fact.

## 26. Why two services at all

Co-locating them would mean one process environment holding both the AI keys
and `KALSHI_PRIVATE_KEY`. Every argument in §2 for Alpha having no execution
path is weakened if the credential that authorises execution is sitting in
the same environment — the isolation would rest on Alpha never growing a
broker client, rather than on Alpha being unable to use one.

| | ATLAS ENGINE | ATLAS ALPHA SHADOW |
|---|---|---|
| start | `python kalshi_alpha_bot.py --loop --live-read-only` | `python tools/alpha_service_run.py` |
| holds | `KALSHI_*`, execution/risk config | `XAI_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` |
| must not hold | the three AI keys | any broker credential or write gate |
| owns on disk | `orders_state`, `positions_state`, risk/equity ledgers, `research_spool/` | `alpha_processed.jsonl`, `alpha_calibration_ledger.jsonl`, `alpha_cost_ledger.jsonl`, `alpha_observations.jsonl`, `alpha_telemetry.json` |

Alpha writes none of the engine's state files and has no code path to one.
Its own state is small and append-only; a Railway volume mounted at
`/data` with `DATA_DIR=/data` is sufficient, and losing it costs calibration
history, never money.

## 27. The startup refusal

`assert_no_broker_credentials()` refuses three kinds of variable:

* **credentials** — `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY`, and the demo and
  prod variants;
* **boolean gates** — `ALLOW_ORDER_SUBMISSION`, `LIVE_TRADING`,
  `LIVE_TRADING_CONFIRMED`, `LIVE_BROKER_WRITES_AUTHORIZED`,
  `KALSHI_ENV_CONFIRM`, `DEMO_TRADING`, `MODEL_APPROVED_FOR_LIVE`,
  `ALLOW_FALLBACK_CAPITAL`;
* **variables whose AUTHORITY IS IN THE VALUE** — `PROD_ACCESS_MODE=CAPITAL`
  and `EXECUTION_MODE=live`. These are the ones a truthiness test misses:
  `CAPITAL` is not `"1"` or `"true"`, and it is precisely the setting that
  turns capital on. A guard that only tested truthiness would have passed
  the one environment it most needed to refuse.

`DEMO_TRADING` is refused deliberately: a demo write is still a broker
write, and demo credentials are real credentials.

The process exits **78** and prints `ALPHA_STARTUP_REFUSED_BROKER_CREDENTIALS`
followed by the offending variable NAMES. No value is ever read — the check
tests presence and, for the valued cases, membership in a fixed list — so
there is nothing available to print even by accident.

## 28. The research feed across a service boundary

**This is the part that does not survive being assumed.** A Railway volume
is mounted into exactly ONE service. The engine writes `research_spool/`
onto its own volume; the Alpha service cannot see that directory at all.
A two-service deployment that kept the local transport would not crash —
it would report zero candidates forever, which is indistinguishable from a
quiet market. Silence is the worst available failure mode here.

So the spool crosses the boundary over the engine's **existing read-only
research API**, as a new `candidates` dataset:

```
ENGINE                                    ALPHA SHADOW
  scanner (read-only observer)
    └─> research_feed.emit_candidate       ALPHA_FEED_TRANSPORT=http
          └─> research_spool/ (volume)       └─> GET /api/research/v1/candidates
                └─> research_export.candidates      (bearer, cursor-paged)
                      served by dashboard_web              └─> HttpSpoolSource
                                                                 └─> mint snapshot
                                                                       └─> providers
```

Why this and not a queue or a database:

* **No new infrastructure.** The route, the bearer auth
  (`RESEARCH_API_TOKEN`, constant-time, refuses when unconfigured), the
  cursor paging and its expiry semantics already exist and are already
  tested. Adding Redis or Postgres for this would add an operational
  dependency to a subsystem whose entire purpose is to be optional.
* **The dependency points the right way.** The engine publishes and never
  learns whether anyone read it. Alpha down, stopped, or never deployed is
  invisible to the engine. The reverse is not symmetric and must not be.
* **The consumer cannot write.** With a shared filesystem, "the consumer
  never writes into the spool" was a property of which paths we chose to
  open. Over HTTP the transport offers no write method at all, so it is
  structural.
* **Identity is unchanged.** `market_snapshot_id` is derived from content,
  and the record id served is the producer's own `record_sha256`. A pulled
  record mints exactly the snapshot a local read would, so the two
  deployments deduplicate identically.

`ALPHA_FEED_TRANSPORT` defaults to `local`, so every single-host deployment
behaves exactly as before. Setting it to `http` requires
`ALPHA_RESEARCH_FEED_URL` (the engine's Railway private endpoint) and
`ALPHA_RESEARCH_API_TOKEN` (matching the engine's `RESEARCH_API_TOKEN`).

**An unreachable feed raises, and is never an empty page.** `FeedUnavailable`
is counted, logged and surfaced on the consumer; "the engine published
nothing" and "we could not ask" are different facts, and reporting the
second as the first is exactly how a broken deployment looks healthy. The
paging is capped at `MAX_FEED_PAGES` so a cursor that never advances stops
loudly instead of spinning.

The feed token is redacted out of transport exceptions the same way provider
keys are, and `describe()` reports `token_configured: true` rather than the
token.

## 29. Health

`python tools/alpha_service_run.py health` reports `service_mode`,
`grok_configured`, `gemini_configured`, `openai_configured`, `pricing_valid`,
`budget_available`, `broker_credentials_present`, `capital_authority`,
`execution_imports`, `quant_connected` and the feed transport, alongside the
detailed per-provider block. No field carries a secret.

`execution_imports` is **measured, not declared**: it counts the money-path
modules actually present in `sys.modules`. A field that always printed zero
would report the very property it exists to check, and would be worthless at
the only moment it mattered.
