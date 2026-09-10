# Engine transaction and continuity model

Base: `3af848e6aebc8769ea59f878d68674f89e04af0d`.

CAPITAL stays OFF. This design does not authorize a deployment, a broker write,
an account migration, or promotion. The remediation implementer cannot grant a
capital safety verdict; an independent counter-review is required.

## Authority and failure semantics

The canonical real path of DATA_DIR identifies one local economic state root.
ExecutionEngine acquires a process-lifetime `WriterLease` before it loads any
economic component. An accidental second engine fails construction. The lease
must remain alive until the engine has stopped. A forked child is not the owner.
Standalone writers additionally use the same OS `flock` plus a process/thread
mutex for every critical transaction. Generation comparison occurs **inside**
that exclusion. A stale ledger generation or journal fingerprint is a refusal;
there is no automatic merge of another writer's history.

Every critical durable write has this protocol:

1. Acquire root exclusion; validate the current manifest and all its file hashes.
2. Check the external checkpoint, when a provider is attached to this runtime.
3. Prepare a private candidate. Ledger JSON and continuity records are staged
   without changing the authoritative object. Check the operation read/write sets.
4. Durably create `state_transaction.pending` before changing economic files.
5. Write a unique temporary file using write-all (including EINTR/short writes),
   fsync it, replace the target, and fsync its parent directory. Write and fsync
   the checksum; verify actual persisted bytes. Advance `state_authority.json`
   with a strictly greater root generation and hashes of committed files.
6. Validate the whole resulting manifest. Advance an attached external authority
   by compare-and-swap; run final freeze validators, then recheck the manifest
   and all file bytes. An ambiguous result is an incomplete transaction.
7. Remove the pending marker and fsync the parent directory. This is the **local
   transaction commit point**. Only then publish the private in-memory state.

No successful result is returned before this sequence completes. An exception
after any economic write leaves the pending marker; a restart requires recovery.
An older backup may be displayed diagnostically but cannot establish continuity
or unlock CAPITAL. Disk-full, permission, rename, fsync, checksum or readback
failures latch the persistence sentinel. The sentinel is not a recovery protocol.

This is a conservative commit protocol, not an automatic undo system: after a
failed operation the published in-memory candidate is unchanged, but some durable
files may already contain prepared evidence. The marker makes that uncertainty
explicit. Token consumption and ledger changes share the same transaction. An aborted
operator action may burn its token in a blocked transaction; a pure migration
prepare refusal writes neither its proposed seed nor speculative evidence. A
crash cannot silently make an ambiguous consumed token reusable.

## Operation contracts

All rows below inherit the root lock, manifest validation, durable primitives,
pending marker, external CAS and restart blocking described above. “Commit”
means step 7, **not** merely a successful data rename or JsonStore return inside
an enclosing transaction.

| Operation | Read set and preconditions | Write set / prepared state | Commit and publication | Crash before / during / after; restart |
|---|---|---|---|---|
| Settlement ingestion | Journal, position and fill-ID content fences; original execution evidence; complete broker outcome; immutable order/event identity; finite PnL | Private journal, positions, retained fill-ID set; journal + positions + seen_fill_ids + root authority | One position transaction, then publish committed component views | Before: old exposure. During: recovery required and old published objects. After: settlement and removal survive together. Missing original entry or unreadable outcome retains exposure; age does not prove settlement |
| Flow ingestion | Journal, positions, seed, cumulative accounted movements, pending observation, ledger generation | Private pending residual, stable movement evidence, flow rows, HWM, continuity and ledger | Ledger transaction then publish state | Before: previous residual. During: blocked recovery. After: distinct equal withdrawals remain distinct; repeated observation is idempotent |
| Intent creation | Orders, existing intent set, identity, quantity, price, side, root generation | Complete immutable payload, creation generation, PREPARED state and digest | Persist and reread complete intent before transport admission | Before: no transport. During: recovery required. After: intent remains available for broker-ID reconciliation |
| Order submission / cancellation | Root health, policy guards, known account, independent checkpoint, complete durable mutation intent | Complete HTTP mutation body and target in transport_intents; no speculative broker result | Local intent commit **precedes** network handoff; root lock spans both | Crash before handoff: intent pending. During/after handoff: broker outcome unknown until reconciliation. No automatic HTTP mutation retry |
| Rebase | Journal, orders, positions, pending intents, submission guard, ledger/continuity generations, authorization token, account, complete broker collection, verifiable execution freeze | Private HWM/rebase/hold/token set and continuity records | One ledger transaction; publish lower HWM only after commit | Before: old HWM. During: old published state plus recovery marker. After: new HWM and mandatory hold survive restart |
| Migration / seed | Exact proposal digest; source file fingerprints, journal frontier, cash, basis, schema, identity, continuity; unseeded state | Private seed, HWM, provenance, initial flows and continuity | One ledger transaction then publication | Before: unseeded. During: unseeded published state; recovery required. After: exact source frontier, no late settlement absorption |
| Attestation | Seed, full evidence digest, unresolved movement checks, continuity, one-use token | Private provenance, token and continuity | One ledger transaction then publication | Failure cannot publish RECONCILED; restart with partial transaction stays blocked. Funding hash alone never substitutes for independent continuity |
| Hold release | Current generation, existing hold, distinct operator action, validation reference, continuity and token | Private hold/rebase validation/token rows | One ledger transaction then publication | Failed or stale action cannot clear the published hold. Partial durable action requires recovery |
| Continuity advancement | Valid full chain, current root generation, existing floor and tokens | Append-only validated record and updated root manifest | Inside enclosing transaction, or standalone root commit | Torn/malformed tail is UNKNOWN and blocks. Short writes are completed or fail. No ignored malformed suffix |
| Recovery completion | Exact manifest digest reviewed by caller; independently CURRENT checkpoint; required files, all hashes/checksums, chain and ledger identity/schema; unique action ID | Append recovery_receipts; root authority; pending marker. Preserve every economic byte and existing hold/token | Receipt fsync, external CAS, exact readback, marker removal + directory fsync; then clear the matching process latch and require manager reload | Before: still blocked. During: pending recovery remains. After: exact independently committed snapshot retained. Missing/old proof or incomplete files are refused; no automatic reconstruction |
| Half-open risk claim/release | Risk-file content fence, current settlement anchor, loss streak and existing claim | Private risk state and root authority | Durable risk transaction then state publication | Failed release preserves the claim. Competing writer reloads. Date rollover retains consumed attempts until new settlement evidence |
| Order adoption and intent closure | Orders, submission guard and pending-intent content fences; exact client/order identity | Private adopted order + pending-intent removal + submission guard | One order transaction, then publish committed objects | Incomplete broker reply retains intention. Failed adoption never publishes removal. An unresolved generic transport record separately blocks further mutation |

