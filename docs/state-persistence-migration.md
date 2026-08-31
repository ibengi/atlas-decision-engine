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

### Gate A — two-observation confirmation (never migrate on one reading)

A single observation can catch a transient: a cycle mid-settlement, a slow
broker call, one page of a paginated feed. The migration is irreversible
against the ephemeral disk, so the window must be *confirmed*, not merely
*seen*.

Require **two consecutive production observations, ≥ 60 s apart**, each
independently proving all four:

- `open_count == 0`
- `guard_reason != max_open_positions`
- no unresolved broker positions
- engine evidence trustworthy (`trustworthy=true`, `observer_status` GREEN or
  AMBER, `engine_sources=reachable`)

**If the second observation disagrees with the first in any field: abort and
return to monitoring.** A disagreement is not noise to be averaged — it is
the window proving it was never open. Do not take a third reading as a
tie-breaker; restart the pair.

Both observations must come from a tick that actually evaluated. A tick that
concluded nothing (degraded observer, failed fetch) is not an observation of
"clear" — it is an absence of observation, and absence is never evidence.

### Gate B — PRE_MIGRATION evidence record

Before the first restart, capture and **commit** a record to
`docs/pre-migration-evidence.json`. Committing is the point: the disk it
describes is about to be destroyed, so the record has to live somewhere the
migration cannot reach.

Required fields, each carrying its own provenance:

| Field | Source |
|---|---|
| `timestamp` | capture time, UTC |
| `deployed_commit` | Railway deployment metadata |
| `open_count` | engine log `open=N` / absence of the guard line |
| `guard_state`, `guard_reason` | CTO `[EVIDENCE_FETCH]` |
| `broker_position_summary` | engine reconciliation log (`matched` / `rebuilt` / `ghost`) |
| `risk_state_summary` | engine `[CAPITAL]` and `pnl_jour` / `frais_cumules` lines |
| `submission_guard_state` | `[SUBMISSION_GUARD_LOADED] active_tickers=N` — **emitted at startup only** |
| `trade_journal_count` | research `capabilities` settled-trade count, via evidence capture |

**Mark every field that cannot be read as `UNAVAILABLE`, never as zero or an
estimate.** Several of these live only on the ephemeral disk and are exposed
solely through log lines; `submission_guard_state` in particular is printed
once at boot, so its pre-migration value is whatever the *last* startup
reported, or UNAVAILABLE if that line has aged out of retention. A record
that quietly writes `0` for an unread field is worse than no record: it
becomes a false baseline that the post-restart comparison will "confirm".

### Runbook

1. **Confirm the window** — Gate A above (two observations, ≥ 60 s apart).
2. **Capture and commit the PRE_MIGRATION record** — Gate B above.
3. **Merge** `fix/orphan-position-release` into `main` (commits `3ed6dd1`,
   `3b31c08`, `e9c8d32`, `ebcbe03`). The service auto-deploys from `main`.
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
8. **Compare against the PRE_MIGRATION record** — every field that was
   MEASURED before must still be measured after, or its change must be
   explained. Fields recorded UNAVAILABLE prove nothing in either direction
   and must not be counted as passing.

**Abort conditions.** Stop and return to monitoring if any of these hold:
Gate A's two observations disagree; a position opens between the two
observations; `/data` is not writable (step 6); or `[STATE_EMPTY]` appears
after the second restart. Aborting costs nothing — the window reopens on the
next settlement. Proceeding past a failed gate cannot be undone, because the
disk holding the truth is gone by then.

A template for the record lives at `docs/pre-migration-evidence.template.json`;
copy it to `docs/pre-migration-evidence.json`, fill it, and commit it before
step 4.

## Accepted one-time loss

The pre-migration journal cannot be carried across — it exists only on the
current container's disk, and this environment has no path to read it (the
Kalshi and Railway service domains are blocked by egress policy, and there is
no exec access). Doing the migration at `open_count == 0` reduces the loss to
its minimum: no position metadata is at stake, and settled history up to that
point is already mirrored in evidence-capture's lake. What is genuinely lost
is the cumulative drawdown and consecutive-loss history — which is precisely
what `[STATE_EMPTY]` now announces instead of hiding.
