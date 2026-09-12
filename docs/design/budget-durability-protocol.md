# Budget and durability protocol (V4-RA-05, V4-RA-06, V4-RA-07)

SHADOW ONLY. Nothing in this protocol grants execution, broker or CAPITAL
authority. It decides whether a *research* provider call may be made and how
its cost is accounted for.

The three findings are one protocol, and patching them separately produces
contradictory semantics: RA-05 decides when a write may be called durable,
RA-06 decides what a spend record means before and after that write, and
RA-07 decides whether a record that is on disk may be believed at all. This
file is the single statement they all refer to.

## The rule the three share

> UNKNOWN never becomes ZERO, and readable never means durable.

Two corollaries, one per direction of failure:

* **Nothing may be acknowledged as durable because it can be read.** Bytes in
  the page cache read back perfectly; a directory entry that was never
  fsynced reads back perfectly. Neither is evidence of durability.
* **Nothing may be accounted as zero because its record is missing or
  unreadable.** A spend whose row failed to write did not become free, and a
  row that cannot be validated does not reduce the total — it destroys our
  right to state one.

## 1. Durability acknowledgement lifecycle (RA-05)

Per PATHNAME, not per directory: a directory fsync persists the entries that
exist when it runs, so a new name created later is a new obligation.

```
                        ┌──────────────────────────────────────┐
                        │  UNPROVEN                            │
   process start ─────▶ │  (the initial state, and the state   │
   any restart   ─────▶ │   every pathname returns to when a   │
                        │   process dies)                      │
                        └───────────────┬──────────────────────┘
                                        │ append_line(path, line)
                                        │   1. write_all bytes
                                        │   2. fsync(file fd)      bytes durable
                                        │   3. fsync_directory(parent)
                                        │
                        ┌───────────────┴───────────────┐
                        │                               │
                  barrier FAILS                   barrier SUCCEEDS
                        │                               │
                        ▼                               ▼
          raise DurabilityUnknown              ┌─────────────────┐
          pathname stays UNPROVEN              │  PROVEN         │
          (proof is also DISCARDED,            │  (this process  │
           so a stale proof cannot             │   confirmed it) │
           survive a later failure)            └────────┬────────┘
                                                        │
                                          append to an already-PROVEN
                                          pathname creates no new
                                          directory entry, so it owes
                                          no new barrier: bytes +
                                          fsync(file fd) only.
```

`os.path.exists(path)` appears **nowhere** in this decision except to force a
barrier (a pathname that does not exist yet certainly owes one). It is never
used to conclude that a barrier already happened. That inference is the
defect: after a barrier failure the file is on disk, so the next append saw
`created == False` and skipped the barrier entirely, acknowledging success
while the name had never been persisted.

The proof registry is **process-local and optimistic only in the safe
direction**. Losing it (restart, crash) returns pathnames to UNPROVEN and
causes a *redundant* barrier, never a skipped one. This is the opposite of
RA-06's defect, where the lost memory was the thing that *blocked* — which is
why one is safe as memory and the other is not:

| memory state | what losing it causes | safe? |
|---|---|---|
| "this barrier already succeeded" (RA-05) | an extra barrier | yes |
| "a spend is unaccounted for" (RA-06) | admission restored | **no** |

**Append-only is preserved.** A barrier failure leaves the row on disk and
raises; the retry appends again. `append_line` never truncates, never
rewrites, and never removes a historical byte. Duplicate rows produced by a
retry are resolved by IDENTITY at the accounting layer (§2), not by editing
the file.

## 2. Budget intent lifecycle (RA-06)

Every provider call is announced on disk **before** the irreversible act, and
resolved on disk after it. One append-only file, four row kinds, one identity
(`intent_id`) tying them together.

```
   BudgetGuard.check(provider, model)
         │
         │ pricing gate, uncertainty gate, caps  ── refused ──▶ no row written
         │                                                      nothing spent
         ▼ allowed
   ┌──────────────────────────────────────────────┐
   │ append RESERVATION                            │
   │   kind=reservation, intent_id, reserved_usd,  │
   │   owner_instance, ts                          │
   └───────────────┬───────────────────────────────┘
                   │
        ┌──────────┴───────────┐
        │                      │
   append FAILS           append DURABLE (RA-05 confirmed the barrier)
        │                      │
        ▼                      ▼
  check() returns        ┌───────────────┐
  allowed=False          │  OPEN         │ ◀── counts toward hourly/daily
  NOTHING IS             │  (announced,  │     spend at reserved_usd
  DISPATCHED             │   in flight)  │
  no money at risk       └───────┬───────┘
                                 │
        ┌────────────────┬───────┴────────┬─────────────────┐
        │                │                │                 │
   provider answered  not dispatched   process dies     row write fails
        │                │                │                 │
        ▼                ▼                ▼                 ▼
   append ACTUAL     append VOID    owner_instance    reservation stays
   api_cost_usd      reason         no longer ours    OPEN on disk
        │                │                │                 │
        ▼                ▼                ▼                 ▼
   ┌─────────┐      ┌─────────┐    ┌──────────────────────────────┐
   │RESOLVED │      │RESOLVED │    │  ORPHANED                    │
   │counts at│      │counts at│    │  counts at reserved_usd AND  │
   │  actual │      │   0     │    │  makes accounting UNCERTAIN  │
   └─────────┘      └─────────┘    │  → every check() REFUSES     │
                                   └───────────┬──────────────────┘
                                               │ operator / reconcile()
                                               ▼
                                        ┌──────────────┐
                                        │  RECONCILED  │
                                        │ counts at the│
                                        │ final figure │
                                        └──────────────┘
```

