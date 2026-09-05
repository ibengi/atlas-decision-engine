# Cancellation under the production access modes — operator procedure

Status: **procedure only.** This document changes no code and no policy. It
writes down a consequence of RC-2 and RC-3 that the independent RC-2 review
(MEDIUM-3) asked to be made explicit *before* an incident rather than
discovered during one.

---

## 1. The rule, in one line

**Cancelling an order is a mutation of a real account, and mutations follow
the same authorization rules as submissions — including when cancelling is the
thing you want.**

`cancel_order` calls `_assert_broker_write_allowed` exactly as `create_order`
does (`kalshi_client.py`). "Read-only" that made an exception for cancellation
would not be read-only; it would be read-only-unless-convenient, which is not a
property anyone can verify.

## 2. What each state actually does

| Environment | `PROD_ACCESS_MODE` | `LIVE_BROKER_WRITES_AUTHORIZED` | `cancel_order` |
|---|---|---|---|
| `demo` | n/a | n/a | **Allowed.** The guard exempts demo entirely. |
| `prod` | unset / blank / misspelled | anything | **Refused** (`BrokerWriteForbidden`). An unreadable mode is read-only. |
| `prod` | `READ_ONLY` | anything, including true | **Refused.** Read-only dominates; no trading flag lifts it. |
| `prod` | `CAPITAL` | false / unset / unreadable | **Refused.** |
| `prod` | `CAPITAL` | true | **Allowed.** |

Note the row that surprises people: `READ_ONLY` **plus** an armed write
authorization still refuses. Since the pre-merge fix pack, that combination
also refuses to *start* — see §6.

The kill switch is deliberately different from the access mode. `KILL_SWITCH`
blocks `create_order` and does **not** block `cancel_order`, because cancelling
reduces exposure and a breaker that trapped open orders would be a hazard
rather than a protection. The access mode is not a breaker: it says which
account you are allowed to change at all.

## 3. The consequence to know about

Both recovery call sites — `order_manager.reconcile_startup` and the TTL cancel
inside `place_and_track` — sit inside `except KalshiAPIError`, and
`BrokerWriteForbidden` subclasses `KalshiAPIError`. A refused cancellation is
therefore **caught and handled fail-closed**: the order stays in
`orders_state.json` and is never recorded as cancelled. Nothing is lost and
nothing is falsified.

But the steady state matters:

> If authorization is granted, orders are placed, and authorization is then
> revoked, the engine can **observe** a resting order indefinitely and will
> **never** cancel it, on any cycle, until authorization is restored.

**Revoking write authorization strands open orders. Flatten first.**

This is the correct trade — the alternative is an account that can be mutated
by a process nobody authorized — but it is a trade, and it must be an
instruction rather than a discovery.

## 4. Emergency procedure — stop new exposure

Use this when the engine must stop opening positions. It does not require
touching the access mode, and none of it can cancel anything.

1. **Close submissions.** Set `ALLOW_ORDER_SUBMISSION=false`. Strictly
   fail-closed: absent, blank or unreadable all mean blocked. Restart the
   service so the value is read.
2. **Confirm.** The logs must show `[ORDER_SUBMIT_ATTEMPT] bloque:
   ALLOW_ORDER_SUBMISSION=false` for any candidate that reaches the money path,
   and `orders_submitted: 0` in each `[CYCLE-SUMMARY]`.
3. **If a faster stop is needed**, set `KILL_SWITCH=true`. It fails closed on
   an unreadable value (`on_invalid=True`), and it leaves cancellation
   available so exposure can still be reduced.
4. Neither step cancels anything. Both only stop *new* orders.

## 5. Inspecting open orders — always available

Reads are never gated by the access mode. In any mode, including `READ_ONLY`
and including with no write authorization at all:

- `orders_state.json` in `DATA_DIR` is the local record of unresolved orders.
- `GET /portfolio/orders` and `GET /portfolio/positions` are reads and are
  permitted.
- `[RECONCILE_VERIFY]` lines report broker-vs-local agreement each cycle, and
  `[RECOVERY_READ_ONLY]` names any unresolved order recovery declined to touch.

Establish what is actually open **before** changing any mode. Reconciliation in
read-only mode is precisely the tool for this: it observes and compares, and it
does not repair.

## 6. Cancelling on purpose — the only supported route

Cancellation requires capital mode **and** an explicit write authorization.
Both, deliberately, and in this order:

1. **Decide and record** why cancellation is necessary, and which order ids.
2. **Flatten intent first.** Set `ALLOW_ORDER_SUBMISSION=false` so the engine
   cannot open new positions while you are reducing.
3. **Set `LIVE_BROKER_WRITES_AUTHORIZED=true`.**
4. **Start in capital mode** — `--live-capital`, or `PROD_ACCESS_MODE=CAPITAL`
   — with the confirmations that mode already requires
   (`KALSHI_ENV_CONFIRM=LIVE`, `LIVE_TRADING_CONFIRMED=YES`, `LIVE_TRADING=1`,
   and the model gatekeeper). Selecting capital mode grants none of these; it
   only makes them the applicable checks.
5. **Cancel**, then **reconcile**: `[CANCEL_V2_CONFIRMED]` with a `reduced_by`
   proof is the only evidence a cancellation happened. A cancellation without
   that proof is not a cancellation; the code refuses to treat it as one.
6. **Verify** `[RECONCILE_VERIFY] MATCH` and that the order has left
   `orders_state.json`.
7. **Stand down deliberately.** Remove `LIVE_BROKER_WRITES_AUTHORIZED`
   **before** returning to `READ_ONLY`. Since the pre-merge fix pack a
   read-only start with the authorization still armed **refuses to start** and
   does not rewrite the variable, so a half-completed stand-down is loud rather
   than inherited by the next capital start.

## 7. What is deliberately not automated

- No path clears `LIVE_BROKER_WRITES_AUTHORIZED` on the operator's behalf.
  Silently clearing a contradictory configuration hides the mistake that
  produced it.
- No path cancels an order "because it looked stuck". An unresolved order in
  read-only mode is logged and left alone for a person to decide on.
- Reconciliation never repairs in read-only mode, on any cycle, however long
  the discrepancy persists.

## 8. Scope

Describes behaviour at the pre-merge fix pack on top of `e377df6`. Nothing here
authorizes a deployment, a credential, or a live order; the engine remains
DEMO-only with `ALLOW_ORDER_SUBMISSION=false`, `MODEL_APPROVED=false` and the
daily oracle unapproved.
