# Broker-authoritative ledger corrections

The trade journal (`kalshi_trades.json`) is append-only. When the broker's
final record of an order differs from what the engine observed live, the
divergence is repaired by **appending a corrective event**, never by
rewriting history. This document states the invariants an operator must
hold when activating one.

## The two event types

| `event_type`        | meaning                                                        |
|---------------------|----------------------------------------------------------------|
| `trade` (or absent) | an independent trade — the unit every count is expressed in     |
| `ledger_correction` | a **delta** applied to one existing trade (`corrects_trade_id`) |

A correction is **not a trade**. `trade_logger.fold_corrections()` — the
single function through which every counting and economic surface reads the
journal (`RiskManager`, `StatsEngine`, the dashboard, `performance.py`, the
research export) — folds a correction's deltas into the trade it corrects
and never yields it as a row of its own.

Consequences, by design:

* **unchanged**: trade count, settled count, win/loss counts, win rate,
  `trades_today`, `consecutive_losses`, settlement recency (the half-open
  cooldown anchor), strategy row counts
* **corrected**: realized/daily PnL, gross PnL, fees, filled quantity, the
  equity curve and therefore rolling drawdown

The corrective row carries `won: None` (it has no independent outcome) and
`settled_at` = the corrected trade's settlement time, so PnL is attributed
to the economic day of the settlement rather than the day the correction
was applied. `applied_at` records the latter.

## Activation is explicit

Deploying code corrects nothing. A correction runs only when the operator
names it:

```
LEDGER_CORRECTION_APPLY_IDS=corr-01a04823-sixth-fill-v1
```

Once applied, the corrective row in the journal is the permanent marker;
the variable can be removed and the correction will not run again. Each
correction also declares strict preconditions on its target row (trade id,
order id, ticker, side, filled quantity, settlement result) and writes
nothing at all if any of them fails — a wrong ledger, an already-corrected
ledger or a not-yet-settled target is a no-op, not a guess.

## SINGLE_REPLICA_REQUIRED

The journal is a JSON file written with atomic replace but **no
cross-process lock**. Two engine replicas writing the same journal can lose
each other's rows — a hazard shared by every engine write, not just
corrections. Mutual exclusion is therefore enforced outside the process:

* the Railway service runs with **exactly one replica**
  (`deploy.multiRegionConfig.<region>.numReplicas = 1`, single region);
  verify with `get-service-config` before activating a correction.

The engine additionally *verifies* what it cannot lock: it re-reads the
journal from disk before appending and again after flushing, and trips the
persistence sentinel — which blocks every order submission — if the marker
count is anything other than exactly one. A replica breach is thus loud and
fail-closed, never a silent double correction.

## Applied corrections

| id | incident | effect |
|----|----------|--------|
| `corr-01a04823-sixth-fill-v1` | 2026-08-28, order `01a04823` on `KXBTCD-26AUG2808-T79599.99`: the cancel of the resting remainder failed (HTTP 404) at 11:32:11Z and the engine stopped watching; the sixth contract filled as a maker fill at 11:34:21Z. Root cause `FILL_ARRIVED_AFTER_CANCEL_FAILURE`. | quantity 5 → 6, gross +0.81 $, fees 0.06 → 0.0539 $, net +0.8161 $ |
