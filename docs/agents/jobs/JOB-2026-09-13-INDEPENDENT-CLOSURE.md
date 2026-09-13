# JOB-2026-09-13-INDEPENDENT-CLOSURE

Status: READY
Risk level: L2

## Accepted candidate context

Repository: `ibengi/atlas-decision-engine`
Candidate branch: `alpha/astra-candidate-feed-v5-astra-remediation`
Rejected base: `57d497566b9a218919c5934046edd67e61b9ff43`
Code SHA: `eb6f41e12b6a95e327a7c2ca5306443e21a37bf4`
Final SHA: `1d14a1ab6a436aeef0c81c7bca5d26eaac21de0d`

The last independent counter-audit found no reproducible FAIL-grade defect but returned OVERALL=INCONCLUSIVE because several invariants were not independently witnessed.

## Objective

Turn each remaining independent-evidence gap into either:
- PASS with a discriminating independent witness, or
- FAIL with a reproducible counterexample.

Do not re-audit already independently established items unless needed for a cross-finding interaction.

## Scope

Primary unresolved items:
- V4-RA-08 — COMMIT durability
- V4-RA-09 — PREPARE durability
- V4-RA-10 — receipt identity
- V4-RA-12 — append-only historical budget retry
- V4-RA-13 — ProcessedStore uncertainty/cache generation
- V4-RA-14 — settlement qualification/chronology
- V4-RA-17 — source/snapshot economic match
- NEW-01 — huge integer produces structured refusal

Partial items to complete:
- V4-RA-11 — append-only correction sub-claim
- V4-RA-15 — runtime registration race sub-claim

## Independent-audit rules

1. Work against exact final SHA `1d14a1ab6a436aeef0c81c7bca5d26eaac21de0d`.
2. Do not modify candidate implementation.
3. Use a separate disposable working tree.
4. Reproduce the original rejected-base witness when practical.
5. Every PASS requires a discriminating positive control; a validator that refuses both valid and invalid inputs does not prove correctness.
6. Prefer direct behavioral witnesses over assertions copied from v5 tests.
7. Keep code correctness separate from external operational evidence.
8. Return PASS / FAIL / INCONCLUSIVE per item.

## Required witnesses

### V4-RA-08
Demonstrate that readable PREDICTION/COMMIT bytes after failed synchronization never produce terminal acknowledgement until a real barrier succeeds. Include repeated failure and recovery.

### V4-RA-09
Drive at least five repeated PREPARE recovery polls with synchronization failure and prove provider dispatch remains zero. Then clear the fault and prove a valid path can proceed.

### V4-RA-10
Place malformed/stale receipts beside valid targets. Prove receipts with wrong snapshot/prediction/schema/digest/ordering cannot qualify another target. Include a valid receipt positive control.

### V4-RA-11 partial
Create a wrong processed prediction identity beside a committed snapshot. Prove automatic reconciliation appends a correction rather than mutating history, and prove the corrected path is stable on the next pass.

### V4-RA-12
Use a genuine historical BUDGET_EXHAUSTED-style row. Prove budget recovery permits exactly one linked successful successor while preserving old bytes. Include fresh-process replay.

### V4-RA-13
Inject metadata/read failures including EACCES and generation changes. Prove uncertainty never becomes absence. Include a healthy read positive control and returned-row aliasing check.

### V4-RA-14
Use a fully valid positive settlement that qualifies, then independently vary one dimension at a time: contract_id, market_snapshot_id, source digest, environment, schema, source authority metadata, evidence ID, strict booleans, malformed timestamps, resolution before/equal prediction and absurd future chronology. Invalid rows must contribute zero learning samples.

### V4-RA-15 partial
Exercise runtime persistence registration while report publication is in flight. Rebind/customize Telemetry/path and prove the active source cannot be overwritten. Include direct/relative/symlink/hardlink aliases and an unrelated-path positive control.

### V4-RA-17
This item must have a genuinely valid positive control that passes. Then pair the same identifiers with economically different source/snapshot content (prices, sizes or derived quote facts) and prove the mismatch is refused before dispatch and excluded from learning.

### NEW-01
Drive `10**500` and adjacent huge-number forms through the actual readiness path. Expected outcome: structured refusal, no OverflowError/ValueError escape, no publication/dispatch. Include ordinary finite positive control.

## Cross-finding checks

Attempt at minimum:
- RA-08 + restart + reconciliation;
- RA-09 + repeated process/service recreation;
- RA-10 malformed receipt + otherwise valid prediction;
- RA-12 historical refusal + later successful retry;
- RA-13 storage uncertainty + terminal skip logic;
- RA-14 chronology + replay/learning;
- RA-17 source/snapshot mismatch + settlement replay;
- RA-15 path rebinding + report publication.

## Existing findings to retain

The previous counter-audit identified:
- `V5-CA-01` LOW — substring-based mutation witness matching, latent/not currently reachable.
- `V5-CA-02` INFORMATIONAL — research hook diagnostic silence.

Do not let these distract from the independent-closure scope unless new evidence raises severity.

## External evidence not resolved by this job

This job does NOT establish:
- real Railway `/data` restart durability;
- authenticated live exchange-schema capture;
- genuine automated Astra identity;
- qualified real settlement authority;
- physical power-loss durability;
- distributed-filesystem safety;
- Docker build success.

## Output

Produce:
1. exact SHA audited;
2. per-item PASS / FAIL / INCONCLUSIVE matrix;
3. exact independent reproduction commands/harnesses;
4. positive controls and negative controls;
5. new findings if any;
6. cross-finding results;
7. remaining external evidence;
8. overall verdict:
   - `ACCEPTED_FOR_NEXT_INTEGRATION_STAGE`,
   - `REJECTED`, or
   - `INCONCLUSIVE`.

Do not declare production readiness or CAPITAL readiness.
