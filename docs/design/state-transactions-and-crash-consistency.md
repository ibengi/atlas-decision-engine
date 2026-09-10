# Authoritative economic state: transactions, crashes and concurrency

Remediation of Astra findings A01–A20 on `3af848e`.

This document states the model the engine now follows for every change to
authoritative economic state, the crash-consistency matrix each operation
satisfies, and — explicitly — what is *not* closed.

The question every rule below answers is one question:

> Can Atlas ever believe economic state is safer, newer, flatter, or more
> complete than the evidence actually proves?

---

## 1. Why one model

Astra's second pass found nine distinct operations each implementing its
own partial transaction. The generation was validated in one statement and
committed in another (A13). A prepared state was installed in the live
object and rolled back afterwards (A14, A10). A refused operator action had
already mutated memory (A15). `os.write` was called once and believed
(A18). A directory fsync was allowed to fail silently (A18). A reader kept
deciding from an authority another process had superseded (A19).

These are not six bugs. They are one missing primitive, six times.

`state_tx.py` now holds it, and nothing else implements these rules:

| Mechanism | Closes | Rule |
|---|---|---|
| `WriterFence` | A13 | The generation check and the write are one step, under an OS-level exclusive lock held across read → validate → write → publish. Re-entrant within a process; a fence that cannot be acquired refuses the write rather than proceeding unfenced. |
| `durable_write_all` | A18 | Every intended byte, or an exception. Short writes and `EINTR` loop; a zero-byte return is an error, not a spin. |
| `fsync_dir` | A18 | Directory fsync failures propagate. Only a genuine "not supported" (`EINVAL`/`ENOTSUP`) is tolerated, because that is a capability statement, not an I/O failure. |
| `check_finite` / `all_finite` | A16 | Explicit `math.isfinite`, never a comparison: `NaN > x` and `NaN < x` are both `False`, so every threshold silently admits it. `bool` and `str` are rejected, not coerced. |
| `StaleAuthority` | A19 | The exception a reader raises *before* a safety-critical decision when the durable generation has moved past its own. |

Two more rules live at the persistence boundary because that is where
non-finite values physically enter and leave state:

* `strict_dumps` — `allow_nan=False`. A `NaN` is refused at write time
  instead of being emitted as the non-JSON literal `NaN`.
* `strict_loads` — refuses non-finite literals *and* duplicate JSON keys.
  A duplicate key is a document that states two values for one field;
  last-wins silently keeps the more convenient one (also A08).

## 2. The transaction

Every change to authoritative economic state follows:

```
    acquire writer fence            (excludes other processes)
      read authoritative generation
      PREPARE an isolated copy       (self.state is NOT touched)
      VALIDATE inputs, finite numbers, generation, evidence
      re-check BOUND SOURCE VERSIONS at the linearization point
      DURABLE COMMIT                 (write-all, fsync, replace, fsync dir)
    release writer fence
    PUBLISH                          (single atomic rebind of self.state)
```

`EquityLedger._commit(prepared, bind=...)` is the implementation.
`EquityLedger._shadow(prepared)` is what makes PREPARE possible without
publication: the helper methods that must run against the new state
(watermark advance, continuity evidence) are given a view over the prepared
copy, so no reader in the process can observe it.

**Publication is a single attribute rebind.** Under the GIL that is atomic:
a concurrent reader sees either the whole old state or the whole new one,
never a half-applied mixture.

**A refused transaction publishes nothing.** Not the HWM, not the hold, not
the token set, not the status, not capital eligibility. `_mark_durable` /
`_rollback_to_durable` extend the same guarantee to the non-transactional
`save()` path, so *every* persistence refusal leaves memory equal to disk.

### Source-version binding (A03, A07)

Validating a decision and writing it are two moments. `_source_versions()`
captures what the decision rested on:

* `settled_count` and `journal_digest` — the journal
* `continuity_seq` / `continuity_hash` — the evidence chain
* `ledger_generation` and `durable_generation` — the ledger
* `open_positions` — exposure

`_commit(..., bind=...)` re-checks them immediately before the durable
write. A settlement that lands after authorization does not get absorbed
into an already-approved migration, and a rebase does not commit against
exposure that has moved.

## 3. Crash-consistency matrix

