# Continuity, fencing and crash semantics

Remediation of the independent Astra audit of `508899b` (findings A01, A03,
A09, A10). This document states what survives which failure, so that a later
reviewer can check the claim rather than infer it.

## 1. The problem the chain solves

Before this change the only record of "how much trading history has been
durably observed" was the `journal_watermark` field **inside**
`equity_ledger.json`. That made the evidence exactly as rewindable as the
state it protected:

| Rewind | Old outcome |
|---|---|
| delete the `journal_watermark` field | reads as "no history evidenced" |
| set `settled_count: 0` | the check is disabled |
| restore journal **and** ledger together | the loss never happened |
| a stale second writer saves an older watermark | last write wins |
| crash between the journal commit and the ledger write | the loss is unrecorded |
| crash between the ledger replace and its `.sha256` | an older backup answers |
| restore a snapshot from before a rebase | the consumed token is usable again |

A `.sha256` sidecar does not help: it is written beside the same bytes and
is restored with them. Detecting a rewind needs an authority the rewind does
not reach.

## 2. The continuity chain

`continuity.py` maintains `DATA_DIR/equity_continuity.log`:

* **Separate file.** Not a `JsonStore` file: no `.bak` rotation, no sidecar
  checksum, so the backup/restore machinery that rewinds the ledger does not
  rewind it.
* **Append-only.** Records are written with `O_APPEND`, fsynced, and the
  containing directory is fsynced. Nothing in the engine truncates or
  rewrites the file. Concurrent appenders take an exclusive `flock` and
  re-read the tail inside it, so two writers cannot both claim the same
  sequence number.
* **Hash-chained.** Each record carries the hash of its predecessor.
  Truncation, reordering or an edited record breaks the chain and is
  refused, not believed.
* **Monotonic sequence.** `seq` increases by exactly one; a gap or a repeat
  is a broken chain.
* **Strictly validated.** A missing field, a non-finite number, an unknown
  `kind` or an unknown chain version invalidates the chain. Absence is never
  a benign default. The single exception is a torn **last** line, which is
  provably a crash mid-append and is ignored with the head unchanged.

Record kinds: `evidence` (journal + ledger history durably observed),
`token_consumed` (a single-use authorization was spent), `recovery` (a
blocking state was cleared by re-establishing the evidence).

### The chain is a floor, never a source of values

The chain says what was once durably true so that anything claiming *less*
is refused. The journal, the ledger and the consumed-token set must all be
at or above it:

* journal below the chain → **rollback**, blocking recovery state;
* ledger watermark below the chain → **rollback**;
* a token in the chain but absent from the ledger → **rollback**, and the
  token stays unusable regardless;
* the ledger merely *ahead* of its chain pointer → normal (a crash between
  the two commit steps), no block.

The blocking state (`continuity_block`, guard `continuity_rollback`) is
persisted, survives restarts, blocks CAPITAL and refuses every operator
action, and clamps the reported strategy equity to the lowest value the
chain ever evidenced — so the drawdown keeps showing the loss instead of
zero. It clears only when the journal and ledger again contain everything
the chain records, verified against that independent chain. That is the
"independently reconstructed and verified" exit; there is no timeout and no
flag.

### Stated limitation

A restore that also removes or rewinds `equity_continuity.log` cannot be
detected from inside the process. No purely local artefact can outrank a
wholesale rewind of the disk it lives on. What the chain guarantees is that
every rollback which leaves it in place fails closed. Detecting the
wholesale case needs an authority outside this filesystem (broker funding
records, an append-only remote log); the `UNRECONCILED` /
attestation path is where such external evidence enters. `state_restore`
additionally **refuses** to write economic state onto a volume whose chain
already evidences history the restore does not carry.

## 3. Fencing

`equity_ledger.json` carries a `generation` counter. `JsonStore.save(...,
expect_generation=N)` re-reads the generation on disk immediately before
writing and **refuses** if it is not `N`. A writer that loaded generation N
therefore cannot clobber state another process moved to N+1. A refused write
is not an error to retry: it means this writer is stale.

One exception, deliberate: when the ledger was answered from a rotation copy
(the primary existed but failed its checksum), the reader adopts the on-disk
high-water generation while keeping the recovered content, so its next
commit explicitly supersedes the unverifiable primary. Without that a
checksum-torn write would leave a ledger that can never be corrected —
stuck, not fail-closed. The same boot blocks on continuity precisely because
that content is not proven current.

## 4. Commit order and interruption points

### Ledger commit (`EquityLedger.save` / `_commit`)

1. the watermark advances **in memory only**;
2. the evidence is appended to the continuity chain, fsynced, directory
   fsynced;
3. the ledger is written through a fenced atomic replace: temp file written
   and fsynced, backups rotated, `os.replace`, directory fsync, then the
   `.sha256` sidecar, then a second directory fsync.

| Interrupted at | What survives |
|---|---|
| before 2 | nothing durable changed; the next load recomputes |
| between 2 and 3 | the chain is **ahead** of the ledger. Safe: the chain only asserts that history existed, so a journal that still contains it re-derives the watermark. A journal that does not is a rollback, and blocks — the correct verdict |
| inside 3, before `os.replace` | the old ledger is intact |
| after `os.replace`, before the sidecar | new data, stale checksum. `JsonStore.load` answers from a rotation copy **and records that it did**; that boot blocks on continuity rather than believing undated state |

### Rebase (`apply_rebase`)

PREPARE on a copy → VALIDATE → RE-VALIDATE against live state → COMMIT
durably → PUBLISH. Nothing authoritative moves until the durable write
returns success; on failure the previous state stays authoritative in memory
and on disk.

The single-use token is burned in the chain **before** the state that spends
it. A crash in that window leaves the safe half of the pair: a token that can
never be replayed and a rebase that did not happen. The operator issues a new
action id. The alternative ordering leaves a token that survives its own
consumption.

### Intent commit (`OrderManager._record_intent`)

Construct → persist → **read back** → only then allow broker submission. Any
failure at any step aborts before transport, with no retry to the broker and
no silent continuation; `pending_intents.json` is a critical basename, so the
failure also trips the persistence sentinel and the engine's global gate.

## 5. What binds a rebase to its authorization

`EquityLedger.bound_state()` fingerprints every local file the preconditions
read — `kalshi_trades.json`, `positions_state.json`, `orders_state.json`,
`pending_intents.json`, `submission_guard.json` — plus the ledger's fencing
generation. `equity_rebase_context` brackets the broker position read with
two order listings and fingerprints the local files on both sides of the
whole collection, publishing `evidence_unstable` when anything moved.
`apply_rebase` re-runs that collection and re-verifies the fingerprints
immediately before committing.

Two broker GETs never become one transaction, and this does not claim
otherwise. The bracket narrows the window to "zero orders observed on both
sides of the position read" and refuses on any disagreement. Combined with
the mandatory post-rebase `capital_hold`, which no rebase can clear by
itself, that is the most conservative protocol available at this boundary.
