# Astra final blocker remediation

Base: `90e460f091438534f50e4963cf04383d7532779c`.
Review scope: Claude's remaining A30–A35, M1 and M3 findings. Earlier A01–A29
contracts remain covered by the retained repository and historical suites.
This handoff requests independent counter-review only. CAPITAL stays OFF.
There is no merge, deployment, real broker request or external provider rollout.

## Result and evidence boundaries

| Finding | Implementation status | Verification and remaining boundary |
|---|---|---|
| A30 writer lease | PASS | ExecutionEngine acquires the process lease before constructing economic components. Competing instances fail construction; forked children lose authority; process death releases the OS lock. Per-transaction fencing remains. |
| A31 external proof | PASS | Host-pinned Ed25519 verification binds the complete challenge and purpose. Echoes, unknown keys, replay and stale checkpoints fail closed. A real independent authority still needs selection and operational qualification. |
| A32 continuity scaling | NEEDS_REVIEW | Measured linear per-append and quadratic cumulative growth. Full historical verification is retained; no compaction or weakened rollback detection. Capacity qualification and a reviewed incremental design remain open. |
| A33 account identity | PASS | Economic identity is stable account/broker/environment; independently signed credential-to-account mapping is separate. Rotation preserves account identity. The current adapter needs independent account attestation before CAPITAL. |
| M1 continuity and broker freeze | PASS | Rebase needs independently verified current continuity, account identity, and a nonexpiring all-writer broker fence covering an atomic empty exposure snapshot. Local locking alone never suffices. No capable real broker integration is claimed. |
| M3 transport lifecycle | PASS | Every legal transition is durable and history is retained. Possible-send failures remain UNKNOWN. Independent presence or authenticated final absence/rejection resolves an intent and allows a later distinct request. Restart resumes reconciliation. |
| A34 authority guards | PASS | The authority guard snapshot is immutable and cannot be lost by rebinding the economic guard list. |
| A35 test lock artifacts | PASS | Test bootstrap isolates default economic state in a temporary directory, with repository ignore rules as defense in depth. |

PASS describes the implemented local contract and its synthetic verification.
It does not qualify an unspecified external provider, broker capability or
production filesystem. Missing proof continues to block CAPITAL and rebase.
The operational need to select these authorities is intentional and remains
visible; an echoed operator label or locally restored checkpoint cannot satisfy it.

## Changes that matter for review

`WriterLease` uses a canonical root and a process-owned OS lock descriptor.
PID text has no authority, so a stale or reused PID cannot create a permanent
false block. Construction failure releases ownership. A fork hook revokes
inherited authority; descriptor identity checks also prevent a stale object from
closing a reused descriptor. This is exclusion for cooperating local writers,
not a claim about unrelated broker clients or arbitrary filesystem editors.

The authenticated evidence interface separates response data from the host's
trust pin. Atlas validates account, environment, generation, digest, challenge,
authority, time, monotonic checkpoint and purpose independently. Account
attestations bind the public credential identifier's fingerprint when available;
secrets never serve as persisted identity. Provider implementations must retain
their current checkpoint outside the economic rollback domain. No production
signer, discovery endpoint, freeze API or final-absence API is invented here.

Transport schema 2 records PREPARED, SENT, ACKNOWLEDGED, UNKNOWN, RECONCILING
and all three terminal outcomes. SENT commits before adapter handoff. A timeout
never automatically retries a mutation. A separate exact broker read can prove
presence; empty pages and elapsed time cannot prove absence. The optional signed
final-outcome contract requires finality and no later acceptance for absence or
rejection. Terminal evidence is immutable, and the persistence boundary rejects
row deletion and skipped history. Legacy PREPARED records are preserved verbatim
and migrate as UNKNOWN because their old format lacked a durable sent boundary.
Final integration review also identified and closed authority revocation during
a proof callback: the engine rechecks lease ownership and all runtime account
bindings after callbacks, before dispatch or terminal publication. The dedicated
regression retains the unresolved durable intent and permits zero dispatches.

High-level pending intentions now consume matching terminal transport evidence.
They cannot disappear after repeated empty polls. Exact order adoption and
absence closure still use the private multi-file transaction protocol. Restart
reconciles low-level intents even with an empty high-level pending collection.
An incomplete filesystem transaction remains RECOVERY_REQUIRED; reconciliation
does not fabricate missing state or erase its recovery marker.

## Reproducibility and validation

The original 1,177-test repository inventory is retained. The prior 96 repository
authority tests and 304 standalone Astra scenarios remain part of validation.
New modules cover process leases, authenticated continuity/account/freeze proof,
transport lifecycle, high-level resolution, and actual transport crash/restart.
Their inventory is 23 writer-lease tests, 68 authenticated-proof tests, 47
transport-lifecycle tests, 12 high-level resolution tests, and 4 actual process
recovery tests: 154 new cases. The combined inventory is 1,427 repository cases
plus 304 standalone controls, or 1,731 cases without double-counting subsets.
The final execution receipt records the exact Git commit, clean tree, source
identity, observed test counts, exit codes and report hashes. Test counts are
derived from execution rather than used to replace collection.

The 59 historical unsafe claims now have executable BEFORE/AFTER source and an
exact assertion mapping. Original baseline harness bytes are preserved, and the
runner verifies their hashes, imports the requested production tree, removes stale
results and checks actual assertion failures. Seven original uncertainties remain
separate; they are not relabeled reproduced defects. See
[the reproduction procedure](historical-reproduction.md).

The required two-order test dispatches synthetic order #1, independently observes
it, retains its CONFIRMED_APPLIED row, then prepares and resolves a distinct order
#2. Additional process tests kill the writer at PREPARED, after possible dispatch,
and in UNKNOWN. The broker history and signing authority live in the parent
process; restarting a child does not reconstruct the external checkpoint from
the economic volume. Delayed visibility remains blocked until proved.

The isolated launcher provides a fresh DATA_DIR, dummy credentials and inherited
Python socket audit denial. Only the canonical repository suite allows loopback
for its local dashboard tests. Synthetic adapters do not use real sessions.
This is Python-level transport isolation; an OS network namespace is not claimed.
No separately committed Claude regression suite was supplied; Claude's described
cases are represented by the new deterministic tests. Docker is unavailable in
this workspace, so `DOCKER_BUILD=NEEDS_REVIEW` until an actual build is performed.

Positive legacy broker fixtures now supply the immutable original quantity,
side/action and price required by the new evidence contract. Negative assertions
are retained. The former repeated-empty-poll closure expectation is strengthened
to require independently authenticated final absence; it is not silently dropped.

## Limits retained for the next reviewer

1. A32 history growth needs workload and restart capacity qualification. Both the
   full chain and terminal transport history remain retained; no silent cap exists.
2. A real trust authority, account mapping source, key custody/rotation/revocation
   and broker all-writer freeze must be selected and independently reviewed before
   their respective operations can be enabled. Default operation stays blocked.
3. The current broker adapter cannot prove final absence after possible send.
   Unknown invisible outcomes remain blocked until independently resolvable.
   Generic operations without reviewed completion semantics also remain blocked.
4. Docker and the actual production storage durability/locking environment still
   require qualification. Synthetic success does not establish either property.

Detailed contracts: [authenticated authority](../design/authenticated-authority-contract.md),
[transport lifecycle](../design/transport-intent-lifecycle.md),
[transaction model](../design/engine-transaction-model.md), and
[continuity scaling](../design/continuity-scaling-review.md).
