# Alpha v5 durability and qualification

SHADOW_ONLY remains the only Alpha mode. Alpha has no broker execution
dependency and provides no input to execution, risk, or order authority.

## Durability state, independently of receipt identity

Every authoritative reader starts with unconfirmed durability. A successful
write creates READABLE_UNCONFIRMED bytes. A readable PREPARE or COMMIT receipt
proves neither file durability nor directory durability. Only a successful
file synchronization and directory synchronization of the same locked file
generation establish CONFIRMED state. An exception leaves durability UNKNOWN;
retry or process restart must cross the real barrier again. There is no new
receipt chain whose existence substitutes for that barrier.

Receipts separately bind a supported schema, the exact preceding row, analysis
identity, snapshot identity, and prediction identity where applicable. New
receipts also bind the canonical target-row digest. Supported historical v4
receipts are validated against their full declared schema and exact target;
they acquire no authority merely by being readable. Unsupported, conflicting,
or malformed receipts block recovery. Torn fragments remain immutable and
separate from subsequent valid appends.

The processed store synchronizes and checks its generation before returning
terminal state. Every service cycle reconciles terminal processed identities
against exact committed predictions. Corrections and orphan quarantine are
new events; original history is retained. A committed no-dispatch
BUDGET_EXHAUSTED attempt can have an explicitly linked append-only successor.
A completed analysis cannot acquire another completed prediction.

A separate process lock covers analysis recovery, PREPARE, synthetic provider
dispatch, prediction commit, and processed acknowledgement. This prevents a
second service from paying for the same analysis while the first has finished
usage accounting but has not yet committed its prediction. Lock timeout is a
nonterminal refusal; process death releases the kernel lock.

## Accounting before dispatch

RESERVATION is durable before a provider call can be admitted. USAGE completes
that exact reservation after a response. Missing, malformed, or unconfirmed
usage leaves an unresolved obligation across process restart. Completion uses
the same real synchronization and generation checks as admission.

Reservations are serialized with cap checks. Pending estimates and completed
usage consume provider, daily, and per-analysis limits. Only the current
process and guard instance can admit parallel peers in its own active analysis
group. Forks and fresh guards cannot inherit that exception. Unknown provider
usage remains blocked until reliable accounting evidence can resolve it;
elapsed time does not erase an obligation.

Unreserved post-spend accounting is refused. No implementation can reconstruct
an unrecorded external expense after process death if all post-spend writes
failed. Historical imports are explicit, append-only, strictly validated
usage. Refunds and corrections have no supported budget event schema and
cannot silently reduce the amount charged. Billed and effective cost claims
must agree.

## Source and settlement qualification

New records retain versioned accepted settlement-source containers inside the
checksum preimage. The supported source schema permits text, name/url objects,
or one flat collection of these; unknown extensions and nested collections
are refused. Replay validates the retained containers and their rendered
identity again. A checksum demonstrates integrity, not source authentication.

Older v4 records may already have lost raw source structure. Their historical
bytes are preserved and can be used for supported SHADOW recovery, but absence
of the retained source proof makes their settlements unqualified for learning.
No marker is invented retroactively. Independent new evidence is needed to
establish facts that the historical producer did not retain.

One validator serves settlement ingress and historical replay. It checks exact
prediction, contract, snapshot, digest, environment and schema identities;
verifies the source preimage and snapshot content; compares economic facts;
requires strictly typed qualification flags; checks the settlement authority
against the recorded qualified-source policy and validates evidence identity.
Prediction and resolution timestamps must be valid, the source observation
must not follow prediction, prediction must strictly precede resolution, and
neither prediction nor resolution may be in the future. Invalid history stays
visible in the audit view and contributes zero learning/calibration samples.

No real settlement authority or authenticated live schema is installed by
this remediation. Synthetic allow-lists exercise interface semantics only.

## I/O and persistence boundaries

The engine observer performs bounded primitive capture and nonwaiting queue
admission. Normalization, serialization, diagnostics, scanning, pruning and
filesystem synchronization run on research workers. Unknown spool metadata
cannot establish spare capacity. Partial files consume records and bytes;
unrecognized files and partials with a live or uncertain owner are retained.

Derived report protection includes actual runtime persistence paths, including
custom telemetry paths, aliases and temporary publication paths. Runtime path
registration is serialized with report publication. No report can replace an
already registered source path.

The supported writer model is cooperating POSIX processes on one local
filesystem. Symlink aliases use one canonical lock identity. Hard-linked
authoritative files are rejected; they are not a supported writer topology.
These tests do not establish multi-host/network-filesystem or physical power
loss behavior. Real Railway /data restart proof remains external evidence.

## Reproduction and evidence

The independent v4 report is retained at
`docs/audits/ASTRA_V4_INDEPENDENT_COUNTER_AUDIT.md`. Permanent v5 counterexamples
and neighboring tests are in `tests/test_astra_v5_*.py`; historical AA, v3 and
v4 tests remain in the suite. `tools/astra_v5_legacy_fixture.py` generates
genuine historical rows using a separately supplied exact v4 checkout.

For a credential-free local run, with dependencies already installed:

```sh
python tools/audit_isolation/run_isolated.py . /tmp/atlas-v5-tests.log run_tests.py
python tools/audit_isolation/run_isolated.py . /tmp/atlas-v5-mutations.json tools/astra_mutation_probe.py --json
```

The launcher gives tests a new DATA_DIR and denies external DNS and sockets.
Only the original suite's local dashboard tests may use loopback. Hosted CI
installs dependencies before enabling the same socket-denying hook. Mutation
runs use disposable source copies, an unmutated baseline, structured phase
evidence, and reviewed semantic witnesses. Setup, teardown, import, collection,
diagnostic and infrastructure outcomes are not behavioral safety kills.
