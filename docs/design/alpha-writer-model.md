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
| `AlphaLedger.record_prediction` | uniqueness check (by `prediction_id` **and** by stable `analysis_id`) → append PREDICTION → append its COMMIT receipt |
| `AlphaLedger.resolve` | prediction lookup → resolution-exists check → append RESOLUTION |
| `AlphaLedger.invalidate` | already-invalidated check → append INVALIDATION |
| `AlphaLedger.record_observation` | already-sampled check for this interval → append OBSERVATION |
| `AlphaLedger.record_costs` | the whole cost batch, so two batches cannot interleave |
| `ProcessedStore.mark` | torn-tail check → append processed row |
| `BoundedSpool.write` | prune → scan → capacity decision → reserve → write → fsync → rename |

The lock is a sidecar rather than the ledger itself so that acquiring it never
opens the ledger for writing, and a crash while holding it cannot leave the
ledger half-open. The OS releases `flock` when the holding process dies, so a
crashed writer does not wedge the ledger.

## What the re-audit corrected

The previous revision of this document said processed-state transitions
"carry no read-modify-write, so they need no lock". That was wrong, and the
re-audit named it.

`durable_append.append_line` reads the file's **last byte** to decide whether
a torn tail needs separating. That read is part of the append, so *every*
append to a shared file is a check-then-act, including the ones that look
like pure appends. Two unsynchronized writers can both observe an intact
tail, or both observe a torn one and both separate it.

The same reasoning applied to three ledger methods that were left outside the
lock — `invalidate`, `record_observation` and `record_costs` — each of which
is visibly a check-then-append. Being append-only does not make a writer
safe; it makes the damage permanent.

**Every appender to a shared file is now serialized**, via
`durable_append.serialized_append` or the ledger's own `lock()`. The table
above is the complete list, and
`test_astra_v3_remediation.AA14_UnserializedAppendersAndStaleCaches` pins it
statically so a new appender cannot quietly skip the lock.

### Caches

`ProcessedStore._cache` was filled once and never invalidated, so one
writer's marks stayed invisible to another for the life of the process. The
cache is now keyed on a **generation** — `(size, mtime_ns, inode)` — and
rebuilt whenever the file has moved underneath it. A false miss costs one
re-read; a false hit cost a duplicated analysis.

`AlphaShadowService.reconcile_processed()` still compares the processed file
against the committed predictions and reports disagreements without repairing
them (AA-13).

### Commit receipts

A prediction is durably committed only when a `COMMIT` row follows it. The
receipt is written after the prediction's own append has returned, and that
append returns only after its `fsync` succeeded — so a readable prediction row
whose write failed mid-sequence is never mistaken for a commit. See AA-13 in
the v3 remediation report.

### Lock ordering

No lock is nested inside another. `sweep_catalysts` calls `invalidate` once
per prediction, each acquiring and releasing; the spool's reservation lock is
a different file and is taken only on the research writer thread. `flock` is
not re-entrant across file descriptors in one process, so this is a rule to
keep rather than a property to rely on.

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
