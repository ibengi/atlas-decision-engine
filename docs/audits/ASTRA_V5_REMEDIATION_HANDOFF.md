# Atlas v5 remediation handoff

Code verification handoff; final documentation-commit CI receipt is delivered separately.

- Repository: `ibengi/atlas-decision-engine`
- Branch: `alpha/astra-candidate-feed-v5-astra-remediation`
- Exact rejected base: `57d497566b9a218919c5934046edd67e61b9ff43`
- Exact code SHA: `eb6f41e12b6a95e327a7c2ca5306443e21a37bf4`
- Final SHA: the documentation commit containing this handoff; its exact SHA and hosted CI receipt are supplied in the final delivery.
- Scope: SHADOW_ONLY defensive remediation. No merge, deployment, real provider request, broker write or CAPITAL authorization.

The code tree was published from the exact rejected base. GitHub tree identities were compared with locally staged byte identities before branch updates. The final documentation commit changes only the handoff and verification receipt. Green CI is supporting evidence; the retained counterexamples, adversarial tests and mutation witnesses are the basis for the closure claims.

## V4 closure matrix

| Finding | Status | Invariant-level remediation | Permanent regression group |
| --- | --- | --- | --- |
| V4-RA-01 — Complete source-container schema | PASS | Unknown keys, nested collections and non-JSON members are rejected before coercion; malformed aliases cannot become absence. | `tests/test_astra_v5_producer.py: V5SourceSchema` |
| V4-RA-02 — Lossless structured source identity | PASS | Supported name/URL structures and all accepted raw aliases remain inside a versioned canonical checksum preimage; replay verifies them again. | `tests/test_astra_v5_producer.py: V5SourceSchema; binding.py: source identity tests` |
| V4-RA-03 — Nonblocking observer and diagnostics | PASS | Observer performs bounded primitive capture and nonwaiting admission only. Worker owns normalization, serialization, logging, scans, pruning and synchronization; contended queue/accounting locks refuse immediately. | `tests/test_astra_v5_producer.py: V5ObserverIsolation` |
| V4-RA-04 — Spool capacity under uncertain metadata | PASS | Any failed directory/member metadata read, including transient ENOENT, refuses admission. In-progress files consume records and bytes; uncertain/live owners prevent cleanup. | `tests/test_astra_v5_producer.py: V5CapacityUncertainty` |
| V4-RA-05 — Directory durability on every retry | PASS | Authority requires actual file and directory barriers, including newly created ancestors and configured symlink plus target ancestry, on recovery as well as first append. | `tests/test_astra_v5_durability.py: V4RA05DirectoryRetry; V5AdversarialGenerationAndAliases` |
| V4-RA-06 — Restart-safe accounting uncertainty | PASS | A durable reservation precedes synthetic dispatch; pending or unconfirmed usage remains a durable obligation across polls, process death and restart. | `tests/test_astra_v5_accounting.py: AccountingDurabilityV5` |
| V4-RA-07 — Strict BudgetLedger replay | PASS | Strict schema, type, identity, time and cost validation refuses malformed history; later valid rows cannot erase uncertainty or downgrade reservation-shaped rows to legacy usage. | `tests/test_astra_v5_accounting.py: StrictBudgetHistoryV5` |
| V4-RA-08 — No terminal state from unconfirmed COMMIT | PASS | A readable prediction or COMMIT remains nonterminal until a real generation-bound synchronization barrier succeeds. Recovery retains the original committed identity. | `tests/test_astra_v5_durability.py: V4RA08CommitDurability; recovery.py: acknowledgement failures` |
| V4-RA-09 — Persistent PREPARE failure never dispatches | PASS | Each authority attempt requires successful synchronization. Five to seven failed polls and recreated services cannot promote readable receipts into authority; no receipt chain is added. | `tests/test_astra_v5_durability.py: V4RA09PrepareDurability; recovery.py: seven-poll witness` |
| V4-RA-10 — Complete receipt identity and schema | PASS | Versioned receipts bind the exact canonical target digest, schema and identities and must follow the target. Supported historical receipts are strict and barrier-gated. | `tests/test_astra_v5_durability.py: V4RA10ReceiptBinding; V4LegacyReceiptRecovery` |
| V4-RA-11 — Automatic exact processed reconciliation | PASS | Each cycle and terminal skip revalidates the exact committed prediction. Incorrect acknowledgements receive append-only corrections; orphans remain quarantined without redispatch. | `tests/test_astra_v5_recovery.py: V5Recovery` |
| V4-RA-12 — Append-only historical budget retry | PASS | A genuine frozen-v4 BUDGET_EXHAUSTED row can receive one linked successful successor after budget/barrier recovery; old bytes remain unchanged across fresh processes. | `tests/test_astra_v5_recovery.py: legacy retry tests; durability.py: V4LegacyReceiptRecovery` |
| V4-RA-13 — ProcessedStore uncertainty is not absence | PASS | Metadata/read/barrier uncertainty refuses scheduling; caches are generation-checked and returned rows cannot mutate authority by aliasing. | `tests/test_astra_v5_recovery.py: metadata, cache and reconciliation tests` |
| V4-RA-14 — Shared semantic settlement qualification | PASS | Ingress and replay share exact identity, source-preimage, economic-content, schema, authority, evidence, strict-boolean and chronology checks. Invalid immutable history contributes zero learning samples. | `tests/test_astra_v5_binding.py: SettlementQualificationTests` |
| V4-RA-15 — Every actual custom persistence path protected | PASS | Runtime registration includes custom Telemetry objects, rebindings and temporary publication paths; final report validation and replacement share the registration lock. Relative, symlink and hardlink aliases are refused. | `tests/test_astra_v5_binding.py: RuntimePathProtectionTests` |
| V4-RA-16 — Consistent missing-identifier quarantine | PASS | Missing, null and blank required identifiers receive the same quarantine treatment and cannot enter qualified learning. | `tests/test_astra_v5_binding.py: missing required identifiers matrix` |
| V4-RA-17 — Source record and snapshot describe one observation | PASS | Canonical source evidence and the actual snapshot are independently compared by economic facts before dispatch and learning; matching labels or a valid checksum do not excuse different prices, sizes or derived quotes. | `tests/test_astra_v5_binding.py: source A/snapshot B tests; recovery.py: binding tests` |
| V4-RA-18 — Truthful mutation classification | PASS | Healthy baselines, structured test phases and reviewed invariant-specific witness frames distinguish real behavioral kills from diagnostic, setup, collection, import and infrastructure outcomes. | `tests/test_astra_v5_mutations.py: V4RA18TruthfulMutationEvidence` |

