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
| `AlphaLedger.prepare` | find existing PREPARE → append PREPARE → append its `PREPARE_COMMIT` receipt |
| `AlphaLedger.record_prediction` | uniqueness check (by `prediction_id` **and** by stable `analysis_id`) → append PREDICTION → append its COMMIT receipt |
| `AlphaLedger.resolve` | prediction lookup → resolution-exists check → append RESOLUTION |
| `AlphaLedger.invalidate` | already-invalidated check → append INVALIDATION |
| `AlphaLedger.record_observation` | already-sampled check for this interval → append OBSERVATION |
| `AlphaLedger.record_costs` | the whole cost batch, so two batches cannot interleave |
| `ProcessedStore.mark` | torn-tail check → append processed row → **invalidate the cache** |
| `BudgetLedger.record` | torn-tail check → append budget row |
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

**RA-09 corrected *when*.** The generation was re-stamped after the append
lock was released:

```python
with serialized_append(self.path) as append:
    append(line)                      # lock released here
if self._cache is not None:
    self._cache[snapshot_id] = row
    self._generation = self._current_generation()
```

Any row another writer appended between those two moments was *inside* the
generation this store stamped and *outside* the cache it stamped it for. The
cache then looked fresh — size, mtime and inode all matching — while missing
a row that was on disk, and stayed that way until something else changed the
file. A missing processed row reads as "this snapshot was never analysed", so
the service pays for an analysis another writer has already committed: the
AA-13 double-spend, reached through the cache instead of through a crash.

The cache is now **invalidated inside the lock**, before the generation can
move. While the lock is held no other writer can append, so the invalidation
cannot be stamped past somebody else's row.

`AlphaShadowService.reconcile_processed()` still compares the processed file
against the committed predictions and reports disagreements without repairing
them (AA-13).

### Commit receipts

A prediction is durably committed only when a `COMMIT` row follows it. The
receipt is written after the prediction's own append has returned, and that
append returns only after its `fsync` succeeded — so a readable prediction row
whose write failed mid-sequence is never mistaken for a commit. See AA-13 in
the v3 remediation report.

**RA-08 extended the same rule one step earlier.** `prepare()` was idempotent
by *lookup*: find a PREPARE row, return it. That row is read from the file,
which is exactly what AA-13's re-audit established is not proof of
durability. An append whose `write` landed and whose `fsync` failed leaves
bytes that read back perfectly; `prepare()` raised on that attempt, so the
service deferred and spent nothing — but on the next poll the lookup returned
those same bytes, the dispatch precondition was declared satisfied, and every
provider was paid against a ledger that was still not writable.

PREPARE now carries its own `PREPARE_COMMIT` receipt. A retry that finds a
receipt-less PREPARE **finishes** its durability rather than trusting it (the
receipt's own append fsyncs the whole file, so the row before it becomes
durable at the same moment), and `alpha_service` gates dispatch on
`prepare_is_durable`, re-read from the ledger.

### Durability is never assumed (RA-05)

`durable_append.append_line` promises that a caller is never told "written"
without durability having been attempted **and confirmed**. Three paths inside
it used to break that promise by swallowing the uncertainty rather than
reporting it: `tail_is_torn` returned `False` on any `OSError`, and both the
directory `open` and the directory `fsync` failures were discarded. All three
now raise `DurabilityUnknown` (an `OSError` subclass, so every existing
caller's error handling still applies), and `BoundedSpool` routes its own
directory fsync through the same function and reports such a write as
**failed**.

An unreadable tail is not an intact tail, and bytes reachable under no name
are not a durable append.

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
  writer in this subsystem goes through `AlphaLedger`, `ProcessedStore`,
  `BudgetLedger` or `BoundedSpool` — all four of which take the lock since
  RA-06 — but a hand-run script that appended directly would not be
  serialized.
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
