# PROD_ACCESS_MODE — READ_ONLY observation vs CAPITAL trading

## The architectural defect this closes

`check_live_allowed()` treated **access to production data** as equivalent to
**authorization to commit production capital**. Reaching LIVE at all required
passing the model gatekeeper.

Observing a market requires no proof of edge. Committing money does. Conflating
them meant the only way to *look* at production was to approve the model — the
worst possible reason to approve a model, and precisely the pressure a
promotion gate exists to resist.

## The two modes

| | `READ_ONLY` | `CAPITAL` |
|---|---|---|
| Market/orderbook/balance/position reads | yes | yes |
| Reconciliation **reads** | yes | yes |
| Full decision engine (model, edge, risk, sizing) | yes | yes |
| `WOULD_SUBMIT` telemetry | yes | n/a |
| Model gatekeeper required to start | **no** | **yes** |
| `LIVE_TRADING` / `LIVE_TRADING_CONFIRMED` required | no | yes |
| `KALSHI_ENV_CONFIRM=LIVE` required | yes | yes |
| Any broker mutation | **impossible** | subject to every existing gate |

## Invalid values: choice A, refuse startup

`PROD_ACCESS_MODE` absent, blank, malformed or unknown **refuses production
startup**. Chosen over defaulting to READ_ONLY because it is the easier claim
to prove: *a process that does not start cannot mutate an account*, and the
proof is a single control point. Defaulting to READ_ONLY is also safe, but its
proof obligation is that read-only dominance holds on **every** path — a far
larger surface.

Both are implemented anyway. Startup refuses (A), **and** `prod_is_read_only()`
is written as "was CAPITAL requested?" rather than "is this READ_ONLY?", so any
path reaching the client without passing startup still gets read-only (B).

```
PROD_ACCESS_MODE = READ_ONLY | CAPITAL
INVALID_MODE_RESULT = REFUSE_PROD_STARTUP  (and read-only at the client)
```

## READ_ONLY dominance

Checked **first** in `KalshiClient._assert_broker_write_allowed`, above
`LIVE_BROKER_WRITES_AUTHORIZED`. In READ_ONLY no combination of
`ALLOW_ORDER_SUBMISSION`, `LIVE_TRADING`, `LIVE_TRADING_CONFIRMED`,
`MODEL_APPROVED_FOR_LIVE`, `LIVE_BROKER_WRITES_AUTHORIZED` or kill-switch state
can produce a mutation.

## Shadow does not "try then fail"

The engine reaches a complete decision — model probability, market probability,
edge, EV, risk gates, sizing — and records `WOULD_SUBMIT` **without calling
`place_and_track`**. The client guard is defence in depth, not the normal
shadow path: a shadow that attempts a write and relies on being refused leaves
a real attempt one bug away from the network, and fills the rejection counters
with refusals that are not decisions.

```
SHADOW_INVOCATES_WRITE_LAYER = NO
```

## Reconciliation observes, it does not repair

Under READ_ONLY, startup recovery logs the discrepancy and continues rather
than cancelling. A "read-only" mode that cancels orders at startup is not
read-only. The TTL cancel in `place_and_track` is guarded too — unreachable in
practice, since no order can have been created, but a guard that depends on
"cannot happen" is not a guard.

## The guarantee does NOT come from the entrypoint

`kalshi_edge_measure.py` constructs `KalshiClient("prod")` directly and never
passes through the startup mode validation. It is safe — it hands only
`get_market` (a GET) to `resolve_pending` and calls no write method — but it
demonstrates the important point:

> **The startup check is a convenience. The prohibition that actually holds
> lives at the client boundary.** If it were ever moved up to the entrypoint,
> any script constructing its own production client would bypass it.

A test pins the set of modules that build production clients, so a new one is a
deliberate decision, and asserts none of the secondary ones reaches a write.

## Mutations

M1 read-only skips the client guard · M2 read-only reaches `create_order` ·
M3 cancel permitted in read-only · M4 model approval upgrades read-only to
capital · M5 `--shadow` selects capital · M6 invalid mode selects capital ·
M7 reconciliation repairs under read-only · M8 gatekeeper blocks observation ·
M9 capital skips the model gate — **all 9 killed**.

M5 survived the first pass: nothing asserted that `--shadow` must not choose an
access mode. Two tests were added.

## What this does not establish

Not LIVE-ready. No production credential exists, LIVE reconciliation has never
run, the canary has never run, `MODEL_APPROVED` is false and no out-of-sample
edge has been demonstrated.

`LIVE_READ_ONLY_READY=NO` · `CAPITAL_LIVE_READY=NO`