Regression group labels refer to classes or test families in the six committed v5 test modules; the exact method inventory is in the evidence package. The authoritative original report is committed at `docs/audits/ASTRA_V4_INDEPENDENT_COUNTER_AUDIT.md`.

## Historical non-regression matrix

| Control | Status | Verified invariant |
| --- | --- | --- |
| AA-01 | PASS | Observed versus derived quotes |
| AA-02 | PASS | Strict source normalization |
| AA-03 | PASS | Truthful absence, contradiction and source identity |
| AA-04 | PASS | Canonical checksum verification |
| AA-05 | PASS | False/unknown/malformed provenance refusal |
| AA-06 | PASS | Observation identity and restart deduplication |
| AA-07 | PASS | Readiness provenance/derived quote refusal |
| AA-08 | PASS | Genuine optional absence |
| AA-09 | PASS | Malformed input does not poison following valid intake |
| AA-10 | PASS | Research observer isolation |
| AA-11 | PASS | Bounded spool, partial files and concurrency |
| AA-12 | PASS | Complete durable shared appends |
| AA-13 | PASS | Commit/acknowledgement/retry recovery identity |
| AA-14 | PASS | Concurrent writers and stable caches |
| AA-15 | PASS | Retained source evidence and exact settlement binding |
| AA-16 | PASS | Actual report-path protection |
| AA-17 | PASS | Semantic mutation evidence |
| AA-18 | PASS | Exact final SHA hosted CI |
| NEW-01 | PASS | 10**500 yields structured readiness refusal |

AA, v3, v4 and the original mutation regression modules are included in both full test runs and in dedicated hosted workflow steps. Positive settlement/source fixtures were updated to represent coherent retained evidence and valid chronology; refusal assertions were preserved or strengthened. The genuine historical refusal fixture was generated by the exact frozen v4 writer and retained separately.

