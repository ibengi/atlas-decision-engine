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
   model gatekeeper (`check_live_allowed`) passing. `KALSHI_KEY_ID` and
   `KALSHI_PRIVATE_KEY` must be a real, loadable RSA pair:
   `prod_credentials_config()` stops a prod boot before any client,
   manager, reconciliation, scan or broker call when either is absent,
   blank, non-PEM, unparseable or not RSA — previously such a boot came
   up and called the broker unsigned.
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
- **Ambiguous outcome ⇒ resolve by asking, never retry.** On any of:
  non-201 with an unclear body, a timeout after send, or a dropped
  connection, the engine looks the order up by its deterministic
  `client_order_id` via `GET /portfolio/orders` (listed server-side by
  ticker, matched locally — Kalshi has no server-side filter on that
  field). Exactly one match ⇒ its `order_id` and status are adopted, no
  new order. No match ⇒ stay locked, no automatic repost. More than one
  match, an unusable listing, or a lookup that fails ⇒ fail-closed: a
  failed lookup is never read as "the order does not exist", and a
  multiple match halts submissions on every ticker until the operator
  decides. The intent (ticker + `client_order_id` + size + price) is
  persisted in `pending_intents.json` before the POST, so the same
  question is re-asked after a restart.

## 3b. Closing an ambiguous intent — the NOT_FOUND policy

`NOT_FOUND` is the answer that looks like permission to try again, and it
is exactly the answer a lagging read replica gives about an order that
does exist. It is therefore the only outcome that is not decisive on its
own. Policy: **`NOT_FOUND_CONFIRMED_2x_60s_FULL_PAGINATION`**.

| Question | Answer |
|---|---|
| Readings before declaring absence | **2** (`AMBIGUOUS_NOT_FOUND_CONFIRMATIONS`) |
| Minimum spacing between them | **60 s** (`AMBIGUOUS_NOT_FOUND_INTERVAL_S`); a closer reading carries no new information and is not counted |
| Full pagination required | **Yes** — a listing truncated at `max_pages` with a cursor still open raises, because that absence is fabricated by pagination |
| Late-appearing order | Adopted at the next reading (submission path, periodic reconciliation, or restart); never a second order |
| Restart mid-window | The count and timestamps live in `pending_intents.json`, so the sequence continues rather than restarting |
| While unresolved | The open intent blocks submission on that ticker **independently of the guard TTL** |
| TTL expiry alone | Never authorises a repost — the TTL measures elapsed time, not proof |
| Anything inconsistent | Fail-closed: `MULTIPLE` and `MALFORMED` halt submissions on every ticker; a failed lookup is `UNAVAILABLE` and does not count as evidence |

Once closed (`CLOSED_ABSENT`), the intent is removed and the ticker
returns to the ordinary duplicate-guard rules — closure is a decision,
not a permanent trap. An intent that can never be resolved (broker
unreadable indefinitely) stays open by design and needs an operator
decision; that is the intended failure mode.

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
