# V5 settlement qualification and retained evidence

The shared `alpha_settlement_validation.settlement_qualification` function
controls both settlement ingestion and historical learning/calibration replay.
An immutable resolution row or a stored `binding_verified` flag is insufficient.
The validator independently checks:

- Supported prediction, resolution, source-record and snapshot schemas.
- Exact prediction, contract, market-snapshot, environment and source-digest
  bindings; the snapshot must verify its own content-derived identity.
- A retained source preimage whose checksum recomputes, whose complete candidate
  contract is valid, and whose economic facts agree with the snapshot. This
  includes all four prices, sizes, rules, source identity, observation and market
  timestamps, and catalyst facts. Genuine optional event absence is preserved.
- Versioned retained settlement-source containers, independently normalized by
  the strict shared source-identity validator. Their rendered identity must agree
  with the source record. Unsupported structures, unknown versions and
  contradictory aliases are unqualified even if their checksum is recomputed.
- Boolean `true` qualification fields, a canonical settlement evidence identity,
  and the named authority's membership in the recorded operator-qualified list.
- A valid observation time, prediction time and resolution time. The prediction
  cannot precede its observation. Resolution must strictly follow prediction;
  neither timestamp may lie in the future relative to the qualification clock.

The authority allow-list records an operator's qualified-source policy. This
code does not authenticate a live exchange response or establish a real
settlement authority. Those remain external evidence requirements; the tests use
only synthetic authority names and source containers.

## Historical evidence limitation

The rejected v4 producer could discard unsupported settlement-source structure
before hashing its normalized record. A checksum cannot recover those lost raw
facts. V5 therefore retains `settlement_source_evidence`, versioned as
`atlas-settlement-source-v1`, inside every newly emitted canonical record.

Existing candidate-v3 records remain readable for SHADOW recovery and accounting.
Historical predictions that lack the new raw source preimage remain visible in
the immutable audit history, but their resolutions are **unqualified for
learning and calibration**. No migration invents the missing source structure,
rewrites a prediction, changes an outcome, or retroactively creates proof. A new
independently observed record is required to supply that evidence.

## Report publication boundary

The report guard protects the actual prediction and cost logs, configured
processed/budget/telemetry paths, supplied runtime persistence objects, and every
Telemetry path registered in the current process. Runtime reassignment retains
the old protected path and registers the new path and in-progress `.tmp` path.
Canonical paths and inode identity cover relative, parent, symlink and hardlink
aliases. Unexpected metadata errors refuse publication.

The process-local registry is held stable across the final path check and report
rename, so concurrent runtime configuration cannot insert a new source path
between those operations. A standalone caller with additional custom stores must
pass their actual objects through `persistence_objects` or the named store
arguments. A report is derived state and never grants execution authority.

All regressions use disposable ledgers and synthetic input. SHADOW_ONLY remains
in force; this policy authorizes no broker or provider request, CAPITAL change,
deployment or production integration.