## Durability state and transaction boundaries

Readability is not durability. A successfully written or replayed row remains unconfirmed until the file and every relevant directory name pass actual synchronization and the same storage generation is verified. Failure remains nonterminal across polling and restart. A later successful barrier can confirm the original bytes and identity without adding another proof receipt.

Budget reservations become durable before any synthetic dispatch. Unknown usage remains a persistent obligation, including beyond ordinary spend windows. Confirmed usage is appended exactly once. A canonical process lock spans analysis recovery, dispatch, prediction commit and processed acknowledgement, closing the gap where a second process could previously redispatch after accounting but before prediction publication.

Predictions, resolutions, processed corrections and historical budget retry links are append-only. No historical record is rewritten to repair it. Invalid history remains auditable and cannot become learning input.

## New self-adversarial tests and defects fixed

| New challenge | Result / fix |
| --- | --- |
| Repeated fsync failures for five to seven polls and fresh process restart | No provider dispatch or terminal acknowledgement until a real barrier succeeds. |
| Malformed receipt beside an otherwise valid prediction | Exact schema, complete target identity and digest required; no recovery by partial match. |
| Storage generation changes between read, barrier and reconciliation | Generation-bound confirmation and rechecks refuse stale authority. |
| Malformed budget history followed by restart and a valid later row | Uncertainty remains; billed/effective inconsistencies and event-to-legacy downgrades are refused. |
| First process finishes accounting while prediction commit is stalled; second process polls same observation | Reproduced duplicate synthetic dispatch; fixed with one process-owned analysis transaction lock. Timeout defers, process death releases the kernel lock. |
| Resolution before/equal to prediction, year 1900, or far in the future | Ingress and replay refuse; a microsecond-after valid positive control qualifies. |
| Unsupported nested source extensions and rehashed false source proof | Strict raw source proof validation rejects unsupported or inconsistent identities. |
| Source A with snapshot B sharing identifiers but different prices or sizes | Economic content comparison blocks dispatch and learning. |
| Optional absent/null event identity | Genuine optional absence remains usable; contradictions still refuse. |
| Symlink target parent was never durably created; nested ancestor creation | Reproduced missing barriers; synchronize both configured alias and actual target ancestry. |
| Runtime-only custom Telemetry path changes during report publication | Reproduced registration race; shared registry/publication lock protects newly active state. |
| Malformed immutable historical settlement followed by otherwise valid new ingestion | Reproduced batch abort; structured refusal quarantines the affected observation and permits an unrelated valid settlement to proceed without rewriting history. |
| unittest teardown fails after a semantic assertion; diagnostic text mimics a witness | Classification remains inconclusive or diagnostic; neither counts as a safety kill. |

The evidence package retains failing before logs as well as successful final receipts. Failures from intermediate integration and adversarial checks are deliberately retained; they are not current unresolved findings.

## Tests

| Run | Result |
| --- | --- |
| Untouched exact v4 baseline, canonical runner | 2,020 passed; 0 failures, errors or skips |
| Final v5 canonical repository runner | 2,185 passed; 0 failures, 0 errors, 0 skips |
| Final full pytest repository run | 2,185 passed; 914 passing subtests; 0 failures/errors/skips |
| Newly committed v5 test methods | 163; plus 2 new semantic witnesses in the historical mutation regression module |
| Dedicated AA-01..AA-18 hosted regression step | 121 passed; 160 passing subtests |
| Dedicated v3 hosted regression step | 92 passed; 23 passing subtests |
| Dedicated v4 hosted regression step | 103 passed; 57 passing subtests |
| Dedicated v5 hosted regression step | 163 passed; 201 passing subtests |
| Historical mutation regression step | 28 passed; 23 passing subtests |
| Original independent source/contract probe | Before: 215/243 passed, 28 failed. After: 243/243 passed. |
| Original binding probe against rejected base | 114 passed, 31 failed, 0 harness errors; counterexamples retained in v5 tests |
| CI CLI smoke commands executed locally | Safety boundary, report/memory, settlement ingress and readiness: 4/4 passed |
| Mutation experiments | 53 healthy baselines; 50 behavioral kills; 2 diagnostic controls; 1 ineffective survivor |

