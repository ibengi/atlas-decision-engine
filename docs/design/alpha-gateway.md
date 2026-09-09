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

`ALPHA_PRICE_IN_PER_MTOK` / `_OUT_PER_MTOK` default to **0.0** and every row
carries `cost_priced: false`. A plausible-looking per-token price would
silently decide the one question this subsystem exists to answer, so
`metrics()` **withholds** the net-of-inference-cost figure and says why
until an operator sets the real rates.

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

`XAI_API_KEY`, `GOOGLE_GEMINI_API_KEY`, `OPENAI_API_KEY` are read at call
time and sent in one header. Gemini's key goes in `x-goog-api-key`, not a
query string, because a key in a URL ends up in every access log on the
path. Error bodies are truncated and redacted before they can reach a log
line — vendors echo request headers into error payloads often enough that
printing one verbatim is a credible way to leak a key. Tests assert no
secret appears in a log, an exception, returned metadata, a prompt or a
persisted row.

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