## Rebase and broker atomicity

The read/version set includes root generation, journal, ledger, local orders, local positions,
pending intents, transport-intent fingerprint, submission guard, continuity, the ledger generation and account
identity. The root transaction rejects nested operations outside its prepared
write set. Every locally authoritative read is revalidated under the same writer
exclusion. State read from a manager cache alone is insufficient.

The existing broker adapter has no atomic account snapshot or verifiable broker
execution-freeze provider. Rebase therefore **refuses** when `execution_freeze`
is absent, expired, or fails verification. Sequential GETs remain diagnostic
evidence; they do not produce a freeze certificate. A future provider must bind
the account, a broker epoch, expiry and an execution exclusion scope; it must
continue to validate at commit. Synthetic tests use a broker whose epoch and
writers are entirely controlled by the test harness.

## Three different rollback problems

* **Local crash consistency:** incomplete markers plus durable write/readback
  prevent a data/checksum crash from masquerading as successful older recovery.
* **Partial local rollback:** the monotonic root manifest and continuity floor
  detect changed, missing or restored files while the stronger authority remains.
* **Whole-volume rollback:** every local byte can rewind together. Detection
  requires a source independent of that volume. No local hash provides this.

`continuity_authority.ContinuityAuthority` is an interface, not deployed
infrastructure. `verify_current(Checkpoint)` must authenticate a fresh nonce and
match account fingerprint, monotonic generation and digest. `advance(before,
candidate)` must perform a linearizable compare-and-swap and authenticate the
candidate reply. Replayed, missing, stale, unavailable or ambiguous replies block.
The caller may not attach a provider that simply echoes the request.

No provider is configured by default. Even an attested RECONCILED ledger is
CAPITAL_BLOCKED without current independent continuity proof. Production needs
an independently reviewed provider and a reconstruction workflow for snapshots
that cannot be proven CURRENT before
any capital promotion; this change does not fabricate either service.

## Identity, readers and read-only inspection

Identity is the canonical tuple `(kalshi, demo|prod, stable non-secret account
identifier)` and its SHA-256 fingerprint. Key IDs and credential bytes are not
account identity. Key rotation for the same account does not change the tuple.
Unknown identity and persisted/runtime mismatch block. Existing unbound state
requires explicit reconciliation/migration; it cannot acquire provenance by
changing an environment variable.

Before a capital-relevant decision, the reader validates its ledger generation,
journal fingerprint, continuity floor, root health, account and independent
checkpoint. Another committed generation causes STALE_INSTANCE / RELOAD_REQUIRED.
Readers do not automatically merge unseen economic history.

Diagnostic tools use readonly views. They read the manifest without acquiring or
creating a lock file, changing checksums, rotating backups, updating generations,
repairing state, or appending continuity. A refused diagnostic stays non-mutating.

## Deployment and engineering boundaries

Local durability assumes a filesystem implementing flock, atomic same-directory
rename, file fsync and directory fsync. Unsupported semantics fail closed; remote
filesystems require their own qualification. Advisory locks coordinate engine
code, not arbitrary privileged filesystem editors. Hash validation detects edits
but cannot prevent arbitrary access to the backing volume.

Broker completion is not part of the local filesystem transaction. A committed
intent followed by a timeout is pending evidence, never evidence of absence.
The external provider and verifiable broker freeze remain explicit integration
requirements. A real HTTP mutation leaves `transport_intents.json` unresolved;
this implementation blocks every subsequent mutation until independently
reviewed outcome reconciliation is supplied. It does not infer completion from
an HTTP response, retry a mutation automatically, or fabricate a broker history
reconstruction service. This operational stop is intentional and is a remaining
integration limitation, not approval for production execution.

Only a current, complete independent snapshot can use `complete_verified_recovery`.
General economic reconstruction, migration between accounts and restoration of
missing external history are outside that completion API; they remain blocked.
CAPITAL stays OFF. Diagnostic reporting never invokes the recovery API.

## Additional preserved evidence

Completed raw journal rows are immutable even before the ledger observes them.
Corrections append linked events. Canonical broker evidence prevents a repeated
correction from gaining a new identity. A flow can be classified as a recorded
loss only by a new, unused negative correction for an existing trade whose
amount matches; an old or unrelated correction is not evidence of that flow.
Negative observations survive balance rebounds; rounding debits have a fixed
cumulative budget. Subcent settlement PnL is retained to six decimal places.
Unknown journal versions and position states are preserved and block; fill-ID
history is never silently truncated. Authentication kwargs cannot be persisted
in economic transport intents.

Forked children discard inherited lock bookkeeping/descriptors and cannot use
an inherited engine lease or ledger authority. They must construct fresh
managers from durable state. The OS writer lease remains owned by its original
process.