Suite counts are overlapping executions, not additive unique-test counts. The canonical total includes five module-level tests. Pytest subtests are reported separately. Multi-process, crash/restart, short/EINTR/zero writes, metadata faults and history-prefix preservation are exercised synthetically.

Final canonical code-content identity: `f8b016d0b7cbc4ca730d6c30dc6636d374d7dfc2379306ad8f03cbe7791032ab`. Unchanged model-validation manifest SHA-256: `8745f027ce3819fa8a49959fb232292979bb0c1c33ce0d04e58248aeb1880a95`.

## Mutation table

| Mutation | Description | Classification | Effective safety mutation |
| --- | --- | --- | --- |
| M01 | re-introduce the substituted settlement source ("kalshi") | `SURVIVED` | No |
| M01P | fabricate a default settlement authority before retained source/provenance capture (stronger M01 variant) | `KILLED_BEHAVIORALLY` | Yes |
| M02 | re-introduce the 0.0 volume default | `KILLED_BEHAVIORALLY` | Yes |
| M03 | use the close time as the expected resolution time | `KILLED_BEHAVIORALLY` | Yes |
| M04 | ignore per-field provenance (SURVIVED in the rejected candidate) | `KILLED_BEHAVIORALLY` | Yes |
| M05 | exclude provenance from the checksum | `KILLED_BEHAVIORALLY` | Yes |
| M06 | accept legacy v1 records | `DIAGNOSTIC_ONLY` | No |
| M07 | allow a missing book side | `KILLED_BEHAVIORALLY` | Yes |
| M07P | accept a complete record whose quotes are declared DERIVED | `KILLED_BEHAVIORALLY` | Yes |
| M08 | add a forbidden execution import to the producer | `KILLED_BEHAVIORALLY` | Yes |
| M09 | let a producer exception propagate into the decision cycle | `KILLED_BEHAVIORALLY` | Yes |
| M10 | overwrite historical bytes instead of appending | `KILLED_BEHAVIORALLY` | Yes |
| M11 | accept legacy while preserving the diagnostics (SURVIVED in the rejected candidate) | `KILLED_BEHAVIORALLY` | Yes |
| M12 | coerce a numeric settlement-source member into a name (AA-02) | `KILLED_BEHAVIORALLY` | Yes |
| M13 | file a contradiction as an ordinary absence (AA-03) | `KILLED_BEHAVIORALLY` | Yes |
| M14 | count only complete files against the spool bound (AA-11) | `KILLED_BEHAVIORALLY` | Yes |
| M15 | treat readable bytes as proof of a durable commit (AA-13) | `KILLED_BEHAVIORALLY` | Yes |
| M16 | accept a partial settlement binding as verified (AA-15) | `KILLED_BEHAVIORALLY` | Yes |
| M17 | trust an unqualified settlement source by default (AA-15) | `DIAGNOSTIC_ONLY` | No |
| M18 | let the processed store skip the durable append protocol (AA-12) | `KILLED_BEHAVIORALLY` | Yes |
| M19 | raise instead of failing closed on a malformed number (NEW-01) | `KILLED_BEHAVIORALLY` | Yes |
| M20 | dispatch to providers without a durable PREPARE (AA-13) | `KILLED_BEHAVIORALLY` | Yes |
| M21 | re-dispatch an analysis that was already committed (AA-13) | `KILLED_BEHAVIORALLY` | Yes |
| M22 | leave an unserialized appender on a shared file (AA-14) | `KILLED_BEHAVIORALLY` | Yes |
| M23 | protect a guessed processed-state path instead of the real one (AA-16) | `KILLED_BEHAVIORALLY` | Yes |
| M24 | drop the source evidence the digest describes (AA-15) | `KILLED_BEHAVIORALLY` | Yes |
| M25 | log synchronously on the engine's own thread (AA-10) | `KILLED_BEHAVIORALLY` | Yes |
| M26 | swallow the directory fsync failure, so an undurable NAME reads as a durable append (RA-05) | `KILLED_BEHAVIORALLY` | Yes |
| M26P | remove the actual directory synchronization primitive (exact prior barrier-deletion variant, RA-05) | `KILLED_BEHAVIORALLY` | Yes |
| M27 | return on the first present settlement-source key, leaving the rest of the member unvalidated (RA-01) | `KILLED_BEHAVIORALLY` | Yes |
| M28 | compare settlement-source aliases by the flattened comma-joined names again (RA-02) | `KILLED_BEHAVIORALLY` | Yes |
| M29 | serialize, hash and validate the record on the engine's observer thread again (RA-03) | `KILLED_BEHAVIORALLY` | Yes |
| M30 | classify spool entries with a second, disagreeing stat again (RA-04) | `KILLED_BEHAVIORALLY` | Yes |
| M31 | let an unreadable budget row make recorded spend SMALLER instead of unknown (RA-06) | `KILLED_BEHAVIORALLY` | Yes |
| M32 | discard the durable prediction row the ledger returned and keep the generated id (RA-07) | `KILLED_BEHAVIORALLY` | Yes |
| M33 | treat readable PREPARE bytes as a durable announcement on a retry (RA-08) | `KILLED_BEHAVIORALLY` | Yes |
| M34 | patch the processed cache and re-stamp its generation AFTER the append lock is released (RA-09) | `KILLED_BEHAVIORALLY` | Yes |
| M35 | promote a recovered spend refusal to a terminal ANALYZED (RA-10) | `KILLED_BEHAVIORALLY` | Yes |
| M36 | make the environment and the contract version optional in a settlement binding again (RA-11) | `KILLED_BEHAVIORALLY` | Yes |
| M37 | qualify a settlement without recomputing the prediction's retained evidence (RA-12) | `KILLED_BEHAVIORALLY` | Yes |
| M38 | let the learning report score every resolution, qualified or not (RA-13) | `KILLED_BEHAVIORALLY` | Yes |
| M39 | let the Meta engine's calibration lookup weight unqualified outcomes (RA-13) | `KILLED_BEHAVIORALLY` | Yes |
| M40 | drop the budget ledger from the report-overwrite guard (RA-14) | `KILLED_BEHAVIORALLY` | Yes |
| M41 | count failed pytest summaries as behavioral kills without structured evidence (V4-RA-18) | `KILLED_BEHAVIORALLY` | Yes |
| M42 | discard unsupported structured source extensions (V4-RA-01/V4-RA-02) | `KILLED_BEHAVIORALLY` | Yes |
| M43 | treat transient spool directory ENOENT as empty capacity (V4-RA-04) | `KILLED_BEHAVIORALLY` | Yes |
| M44 | admit a resolution before its prediction (V4-RA-14 chronology) | `KILLED_BEHAVIORALLY` | Yes |
| M45 | admit impossible future resolution chronology (V4-RA-14) | `KILLED_BEHAVIORALLY` | Yes |
| M46 | wait for a contended research queue mutex on the engine hook (V4-RA-03) | `KILLED_BEHAVIORALLY` | Yes |
| M47 | bind source A to economically different snapshot B (V4-RA-17) | `KILLED_BEHAVIORALLY` | Yes |
| M48 | leave a concurrency gap between provider accounting and prediction publication (v5 self-adversarial) | `KILLED_BEHAVIORALLY` | Yes |
| M49 | allow lossy historical source evidence into settlement learning (V4-RA-02/V4-RA-17) | `KILLED_BEHAVIORALLY` | Yes |
| M50 | drop atomic runtime path registration during derived-report publication (V4-RA-15) | `KILLED_BEHAVIORALLY` | Yes |

