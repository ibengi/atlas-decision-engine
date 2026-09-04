# LIVE broker write authorization

## The invariant

> **A broker write in PRODUCTION requires explicit client-boundary
> authorization. LIVE observation in read-only mode requires none.**

This is enforced by construction, not by configuration. It is not a claim that
the system is LIVE-ready — it is not. It is the control that must exist
*before* a production credential does.

## Why a new gate rather than reusing an existing one

Every control that existed before this was a policy flag which some legitimate
operation might open for its own reasons:

| Flag | Legitimately true when… |
|---|---|
| `ALLOW_ORDER_SUBMISSION` | a DEMO canary is running |
| `LIVE_TRADING` / `LIVE_TRADING_CONFIRMED` / `KALSHI_ENV_CONFIRM` | the process is merely *reaching* production at all |
| `MODEL_APPROVED_FOR_LIVE` | the model has been promoted |
| `KILL_SWITCH` (false) | normal operation |

None of them means "a real-money mutation is authorized." Reusing any of them
would have made LIVE read-only depend on a flag whose purpose is something
else — so a legitimate change elsewhere could silently arm real writes.

`LIVE_BROKER_WRITES_AUTHORIZED` means exactly one thing and is consulted by
exactly one guard.

## Parsing

Strict, fail-closed, via `config._env_gate`:

| Value | Result |
|---|---|
| absent | `false` |
| `""`, `" "`, `"\t"` | `false` |
| `maybe`, `ture`, `2`, `-1`, `TRUE!` | `false` (logged loudly) |
| `false`, `0`, `no`, `n`, `off`, `non` | `false` |
| `true`, `1`, `yes`, `y`, `on` | `true` |

Only an explicit, recognised true word arms it. Nothing the parser fails to
understand ever authorizes a real-money write.

## Where it is enforced

Two independent layers, both required and both separately tested:

1. **Per method** — `create_order` and `cancel_order` each call
   `KalshiClient._assert_broker_write_allowed()` before doing anything else.
2. **At the transport** — `KalshiClient._req()` calls the same helper for any
   verb in `MUTATING_HTTP_METHODS` (`POST`, `PUT`, `PATCH`, `DELETE`), before
   the key check and before any request is issued.

The transport layer is what makes the inventory durable: **a write method added
in future is covered on the day it is written**, even if its author forgets the
guard. The per-method layer is what keeps the guarantee if `_req` is ever
refactored.

> The verb set is deliberately broader than what the client uses today (only
> `POST` and `DELETE`). Listing only the verbs currently in use would mean a
> future `PUT` was unguarded until somebody remembered to update the list.

**Cancellation is also a write.** It reduces exposure, so the *kill switch*
deliberately does not block it — but on an unauthorized production account it
still mutates a real account, and is refused. "Read-only" does not mean "except
when it suits us."

## What is NOT affected

- **DEMO is unchanged.** The guard returns immediately when `env == "demo"`.
- **LIVE reads are unchanged.** `get_markets`, `get_market`, `get_balance`,
  `get_order`, `get_fills`, `list_orders`, `get_positions` and reconciliation
  reads all use `GET` and are untouched. A test asserts each still reaches the
  transport while writes are forbidden, and that no read is built on a mutating
  verb.
- **No existing guard was removed or weakened.** `ALLOW_ORDER_SUBMISSION`, the
  model gate, the daily quarantine, the kill switch, risk controls and the
  duplicate guard all remain. This is an additional, independent layer.

## Broker-write inventory

| # | Path | Verb | Guarded by |
|---|---|---|---|
| 1 | `KalshiClient.create_order` | `POST /portfolio/events/orders` | method guard + transport guard |
| 2 | `KalshiClient.cancel_order` | `DELETE /portfolio/events/orders/{id}` | method guard + transport guard |

Call sites: `order_manager.py` ×3 (behind the full gate ladder),
`kalshi_demo_execution_check.py` ×2 (DEMO-only probe; refuses any LIVE context
itself, and is covered by the client-layer guards like any other caller).

No `replace_order`, `amend_order`, batch operation or close-position helper
exists. `alert_notifier.py` issues a `requests.post`, but to a configured
alerting webhook — not a Kalshi endpoint, and not a broker write.

## Mutation results

| # | Mutation | Outcome |
|---|---|---|
| M1 | remove the LIVE client write guard | **killed** (12 failures) |
| M2 | malformed value treated as true | **killed** (2 failures) |
| M3 | remove `create_order`'s own guard | **killed** (1 failure) |
| M4 | remove `cancel_order`'s own guard | **killed** (1 failure) |
| M5 | let `ALLOW_ORDER_SUBMISSION` bypass the guard | **killed** (13 failures) |

**M3 and M4 initially SURVIVED.** Every test still passed with a per-method
guard deleted, because the transport backstop caught the write anyway — two
layers existed, but only one was pinned, so the method-level guard could have
been removed silently. Three tests were added that neutralise one layer and
assert the other still refuses, in both directions. This is recorded because a
mutation that survives a first pass is the most useful result the matrix
produces.

## What this does not establish

This control does not make the system LIVE-ready. It is a precondition, not a
readiness signal:

- no production credential exists, and none may be created by the agent
- LIVE reconciliation has never been executed
- the LIVE canary has never been executed
- `MODEL_APPROVED` is false and no out-of-sample edge has been demonstrated
- the daily settlement oracle is not validated

`LIVE_READ_ONLY_READY=NO`, `CAPITAL_LIVE_READY=NO`.