### Why `owner_instance` and not a pid

A reservation records the random token this *process instance* generated at
import. A reservation is ORPHANED when it is unresolved and its
`owner_instance` is not ours. That is deterministic: a restart always yields
a new token, so the RA-06 witness (spend incurred, actual row unwritable,
process restarts) is orphaned immediately rather than after a timeout.

A pid would not do: pids are reused, and `owner_is_alive()` answers
"unknown → alive", which for a *partial file* means "keep it" (safe) but for
a *reservation* would mean "still in flight, do not block" (unsafe). The
conservative answer has the opposite sign for the two objects, so the
mechanism cannot be shared.

Under the writer model this subsystem documents (one Alpha service, one
volume — see `durable_append`'s AA-14 note), a second concurrent writer's
open reservations would read as orphaned and block. That is fail-closed and
is stated here rather than discovered.

### State transitions under each event

| event | reservation | accounting | admission |
|---|---|---|---|
| prepare (check allowed) | about to be written | unchanged | pending |
| persist OK | OPEN | spend += reserved | allowed, dispatch proceeds |
| persist FAILS | never written | unchanged | **refused**, nothing dispatched |
| dispatch | OPEN | spend += reserved | — |
| response | OPEN | — | — |
| actual accounting OK | RESOLVED | spend += actual | normal |
| actual accounting FAILS | stays OPEN | spend += reserved | refused (memory latch) |
| fsync failure | reservation not durable → treated as never written | unchanged | refused |
| process death | stays OPEN on disk | — | — |
| restart | ORPHANED (foreign owner) | spend += reserved | **refused: uncertain** |
| retry | idempotent by `intent_id`; a duplicate ACTUAL for a resolved intent is a lifecycle violation, not a second charge | — | — |
| reconciliation | RECONCILED | spend += final | restored |

**No transition turns UNKNOWN into ZERO.** The only exit from ORPHANED is an
explicit RECONCILED row, which is itself append-only and validated.

## 3. Ledger validity (RA-07)

Accounting is computed only over rows that pass a strict versioned schema.
A row that cannot be validated does not get skipped and does not get deleted:
it makes the total UNKNOWN, and `BudgetGuard.check` converts UNKNOWN into a
refusal.

```
   read line ──▶ JSON parse ──fail──▶ UNKNOWN (refuse)
                     │
                     ▼
              strict schema ──fail──▶ UNKNOWN (refuse)   row PRESERVED
                     │
                     ▼
              lifecycle check ──fail──▶ UNKNOWN (refuse) row PRESERVED
                     │
                     ▼
                resolve by intent_id ──▶ total
```

Validated per row: object shape; schema version; `kind` in the known set;
`ts` present, a real finite number, positive and not absurdly far in the
future; `intent_id` present and well-formed on every v1 row; the cost field
required by the kind present, numeric (bool is not a number), finite, and
within policy bounds.

**Refund policy, stated so "negative" is not a loophole.** A negative amount
is a CORRECTION and is accepted only on a `reconciled` row, which is the row
kind an operator writes deliberately. A negative `reserved_usd` or a negative
`api_cost_usd` on an `actual` row is malformed — otherwise the cheapest way
to defeat a cap would be to append a large negative cost.

**Legacy rows.** Rows written before this schema carry no `schema` key. They
are historical `actual` charges and are validated under the legacy rules
(object, valid `ts`, present finite non-negative `api_cost_usd`). They are
never rewritten — RA-05's append-only guarantee and this one are the same
guarantee.

## 4. Where the three meet

* RA-05 is what makes RA-06's reservation meaningful: a reservation whose
  directory entry was never persisted is not an announcement, and `check()`
  refuses rather than dispatching against it.
* RA-07 is what makes RA-06's *recovery* meaningful: reading the reservation
  back after a restart only tells us something if the rows can be believed.
* RA-06 is what makes RA-05's duplicate-on-retry acceptable: retries are
  resolved by `intent_id`, so an append-only file with a repeated row still
  yields one charge.