**M01 is an actual ineffective survivor, not a claimed kill.** The late fabricated source lacks independently replayable retained evidence; the zero-mint/zero-prediction witness and a positive complete-observation control both pass under that mutation. M01P moves fabrication before evidence capture and is killed behaviorally. M06 and M17 are diagnostic-only because other independent guards preserve refusal. No diagnostic or setup failure is included among behavioral kills.

```text
SURVIVING_EFFECTIVE_SAFETY_MUTATIONS = 0
UNRESOLVED_EFFECTIVE_SAFETY_MUTATIONS = 0
INCONCLUSIVE_EXPERIMENTS = 0
NOT_APPLIED = 0
SETUP_FAILURES_COUNTED_AS_KILLS = 0
```

The classifier supports KILLED_BEHAVIORALLY, DIAGNOSTIC_ONLY, INCONCLUSIVE_SETUP, INCONCLUSIVE_COLLECTION, INCONCLUSIVE_IMPORT, INCONCLUSIVE_INFRASTRUCTURE, SURVIVED and NOT_APPLIED. Raw final stdout/stderr and phase receipts are retained for each baseline and mutation.

## Hosted CI

Code commit CI: [34707841477](https://github.com/ibengi/atlas-decision-engine/actions/runs/34707841477), exact SHA `eb6f41e12b6a95e327a7c2ca5306443e21a37bf4`, conclusion `success`.
Final documentation-commit CI is checked after publication; its exact run ID and SHA are supplied with the external final handoff receipt. This document does not claim a result for a future commit.

Workflow: `.github/workflows/alpha-learning-v1.yml`. Hosted tests use the committed external-socket-denying isolation hook. Dependency installation occurs before isolation; it is not a model provider request. GitHub operations were limited to repository reading and publishing/verifying the requested candidate branch.

## Remaining engineering and external limitations

- No unresolved HIGH or CRITICAL defect was found after the final self-adversarial fixes; this is a remediation claim for independent verification, not a substitute for Claude’s counter-audit.
- Supported writer model: cooperating POSIX processes on one local filesystem. Symlinks share canonical writer locks. Hardlinked authoritative ledger files are deliberately refused. Distributed filesystems, hostile/noncooperating writers and physical power-loss behavior are not established by these tests.
- Repeated storage errors, unresolved usage, uncertain partial-file ownership and reused live PIDs may block progress indefinitely. They cannot be cleared merely because rows are readable, old, or inconvenient. Actual synchronization or reliable accounting/ownership evidence is required.
- Older v4 records may have already lost raw source-container structure. Their immutable rows remain available for supported SHADOW recovery but cannot qualify for settlement/learning without verifiable retained source proof. No qualification marker is invented retrospectively.
- A checksum proves preimage integrity, not external authenticity. Synthetic trusted-source policies verify the interface only; no real settlement authority is installed or authorized here.
- Chronology is checked against the local UTC clock; no independent time attestation is supplied.
- The runtime persistence registry is process-local. Standalone callers and separate processes must supply their actual custom persistence objects or paths to report protection; there is no global filesystem discovery claim.
- Derived report directory synchronization remains best-effort; a report may need regeneration after a crash. This does not replace or weaken authoritative source-history synchronization.
- Real Railway `/data` restart proof, genuine automated Astra identity, authenticated live exchange schema capture and a qualified real settlement authority remain external evidence requirements. No external target testing was performed.
- Docker was not built in this remediation: `DOCKER_BUILD=NEEDS_REVIEW`. No Docker success is claimed.

## Reproduction

Checkout the exact final SHA in a disposable clone and install the pinned repository dependencies in an isolated environment. No broker or provider credentials are needed. Run:

```sh
python tools/audit_isolation/run_isolated.py . /tmp/atlas-v5-tests.log run_tests.py
python tools/audit_isolation/run_isolated.py . /tmp/atlas-v5-mutations.json tools/astra_mutation_probe.py --json --evidence-dir /tmp/atlas-v5-mutation-phases
```

The committed launcher creates a fresh DATA_DIR, strips unrelated environment variables and denies external sockets/DNS. Only the canonical suite’s synthetic local dashboard tests permit loopback. Historical fixtures can be regenerated with `tools/astra_v5_legacy_fixture.py` and an independently checked out exact v4 base. No historical JSON result file is needed to execute the regressions.

## Final safety counters

```text
SHADOW_ONLY = true
BROKER_WRITES = 0
REAL_PROVIDER_REQUESTS = 0
CAPITAL_CHANGES = 0
MAIN_CHANGES = 0
PRODUCTION_DEPLOYS = 0
RAILWAY_CHANGES = 0
CREDENTIAL_CHANGES = 0
HISTORICAL_LEDGER_REWRITES = 0
CAPITAL_ENABLED = NO
MERGED = NO
DEPLOYED = NO
```

All provider calls and ledger fault injections used synthetic fixtures and disposable state. Protected broker/risk/order/CAPITAL configuration files remain byte-identical to the base. The sole execution-engine change moves the existing research observation handoff onto the bounded neutral queue. Alpha introduces no broker/execution/order imports or execution authority.
