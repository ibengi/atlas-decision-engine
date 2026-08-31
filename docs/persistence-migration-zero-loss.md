# ATLAS_PERSISTENCE_MIGRATION_ZERO_STATE_LOSS — requirements

Blocker 1 (no volume, `DATA_DIR` unset on the production engine service)
is deliberately **not** solved by Phase 1. This document fixes the exact
requirements the eventual migration must meet. It complements
`state-persistence-migration.md` (Gate A / Gate B runbook); where the two
differ, this document is stricter and wins.

## The bar

A migration is ZERO_STATE_LOSS only if every one of these survives it
**without reset**:

| Artifact | Carried by |
|---|---|
| `submission_guard.json` | file copy (in-TTL entries must survive) |
| `orders_state.json` | file copy |
| `kalshi_trades.json` (trade journal) | file copy — sole source of the four derived controls below |
| `daily_realized_pnl` | derived from journal → journal copy is the requirement |
| `rolling_drawdown` | derived from journal |
| `consecutive_losses` | derived from journal |
| `trades_today` | derived from journal |
| settlement recency (`seconds_since_last_settlement`) | derived from journal |
| `positions_state.json` + `risk_state.json` (half-open claim) | file copy |
| reconciliation state | re-derived at boot against the broker (allowed: broker is authoritative) |

**UNAVAILABLE is not permission to reset.** If an artifact cannot be
exported from the old container, the migration is not zero-loss; it may
still be executed as a *minimal-loss* migration, but only at
`open_count == 0` (Gate A) and only with the loss named explicitly in the
PRE_MIGRATION record — never silently.

## Hard requirements

1. **Export before destruction.** Attaching a Railway volume redeploys the
   service and destroys the current ephemeral disk. The state files above
   must therefore be exported from the *running* container **before** the
   volume is attached. Acceptable mechanisms (pick one, read-only):
   - a read-only, token-authenticated state-export endpoint on the
     existing research API (serves the exact bytes of the files above), or
   - Railway exec/SSH copy, if available to the operator.
   The export must be checksummed (sha256 per file) at capture time.
2. **Import before first trading cycle.** The exported files are placed
   into `/data` (the new volume) before the engine's first post-migration
   boot completes its OrderManager construction, so the dedup guard and
   journal are live from the very first cycle.
3. **Continuity enforcement on.** The migrated deployment sets
   `DATA_DIR=/data` and `REQUIRE_PERSISTENT_STATE=true`. `ALLOW_FRESH_STATE`
   is set to `true` for exactly one boot **only** in the deliberate
   minimal-loss variant (empty `/data` acknowledged); it is never left set.
   From then on, a wiped or unmounted volume halts trading fail-closed
   (`blocked:persistence_failure`, `create_order_calls=0`) instead of
   silently resetting every risk counter — proven by
   `tools/restart_harness.py` (16/16).
4. **Write-through verification.** After mounting: confirm no
   `JsonStore.save` ERROR appears, `[SUBMISSION_GUARD_LOADED]
   active_tickers=N` matches the exported guard, and `state_epoch.json`
   exists on `/data`.
5. **Second controlled restart** must show: no `[STATE_EMPTY]`, identical
   journal row count, identical `daily_realized_pnl` / `rolling_drawdown` /
   `consecutive_losses` / `trades_today` (compare derived values, not just
   files), no duplicate order (re-submission of an in-TTL ticker returns
   `blocked:duplicate_submission_guard`), no resurrected position.
6. **Gate A/Gate B unchanged.** Two consecutive observations ≥ 60 s apart
   proving `open_count == 0` etc., and a committed PRE_MIGRATION record,
   exactly as in `state-persistence-migration.md`. With export (req. 1)
   in place the journal loss disappears; Gate A still minimizes exposure
   in case the export proves impossible.
7. **No runtime state or secrets in Git.** The PRE_MIGRATION record
   contains counts, hashes and log-derived fields only — never the state
   files themselves, never credentials. `.gitignore` already excludes the
   runtime state files; that exclusion must not be weakened.

## Explicitly out of scope for Phase 1

- Attaching the volume, changing `DATA_DIR`, or setting the two new env
  flags on production. Both flags default to off; the deployed engine's
  behaviour is unchanged until an operator flips them at migration time.
