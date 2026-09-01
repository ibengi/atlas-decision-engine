# Bounded LIVE canary — design only

**Status: NOT EXECUTED.** This document defines the envelope for a first
LIVE order. It selects no market, places no order, and enabling it requires
a separate operator authorization. Nothing in this file changes engine
behaviour; every control it relies on already exists and is tested.

## 1. Envelope

| Bound | Value | Enforced by |
|---|---|---|
| Contracts per order | **1** | `MAX_CONTRACTS_PER_ORDER=1`; `contract_cap_config()` refuses a missing/invalid/zero/negative/over-max value, and `place_and_track` blocks `count > cap` rather than clamping |
| New LIVE exposure | **1 contract**, ≤ 0.85 $ | cap of 1 × `MAX_ENTRY_CENTS=85` |
| Markets | **one** | operator selects a single ticker; `ONE_TRADE_PER_MARKET=true` |
| Orders | **one**, then stop | `MAX_TRADES_CYCLE`, plus the manual stop in §4 |
| Open positions | 1 (cap 3) | `MAX_OPEN_POSITIONS` |
| Retry after ambiguity | **none** | see §3 |

## 2. Preconditions (all must hold at the moment of arming)

1. `ALLOW_ORDER_SUBMISSION=false` until the instant of the canary, flipped
   for one run and returned to `false` immediately after.
2. `MAX_CONTRACTS_PER_ORDER=1` — **explicitly confirmed**, not assumed.
3. `REQUIRE_PERSISTENT_STATE=true`, `DATA_DIR=/data/state5`, volume mounted,
   `state_epoch.json` present and continuous.
4. `numReplicas=1` (SINGLE_REPLICA_REQUIRED — see `ledger-corrections.md`).
5. Persistence sentinel healthy; startup reconciliation **MATCH**.
6. LIVE credentials installed **and** the three LIVE confirmations set
   (`KALSHI_ENV_CONFIRM=LIVE`, `LIVE_TRADING_CONFIRMED=YES`,
   `LIVE_TRADING=1`) — the engine refuses production otherwise — plus the
   model gatekeeper (`check_live_allowed`) passing.
7. A fresh pre-canary state capture (five files + SHA-256s) taken and held
   off-box, as at each earlier migration gate.

## 3. Submission semantics — the ambiguity rule

The 2026-08-28 incident is the design driver: an order whose cancel failed
kept filling after the engine stopped watching it.

- **HTTP 201 is authoritative.** A 201 means the order exists at the
  broker, whatever any later read says.
- **A failed read-back NEVER causes a repost.** `[ORDER_VERIFY] relecture
  en erreur` is a logging event, not a retry trigger; the order is treated
  as live and reconciled, never re-sent.
- **Deterministic `client_order_id`** derived from
  (ticker, side, count, price) via `_client_order_id`, so even a duplicate
  POST is idempotent broker-side.
- **The submission guard is persisted around the broker interaction** and
  survives restart (`submission_guard.json`, TTL 21600 s): after a restart
  the same ticker cannot be re-submitted, which
  `tools/restart_harness.py` verifies on every run.
- **Ambiguous outcome ⇒ stop, never retry.** Any of: non-201 with an
  unclear body, a timeout after send, a cancel failure, or a read-back the
  engine cannot interpret, ends the canary and hands the decision to the
  operator with the raw broker payload.

## 4. Automatic stop conditions

The canary ends after **one order lifecycle** (fill or resolution), and
immediately on any of:

| Condition | Existing mechanism |
|---|---|
| persistence failure | `PersistenceSentinel` → `blocked:persistence_failure` |
| broker uncertainty | `reconcile_halt` UNKNOWN / BROKER_UNAVAILABLE |
| reconciliation mismatch | `reconcile_halt` MISMATCH |
| duplicate ambiguity | `blocked:duplicate_submission_guard` |
| unexpected position quantity | periodic `verify_against_broker` (900 s) |
| unexpected fill count | order verify vs. journal; any divergence halts |
| contract cap violation | `blocked:contract_cap_exceeded` / `_invalid` |
| unhandled broker response | fail-closed default: stop and report |

Operator stop: `KILL_SWITCH=true` blocks every cycle before the balance
call, and `ALLOW_ORDER_SUBMISSION=false` blocks at the submission path.
Either is sufficient; both are one variable change away.

## 5. Evidence to collect before a second order

No second LIVE order until the operator reviews:

1. `[ORDER_SUBMIT_ATTEMPT]` / `[ORDER_SUBMIT_RESPONSE]` with HTTP status,
   `client_order_id`, broker `order_id`.
2. Broker order record (`GET /portfolio/orders/<id>`): status, fill count,
   remaining count, maker/taker split, fees.
3. The journal row, position row and submission-guard entry written for it.
4. A reconciliation pass showing broker and local agreeing on quantity.
5. Settlement: broker result, payout, and the engine's PnL for the same
   contract, reconciled to zero delta.
6. A restart, proving the whole lifecycle survives it.

## 6. What this design deliberately does not do

No automatic sizing above one contract, no second market, no retry logic,
no automatic re-arm after a stop, and no change to any fail-closed gate.
The canary is an observation of one contract's full lifecycle under real
money, not a trading session.