`LEDGER` = `equity_ledger.json`, `CHAIN` = `equity_continuity.log`
(append-only), `JOURNAL` = `kalshi_trades.json`,
`INTENTS` = `pending_intents.json`.

Every row's **publish point** is *after* its durable commit point. Every
row's **crash before commit** outcome is "previous valid state", and every
**crash during commit** outcome is "previous valid state" — because the
temp file is only promoted by `os.replace` after being fully written and
fsynced, and `os.replace` is atomic.

| Operation | Read set | Write set | Fence | Generation | Prepare | Durable commit point | Publish point | Crash before | Crash during | Crash after | Restart result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **SETTLEMENT** | JOURNAL | JOURNAL | JOURNAL fence | n/a (append-structured) | row mutated in list | `JsonStore.save(JOURNAL)` returns | list already live | trade stays open | trade stays open | settled | journal re-read; ledger recomputes from it |
| **OBSERVE CASH** | LEDGER, JOURNAL, positions | LEDGER, CHAIN | LEDGER fence | `expect_generation` | in-memory then `save()` | `JsonStore.save(LEDGER)` returns | on success; on failure memory is rolled back to disk | no flow recorded | no flow recorded | flow recorded | adverse floor re-read; unexplained adverse residual still blocks |
| **REBASE** | LEDGER, JOURNAL, CHAIN, orders, positions | CHAIN (token), LEDGER | LEDGER fence | `expect_generation` + `bind` | deep copy | `JsonStore.save(LEDGER)` returns | `self.state = prepared` | old HWM, no hold, token **burned** | old HWM, no hold, token burned | new HWM + hold | token unusable; operator issues a new action id |
| **ATTESTATION** | LEDGER, CHAIN | CHAIN (token), LEDGER | LEDGER fence | `expect_generation` | deep copy | `JsonStore.save(LEDGER)` returns | `self.state = prepared` | previous status | previous status | RECONCILED | status unchanged unless the write completed |
| **HOLD RELEASE** | LEDGER, CHAIN | CHAIN (token), LEDGER | LEDGER fence | `expect_generation` | deep copy | `JsonStore.save(LEDGER)` returns | `self.state = prepared` | hold **stays** | hold stays | hold released | CAPITAL stays ineligible unless the write completed |
| **MIGRATION (seed)** | LEDGER, JOURNAL, cash | LEDGER, CHAIN | LEDGER fence | `expect_generation` + `bind` | deep copy | `JsonStore.save(LEDGER)` returns | `self.state = prepared` | UNSEEDED | UNSEEDED | seeded | never half-migrated, in memory or on disk |
| **INTENT CREATION** | INTENTS | INTENTS | INTENTS fence | none (see limitation 5) | dict entry | `save` + full read-back | in-memory row already set, then verified | no intent, no POST | no intent, no POST | intent durable | unreadable/malformed ⇒ `RECOVERY_REQUIRED`, writes blocked |
| **FLOW CLASSIFICATION** | LEDGER | LEDGER | LEDGER fence | `expect_generation` | in-memory then `save()` | `JsonStore.save(LEDGER)` returns | on success; rolled back on failure | flow stays unclassified | stays unclassified | classified | an unclassified flow keeps CAPITAL blocked |

The rebase/attestation/hold-release rows share one deliberate asymmetry:
**the single-use token is burned in the append-only chain *before* the state
that spends it.** A crash between the burn and the commit leaves a token
that can never be replayed and an action that never happened. That is the
safe half of the pair; the alternative — burn after commit — leaves a token
that survives its own consumption. It is a one-way failure direction, it
costs an operator a new action id, and it never makes state look safer.

## 4. Concurrency semantics

* **One writer per path.** `JsonStore.save` takes the fence
  unconditionally, not only when a generation was supplied: a fenced writer
  and an unfenced writer are still a race.
* **Fences are re-entrant per process.** `flock` locks an open-file
  description, so a nested acquire on a second descriptor would deadlock
  against itself. A per-path `RLock` plus a depth counter prevents that
  while still excluding other processes.
* **Losing the fence is a refusal.** Never a downgrade to an unfenced
  write; on a CRITICAL file it also trips the persistence sentinel.
* **Readers are versioned.** `authority_is_current()` compares the durable
  generation against the one this instance holds. `GUARD_STALE_READER`
  fires before any other accounting guard, and `require_current_authority`
  refuses rebase, attestation and hold release outright.
