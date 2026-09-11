# Alpha writer model (Astra AA-14)

SHADOW ONLY. Nothing described here has execution, broker or CAPITAL
authority.

## The question AA-14 asked

`record_prediction` and `resolve` were check-then-append: read the ledger to
decide whether the row already exists, then append if it does not. That
sequence is not atomic. Two Alpha writers could both read "not recorded" and
both append, producing two predictions for one snapshot — and every
calibration number computed afterwards would double-count it.

Astra offered two acceptable answers: **(A)** enforce a single Alpha writer
structurally, or **(B)** serialize check+append across processes. This
deployment implements **B**, with **A** as the operational assumption it is
allowed to rely on.

## What is implemented

`durable_append.exclusive_lock` takes an exclusive `fcntl.flock` on a
**sidecar** lock file (`<ledger>.lock`) and holds it across the entire
critical section:

| Operation | What the lock covers |
|---|---|
| `AlphaLedger.prepare` | find existing PREPARE → append PREPARE |
| `AlphaLedger.record_prediction` | uniqueness check (by `prediction_id` **and** by stable `analysis_id`) → append PREDICTION |
| `AlphaLedger.resolve` | prediction lookup → resolution-exists check → append RESOLUTION |

The lock is a sidecar rather than the ledger itself so that acquiring it never
opens the ledger for writing, and a crash while holding it cannot leave the
ledger half-open. The OS releases `flock` when the holding process dies, so a
crashed writer does not wedge the ledger.

Processed-state transitions (`ProcessedStore.mark`) are append-only and
last-row-wins by design; they carry no read-modify-write, so they need no
lock. A generation change written by another writer is therefore visible on the
next `_load()`, and `AlphaShadowService.reconcile_processed()` is what compares
the processed file against the committed predictions (AA-13).

Conflicting resolutions are **surfaced, never silently first-win**:
`ingest_settlements` reports a `conflicts` entry naming both outcomes and
appends nothing.

## The limits, stated rather than assumed

* **`flock` is advisory.** It binds only processes that ask for it. Every
  writer in this subsystem goes through `AlphaLedger`, and no other module
  opens these files for writing — but a hand-run script that appended directly
  would not be serialized.
* **`flock` is per-host.** Two *machines* sharing one ledger over NFS or a
  similar network filesystem are **not** made safe by this mechanism. Do not
  deploy a second Alpha service against the same volume from another host.
* **Non-POSIX platforms degrade to a no-op.** `durable_append` imports `fcntl`
  defensively; where it is unavailable the lock yields without locking and the
  single-writer assumption becomes load-bearing rather than enforced.

## The writer model this deployment assumes

**Exactly one Alpha service process writes the Alpha ledgers, on one host,
against one volume.** The lock exists so that concurrency *within* that host —
a restart overlapping its predecessor, a CLI run alongside the service, two
threads — cannot corrupt the ledger. It is defence in depth behind the
single-writer deployment, not a licence to run several.

If a second writer is ever wanted, the choice is not "add more locking": it is
to give each writer its own ledger file and merge on read, because
`analysis_id` uniqueness is the property the calibration numbers depend on and
it cannot be maintained across hosts by an advisory per-host lock.
