# Engine state persistence — audit and migration runbook

## The problem in one line

The `ibengi/atlas-decision-engine` Railway service has **no volume**, and
`DATA_DIR` defaults to `"."` (`config.py`). Every state file the engine owns
therefore lives on the container filesystem and is destroyed by any restart or
redeploy.

The two sibling services in this project already do the opposite:
`atlas-evidence-capture` and `atlas-engine-shadow-burnin` both mount a volume
and set `DATA_DIR`. The engine is the outlier.

## What is actually at stake

`RiskManager` derives **all** of its historical limits from the trade journal
rather than from a stored counter:

| Control | Derived from | After journal loss |
|---|---|---|
| daily loss stop | `daily_realized_pnl()` → settled trades today | resets to 0 |
| drawdown breaker | `rolling_drawdown()` → whole settled curve | resets to 0 |
| consecutive-loss breaker | `consecutive_losses()` → tail of settled trades | resets to 0 |
| max trades/day | `trades_today()` → journal timestamps | resets to 0 |
| half-open cooldown | `seconds_since_last_settlement()` | becomes unknown |

So a restart does not merely lose bookkeeping: it **silently disarms every
historical risk control at once**, and the engine comes back looking healthy.
That is the finding that makes this migration worth doing carefully.

`submission_guard.json` is the other sharp edge. Its own docstring says it
exists so that "un redemarrage Railway ne permette pas de resoumettre le meme
ticker pendant le TTL" — it is the fix for the 2026-07-25 incident where one
signal was re-submitted and filled ~8 times. On an ephemeral disk that
guarantee is void: the guard it persists dies with the container it protects
against.

## Classification

| Artifact | Class | Notes |
|---|---|---|
| `positions_state.json` | RECONSTRUCTABLE_WITH_INFORMATION_LOSS | Broker gives existence/side/quantity. Entry price, fees, strategy, `opened_at`, `decision_id` are **not** recoverable — now labelled `*_estimated` rather than passed off as measured. |
| `kalshi_trades.json` (trade journal) | **MUST_BE_PERSISTED** | Sole source of all risk history (table above). Settled rows are mirrored off-box into evidence-capture's lake via `/api/research/v1/settlements`, so settled history is partially reconstructable; **open** trade rows are exported by nothing. |
| `submission_guard.json` | **MUST_BE_PERSISTED** | Duplicate-submission lock. Unreconstructable by definition — it is memory of an action, not a state the broker reports. |
| `orders_state.json` | **MUST_BE_PERSISTED** | In-flight orders resolved at startup by `reconcile_startup`. If lost, an order in flight at restart is forgotten; its fill returns later as a broker position with a fabricated entry price. |
| `risk_state.json` | **MUST_BE_PERSISTED** | Holds the half-open claim (`half_open_anchor` / `half_open_claimed`). Losing it hands back an extra post-loss-streak attempt. |
| `seen_fill_ids.json` | DISPOSABLE | Written and loaded, but **never read for any decision** — no membership test exists anywhere in the codebase. Dead state today; would become MUST_BE_PERSISTED the moment a consumer is added. |
| `reconciliation_report.json` | DISPOSABLE | Diagnostic snapshot of the last reconciliation. |
| `capital_curve.json`, `reports/` | RECONSTRUCTABLE_WITH_INFORMATION_LOSS | Recomputable from the journal; worthless if the journal is gone. |
| `cycle_report.json`, `dashboard_state.json`, `pipeline_stats.json`, `reject_reasons.json` | DISPOSABLE | Observability, rewritten every cycle. |
| Broker positions / orders / fills | AUTHORITATIVE_BROKER_RECONSTRUCTABLE | Always the source of truth for what exists. |

## The solution (no code change, no redesign)

Attach a Railway volume at `/data` and set `DATA_DIR=/data`.

One variable covers both sides: the engine writes through `config._p()`, and
the research API server is started in-process with `CFG.DATA_DIR`
(`kalshi_alpha_bot._start_dashboard_if_enabled` → `start_dashboard`), so
writer and reader move together atomically. `JsonStore` already provides
atomic writes, sha256 checksums, backup rotation and corruption recovery —
the durability layer is fine; it was simply pointed at a disk that evaporates.

Postgres was considered and rejected: it would mean rewriting every
`JsonStore` call site for no gain the volume does not already give, and the
mission calls for the smallest safe change.

## Migration procedure

**Precondition — do not skip.** Attaching a volume triggers a redeploy, which
destroys the current ephemeral state at that instant. Migrating while
positions are open converts them into broker-rebuilt rows with estimated
entry prices, permanently corrupting their settlement PnL.

> **Run only when `open_count == 0`** — i.e. after the three current positions
> settle and the guard leaves `max_open_positions`.

1. **Confirm the window.** Engine logs show no `MAX_OPEN_POSITIONS` line and
   the CTO reports `guard_state != blocking`.
2. **Record the before-state** from the CTO control plane
   (`/api/v1/autonomy/control-plane`): guard state, open count, deployed
   commit.
3. **Merge** `fix/orphan-position-release` into `main` (commits `3ed6dd1`,
   `3b31c08`, `e9c8d32`). The service auto-deploys from `main`.
4. **Create and attach the volume** at mount path `/data`.
5. **Set `DATA_DIR=/data`** on the service.
6. **Verify writability first.** `JsonStore.save` fails soft — it logs
   `JsonStore.save(...)` at ERROR and returns `False`. If the container runs
   non-root against a root-owned mount, state silently stops persisting and
   everything otherwise looks normal. Confirm no such error appears, and that
   `[SUBMISSION_GUARD_LOADED]` appears on the following boot.
7. **Prove persistence** with a second, deliberate restart: `[STATE_EMPTY]`
   must **not** appear, and `Recovery: N position(s)` must reflect real state
   read back from `/data`.

## Accepted one-time loss

The pre-migration journal cannot be carried across — it exists only on the
current container's disk, and this environment has no path to read it (the
Kalshi and Railway service domains are blocked by egress policy, and there is
no exec access). Doing the migration at `open_count == 0` reduces the loss to
its minimum: no position metadata is at stake, and settled history up to that
point is already mirrored in evidence-capture's lake. What is genuinely lost
is the cumulative drawdown and consecutive-loss history — which is precisely
what `[STATE_EMPTY]` now announces instead of hiding.