* **The continuity chain serialises its own appenders** with `flock` and
  re-reads the tail inside the lock, so two appenders cannot compute the
  same `seq`.

## 5. Account and environment binding (A20)

`DATA_DIR` has no environment segment, so a directory populated under DEMO
and later mounted under PROD used to load cleanly — its numbers perfectly
self-consistent and about a different account.

`account_binding.py` stamps a non-secret fingerprint into the ledger and
re-checks it on load: environment name, API host, and the SHA-256 of the
**public API key id** (never the private key; hashed anyway so no
identifier lands in a state file or a log).

| Case | Detected by | Outcome |
|---|---|---|
| DEMO → PROD, PROD → DEMO | `environment`, `api_host` | `mismatch` → `GUARD_ACCOUNT_BINDING`, CAPITAL ineligible |
| PROD account A → PROD account B | `key_fingerprint` | `credential_changed` → blocked |
| Credential rotation, same account | **indistinguishable** | blocked; operator re-binds with `STATE_BINDING_REBIND_ACK=<fingerprint>` |
| New deployment, same account | nothing changes | `match` |
| Legacy state with no stamp | — | `unbound`: adopted and logged as *claimed, not proven* |

The rotation false positive is deliberate. Kalshi exposes no account
identifier through any endpoint this engine calls, so nothing local can
distinguish "same account, new key" from "different account". The
alternative false negative silently adopts another account's loss history.
The acknowledgement names the exact fingerprint being adopted, so it cannot
sit armed and accept whatever comes next.

## 6. Whole-volume rollback

**`WHOLE_VOLUME_ROLLBACK = EXTERNAL_AUTHORITY_REQUIRED`**
**`ROLLBACK_RESISTANCE = MITIGATED_WITH_LIMITATION`**

Closed, and tested: every rollback that leaves the continuity chain in
place — a coordinated journal+ledger restore, a backup recovery of an older
ledger, a stale concurrent writer, a crash between two durable steps, a
deleted watermark field, a zeroed counter, a replayed consumed token.

Not closed: a rollback of the whole volume, which takes the chain with it.
After such a restore every local file agrees, and agreement is all a local
check can measure. No purely local artefact can outrank a wholesale rewind
of the disk it lives on, and pretending otherwise would be the exact
failure this audit exists to prevent.

The interface for an external authority is *specified, not built*
(`continuity.EXTERNAL_AUTHORITY_CONTRACT`), because adding a network
dependency to the component whose job is to be trustworthy needs its own
review. It must answer "settled history and realized PnL at instant T",
live outside this filesystem, and not be rewritable by whoever holds this
filesystem. Candidates, by increasing cost: broker settled-trade history,
broker deposit/withdrawal history, a signed remote checkpoint, an
append-only remote log, an external monotonic sequence.

The entry point already exists and is the only door such evidence may use:
`EquityLedger.apply_attestation`, which binds an operator action id to a
hash of the funding records and refuses while any continuity block, journal
mismatch or unresolved flow is open. An implementation of the first two
candidates would compute that hash from broker data instead of from an
operator's file; nothing else in the ledger would change.

## 7. Known limitations

1. **Whole-volume rollback** — section 6. External authority required.
2. **Credential rotation** — section 5. Blocks; resolved by an explicit,
   fingerprint-specific operator acknowledgement.
3. **Token burn precedes the commit** — section 3. Deliberate, one-way,
   fail-safe.
4. **The fence is advisory within one host.** `flock` excludes processes on
   the same filesystem view. Two containers mounting the same volume are
   covered on Linux; NFS without a working lock daemon is not, and no
   deployment of this engine uses one.
5. **`pending_intents.json` has no generation counter.** It is fenced, and
   the intent is re-confirmed field-by-field at the linearization point
   immediately before transport, but it does not carry a monotone
   generation the way the ledger does. A second writer is therefore
   detected by content, not by version.
6. **Journal identity depends on the broker supplying an order id.** A
   settled row with no broker identity at all falls back to an economic
   tuple, and only when that tuple is fully populated — an unidentifiable
   row is left alone rather than merged, because over-deduplication would
   under-report history, which is the same error in the opposite direction.
