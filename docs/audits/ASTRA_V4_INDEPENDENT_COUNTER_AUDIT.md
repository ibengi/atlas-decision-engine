# Atlas Alpha v4 — independent defensive counter-audit

## A. VERDICT

**REJECTED**

Exact candidate: `57d497566b9a218919c5934046edd67e61b9ff43`. Rejected v3 base: `762c794538ab5b2daf1e92c1a6c7e46c88504d3d`. Branch: `alpha/astra-candidate-feed-v4-remediation`.

**The canonical local repository runner passes all 2,020 tests**, with zero failures, errors or skips. [Hosted CI run 34703319374](https://github.com/ibengi/atlas-decision-engine/actions/runs/34703319374) is completed/success on the exact final SHA. The only file changed between code SHA `f3ffc74d10a6e9cd09512c4f68375dda875034da` and final head is `docs/audits/ASTRA_V4_REMEDIATION_REPORT.md`; no executable change intervenes. The local unittest-based canonical runner does not separately tally the reported 699 pytest subtests; that count is not claimed as independently reproduced.

All 277 source blobs and the complete Git tree `4fe323124f44127262ab8820d9291b31c3cb00f6` were verified. The v3→v4 diff changes 29 files. Eighteen protected execution/configuration/economic files are byte-identical to v3. No candidate source or repository branch was modified. Static review and independent local witnesses establish residual defects despite the green suite.

**Prior RA closure: 2 PASS, 2 PARTIAL, 11 FAIL.** All nine previously passing AA/NEW controls remain PASS. The prior exact M26 deletion is now killed behaviorally. Across 42 tested variants, 40 are behaviorally killed and two are diagnostic-only; no effective tested survivor remains. The runner nevertheless mislabels diagnostic and unittest setup failures as behavioral kills.

| Independent evidence family | Result |
|---|---|
| Contract/source | 243 checks: 215 PASS, 28 failed expectations; zero harness errors. |
| Observer/spool | 91 scenarios: 86 PASS, 4 failed expectations, 1 availability limitation. One failure concerns an unused legacy helper, not a reachable blocker. |
| Persistence/accounting | 66 cases: 46 PASS, 20 failed expectations; zero harness errors. Parallel budget admission and negative cost have explicit applicability qualifications. |
| Settlement/report paths | 145 checks: 114 PASS, 31 failed expectations; zero harness errors. Includes six safe-refusal quarantine classification gaps and the scoped internal-pair boundary. |
| Runtime authority | 42 checks PASS; no attempted forbidden imports. Static analysis additionally parses 94 root/tool modules. |
| Mutation variants | 41 supplied + independent M26P; 40 behavioral kills, 2 diagnostic-only, 0 effective survivors. |

These are different units of evidence, not a summed repository test count. Fault injection used newly created disposable fixtures and a Python network-denying launcher. The original suite alone could use loopback for its local dashboard tests. No real provider, broker, credential or deployment was accessed by test code. Actual subprocess deaths/restarts were exercised; physical power loss and real Railway storage durability were not.

External evidence remains **UNPROVEN** and separate from code failures: real Railway `/data` restart proof; genuine automated Astra identity; authenticated live schema capture; qualified settlement authority; real research feed availability. None is used alone to fail a code control.

Scope limitations: local POSIX writer behavior does not establish multi-host safety; parallel budget admission is conditional on the documented one-service model; negative-cost handling needs a defined correction/refund policy; PID reuse conservatively retains orphan partial capacity; an unused legacy cleanup helper remains weaker. Internal source/snapshot mispairing below is a caller-fault witness, not proof that normal pending() creates mismatched pairs.

## B. FINDINGS

**V4-RA-01 — MEDIUM**

**File/function:** research_feed.py: settlement_source_identity; lines 184, 205.

**Exact defect:** Only name/url keys are inspected; unknown companion keys are ignored. Recursive members.extend accepts nested lists and silently drops empty nested lists, instead of validating the complete allowed container before normalization.

**Reproduction:**

```json
[
  {
    "source": [
      {
        "name": "A",
        "unsupported": {
          "ref": "B"
        }
      }
    ],
    "expected": "structured refusal, spool=0, predictions=0",
    "observed": "valid record, actual async spool=1, predictions=1"
  },
  {
    "source": [
      [
        {
          "name": "A"
        }
      ]
    ],
    "expected": "structured refusal, spool=0, predictions=0",
    "observed": "flattened source A, actual async spool=1, predictions=1"
  },
  {
    "source": [
      {
        "name": "A"
      },
      []
    ],
    "expected": "structured refusal, spool=0, predictions=0",
    "observed": "empty member dropped, actual async spool=1, predictions=1"
  }
]
```

**Impact:** Malformed/unsupported exchange-source structure is normalized into evidence carrying a valid checksum and becomes a durable prediction row. The synthetic rows used providers=[], INSUFFICIENT_DATA and p_meta=null; no claim of execution or real provider use.

**Required remediation:** Validate a declared, versioned source-object schema including unknown keys, and reject nested/empty member containers before normalizing. If extensions are supported, validate and retain them rather than silently discard them.

**Qualification:** All tested numeric/boolean/object/list URL companions now refuse. Malformed scalar members taint the collection. Explicit URL=null is not counted as a failure because v4 explicitly documents it as absent, and the current request does not require a URL for every source.

**Evidence index:** `evidence/contract-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-02 — HIGH**

**File/function:** research_feed.py: settlement_source_identity / settlement_source_comparator / candidate_from_market; lines 184, 205, 240, 249, 618.

**Exact defect:** Canonicalization still discards nested collection boundaries and unknown companion identity fields before alias comparison, rendering, and checksum. Distinct unsupported source structures therefore yield identical entire record preimages, and aliases naming those structures are accepted without contradictory_fields.

**Reproduction:**

```json
[
  {
    "left": [
      [
        {
          "name": "A"
        }
      ],
      [
        {
          "name": "B"
        }
      ]
    ],
    "right": [
      {
        "name": "A"
      },
      {
        "name": "B"
      }
    ],
    "expected": "reject unsupported shape or preserve distinction",
    "observed": "byte-identical complete records and equal SHA256; aliases accepted; actual async spool=1 and predictions=1"
  },
  {
    "left": [
      {
        "name": "A"
      },
      []
    ],
    "right": [
      {
        "name": "A"
      }
    ],
    "expected": "reject unsupported shape or preserve distinction",
    "observed": "byte-identical complete records and equal SHA256"
  },
  {
    "left": [
      {
        "name": "A",
        "authority_id": "ONE"
      }
    ],
    "right": [
      {
        "name": "A",
        "authority_id": "TWO"
      }
    ],
    "expected": "reject unknown identity field or preserve distinction",
    "observed": "byte-identical complete records and equal SHA256; aliases accepted; actual async spool=1 and predictions=1"
  }
]
```

**Impact:** Conflicting/raw malformed source evidence becomes indistinguishable after retention; a correct checksum can only attest to the lossy normalized preimage. This is not a cryptographic hash collision.

**Required remediation:** Define canonical structured identity only after complete schema validation; preserve supported collection boundaries and all supported identity fields, reject unsupported shapes/fields, and compare the same complete canonical value before rendering and hashing.

**Qualification:** Same display name/different URL, comma-name versus multiple members, escaped pipe/angle delimiters, reordered aliases, and duplicate names with differing URLs remain distinct or fail closed. A documented single object and singleton list remain intentionally equivalent.

**Evidence index:** `evidence/contract-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-03 — HIGH**

**File/function:** execution_engine.py:415-446 ExecutionEngine._shadow_observer; research_feed.py:83-99 _diagnostic / candidate_from_market.

**Exact defect:** candidate_from_market still executes on the observer; malformed alias diagnostic formatting raises and the unchanged outer observer calls synchronous BOT log.debug. Holding that real Handler stalls the engine cycle until release.

**Reproduction:** Run writer_probe.py through run_isolated.py. outer_observer_research_error_logging_stall passes a synthetic integer alias 10**4400 plus conflicting event_id through the real MarketOpportunityPipeline and unbound ExecutionEngine._shadow_observer with a synthetic holder. The research-specific DEBUG Handler sets entered and waits on Event; cycle is unfinished during 0.2-second held window and completes after release. No engine constructor, client or provider is created.

**Observed:** {"cycle_completed_after_release": true, "cycle_completed_while_held": false, "handler_reached": true, "orders_submitted": 0}

**Impact:** A malformed research observation and slow handler can delay the execution observer despite worker finalization isolation.

**Required remediation:** Place source normalization and exception reporting behind the same bounded worker handoff or make all observer normalization/error branches total, bounded and unable to synchronously log. Add actual pipeline/observer stall witnesses beyond emit_candidate-only tests.

**Qualification:** The concrete handler witness requires BOT DEBUG enabled. No claim that ordinary hashing remains on observer; those probes now pass.

**Qualification:** Checksum, full contract validation, finalizer, worker serialization, writer logging, fsync, consumer, spool scan and pruning now isolate correctly.

**Evidence index:** `evidence/writer-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-04 — MEDIUM**

**File/function:** research_spool.py:159-180 BoundedSpool._scan; write/_reserve_and_write.

**Exact defect:** FileNotFoundError from listdir is accepted as an empty spool, and from stat as a vanished record, including during a reserved write after the directory was created. Transient ENOENT hides existing occupancy and admits a second record past max_records=1.

**Reproduction:** Initialize an existing spool directory and existing.json, max_records=1. Inject FileNotFoundError(ENOENT) only for its listdir or for existing.json stat during spool.write; all real files remain present. Both writes return true, create a second complete JSON, preserve old bytes and report capacity_unknown=0. The genuinely absent initial directory positive control succeeds normally.

**Observed:** {"actual_records_after_write": 2, "capacity_unknown": 0, "configured_max_records": 1, "old_bytes_preserved": true, "write_return": true}

**Impact:** Inconsistent metadata can understate occupancy and defeat configured spool count/byte bounds.

**Required remediation:** Differentiate first-time construction of an absent directory from failure to enumerate a directory already initialized and reserved. In the admission scan, propagate uncertain ENOENT as SpoolCapacityUnknown and refuse the append; retry/recovery may occur asynchronously.

**Qualification:** ENOENT here is deliberate synthetic metadata inconsistency, not a claim that local POSIX stat normally lies; it exercises the explicit v4 transient inconsistency requirement.

**Qualification:** The original double-stat/isfile problem is fixed. EACCES, nonregular recognized suffixes, partial count/byte accounting, cleanup failures, short/EINTR/zero writes, multiprocess reservation and actual process-death restart recovery now pass.

**Evidence index:** `evidence/writer-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-05 — HIGH**

**File/function:** durable_append.py:203,216-222 append_line.

**Exact defect:** After the first directory open/fsync raises, the created file remains visible. The next serialized append sees created=False and returns success without ever successfully synchronizing the parent directory.

**Reproduction:** `directory_open_failure_retry_stays_closed`, `directory_fsync_failure_retry_stays_closed`.

**Impact:** Readable pathname is mistaken for established pathname durability. Append acknowledgement can resume while the same directory fault is still present.

**Required remediation:** Require successful directory synchronization before every acknowledgement whose pathname durability has not been established; make recovery perform that barrier. Do not infer prior barrier success solely from file existence.

**Qualification:** Measures missing barrier and false acknowledgement under injected errors. No actual power loss or disk corruption was simulated.

**Evidence index:** `evidence/durable-budget-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-06 — HIGH**

**File/function:** alpha_cost.py:493-503 BudgetGuard.__init__; 577-617 record_actual; 431-433 BudgetLedger.rows.

**Exact defect:** The new accounting_uncertain latch only survives the current guard. Zero-byte or EIO write failures leave no cost row; a fresh Python process initializes the latch empty and treats the empty ledger as zero spend.

**Reproduction:** `budget_zero_failure_restart`, `budget_write_eio_failure_restart`.

**Impact:** Restart can restore provider admission after a failed synthetic $2 cost record despite a $1 budget cap.

**Required remediation:** Use durable pre-dispatch reservations/usage intents and reconcile unresolved accounting after restart. A memory-only post-spend latch cannot preserve evidence after writes fail.

**Qualification:** No provider call was made; synthetic completed-usage records exercise the exact accounting interface.

**Evidence index:** `evidence/durable-budget-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-07 — HIGH**

**File/function:** alpha_cost.py:431-486 BudgetLedger.rows/spent/spent_today; 545-553 BudgetGuard.check.

**Exact defect:** JSON parsing is treated as sufficient spend schema validation. List rows or missing/null cost and missing timestamp rows are silently ignored, understating spend. Invalid timestamp raises ValueError outside the guard RuntimeError handler.

**Reproduction:** `budget_schema_list`, `budget_schema_missing_cost`, `budget_schema_null_cost`, `budget_schema_missing_timestamp`, `budget_schema_invalid_timestamp`.

**Impact:** Malformed but parseable evidence can produce allowed=True with $0 spend; another malformed row crashes rather than returning structured refusal.

**Required remediation:** Validate row shape, required timestamp and cost fields, numeric bounds and finiteness; convert any unverifiable row into structured accounting refusal while retaining its bytes.

**Evidence index:** `evidence/durable-budget-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-08 — HIGH**

**File/function:** alpha_ledger.py:511-535 commits/committed_prediction; alpha_service.py:658-699 _analyze_one.

**Exact defect:** Readable COMMIT bytes whose fsync failed are accepted as durable. A failed prediction fsync followed by a failed recovery-COMMIT fsync still produces terminal ANALYZED and scheduled observations.

**Reproduction:** persistence-recovery-probe.json / prediction_and_COMMIT_both_fail_fsync_before_ACK: both ledger fsync attempts return EIO, terminal acknowledgement=true; fresh Python process sees processed row and makes zero provider calls.

**Impact:** The prediction/receipt have no successful synchronization barrier after either write, yet processed state treats the analysis as durable. Physical power loss was not simulated.

**Required remediation:** Establish durability under the writer lock when recovering readable rows/receipts; propagate unknown synchronization outcome to a nonterminal state. Do not use an additional readable receipt as an infinite substitute for a confirmed barrier.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-09 — HIGH**

**File/function:** alpha_ledger.py:353-407 prepare/prepare_commits/prepare_is_durable; alpha_service.py:568-578 _analyze_one.

**Exact defect:** PREPARE_COMMIT adds another readable receipt and moves persistent fsync failure acceptance to the third poll.

**Reproduction:** persistence-recovery-probe.json / third_PREPARE_retry_with_persistent_fsync_failure: synthetic provider call counts [0,0,1] while every prediction-ledger fsync fails.

**Impact:** Analysis dispatch begins with no durable PREPARE announcement under an ongoing persistence outage.

**Required remediation:** Re-establish and confirm the ledger durability barrier before dispatch on every uncertain recovery path. Add three-or-more retry regression coverage and process-restart variants.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-10 — HIGH**

**File/function:** alpha_ledger.py:392-407 prepare_commits/prepare_is_durable and 511-535 commits/committed_prediction.

**Exact defect:** Receipt lookups key solely by analysis_id, ignoring their snapshot and prediction identities.

**Reproduction:** persistence-probe.json / COMMIT_identity_must_match_named_prediction_and_snapshot and persistence-recovery-probe.json / PREPARE_COMMIT_snapshot_identity_is_required each append malformed synthetic receipt rows and observe accepted=true.

**Impact:** Malformed or stale receipts qualify unrelated rows as committed/prepared.

**Required remediation:** Validate receipt schema and complete identity binding against the exact named row and requested snapshot before acknowledging durability; reject conflicting receipts.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-11 — MEDIUM**

**File/function:** alpha_service.py:299-330 cycle; 355-384 reconcile_processed.

**Exact defect:** Restart cycle does not invoke processed reconciliation. Explicit reconciliation checks only that the snapshot has some committed prediction and ignores a wrong processed prediction_id.

**Reproduction:** persistence-probe.json / orphan_ANALYZED_automatically_detected_on_restart_cycle: no automatic error or retry. persistence-recovery-probe.json / wrong_processed_prediction_id_detected_even_if_snapshot_committed: an ANALYZED mark names never-committed-id; normal cycle and explicit reconciliation leave it undetected.

**Impact:** Persisted acknowledgement/prediction disagreements remain hidden across restart and may suppress intake indefinitely.

**Required remediation:** Automatically validate processed-to-prediction identity on startup/poll, surface unknown reads and quarantine mismatches. Any correction should append a new status record, preserving history.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-12 — MEDIUM**

**File/function:** alpha_service.py:439-465 _acknowledge_recovered; 542-549 _analyze_one.

**Exact defect:** A legacy committed BUDGET_EXHAUSTED row remains nonterminal but is recovered forever; available budget never permits actual analysis.

**Reproduction:** persistence-recovery-probe.json / legacy_budget_refusal_eventually_retries: three new service instances with allowed budgets plus a fresh Python process all make zero synthetic provider calls; only legacy refusal remains.

**Impact:** The same observation is repeatedly deferred without any chance of completing analysis. Current-build refusals do retry correctly.

**Required remediation:** Define an append-only transition from historical refusal attempts to a real completed analysis and distinguish refusal attempts from completed prediction uniqueness.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-13 — MEDIUM**

**File/function:** alpha_consumer.py:147-171 ProcessedStore._current_generation/_load.

**Exact defect:** Metadata errors are mapped to generation=None and os.path.exists false; existing processed rows become ordinary absence without a refusal.

**Reproduction:** persistence-recovery-probe.json / processed_metadata_unknown_is_not_absence: A reads empty, B appends other, A appends mine; target stat EACCES makes A.seen(other)=false without error, while fresh healthy read is true.

**Impact:** Uncertain processed-state visibility triggers duplicate intake/recovery and prevents reliable terminal-state determination during storage faults.

**Required remediation:** Treat only ENOENT as absent; raise/return structured unavailable on other stat failures and preserve cache uncertainty explicitly.

**Evidence index:** `evidence/persistence-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-14 — HIGH**

**File/function:** alpha_ledger.py: settlement_qualification; qualified_resolved; calibration; lines 165, 212, 746, 791.

**Exact defect:** Qualification checks truthiness and field presence without comparing settlement_binding values to prediction source_binding, validating timestamp/evidence types, verifying recorded allow-list membership, or enforcing prediction/settlement chronology. The ingestion timestamp parser also enforces syntax only.

**Reproduction:** `direct_learning_mismatch_contract_id`, `direct_learning_mismatch_market_snapshot_id`, `direct_learning_mismatch_source_record_sha256`, `direct_learning_mismatch_environment`, `direct_learning_mismatch_contract_schema`, `direct_learning_malformed_timestamp`, `direct_learning_numeric_timestamp`, `direct_learning_string_false_flags`, `direct_learning_authority_outside_recorded_allowlist`, `direct_learning_malformed_evidence_id`, `ingress_learning_before_prediction`, `ingress_learning_future_resolution`, `direct_learning_missing_prediction_time`, `direct_learning_malformed_prediction_time`, `direct_learning_future_prediction_time`.

**Observed:** Each listed defective fixture yields learning_samples=1, calibration_samples=1 and Brier=0.16 for a hand-authored 0.6 forecast resolved YES. Ordinary ingress accepts resolution in 1900 before prediction in 2026 and resolution in 2999.

**Impact:** Invalid or hindsight-contaminated outcomes can enter calibration, model weights, learning reports and memory despite the qualification flag.

**Required remediation:** Use a shared strict qualification validator at ingress and replay. Compare every binding member to the committed prediction and retained source; require exact boolean flags, typed complete evidence identity, source consistent with recorded qualification metadata and valid ordered timestamps. Keep invalid rows immutable but excluded with reasons.

**Qualification:** No real settlement authority is asserted. Recorded allow-list consistency is a local code invariant distinct from external source authentication.

**Evidence index:** `evidence/binding-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-15 — MEDIUM**

**File/function:** alpha_learning_runtime.py: _protected_source_paths; write_learning_report; lines 122, 130, 169, 192.

**Exact defect:** The publisher accepts actual ProcessedStore and BudgetLedger objects but no Telemetry object or complete path registry. Telemetry constructor overrides are therefore omitted.

**Reproduction:** `report_extended_telemetry_custom_object_exact`, `report_extended_telemetry_custom_object_relative`, `report_extended_telemetry_custom_object_dotdot`, `report_extended_telemetry_custom_object_symlink`, `report_extended_telemetry_custom_object_hardlink`.

**Observed:** A Telemetry object attached to a synthetic AlphaShadowService is flushed with cycles=7. Publishing a report onto its custom actual path succeeds and changes its bytes; protected budget spend remains $12.50. Link-alias tests allow publication but preserve the original target bytes.

**Impact:** Custom telemetry evidence can be replaced by a derived report; the universal custom-path protection claim is incomplete.

**Required remediation:** Pass/register every actual persistence object/path, including telemetry, into the guard and wire actual objects at publishing call sites. Preserve existing inode/path alias checks.

**Qualification:** Only current production publisher call site is tools/alpha_learning_report.py:40; runtime argument capture confirms it passes actual default ProcessedStore/BudgetLedger objects. Default and configured telemetry paths are protected. No service startup publishes a report in the reviewed source.

**Evidence index:** `evidence/binding-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-16 — LOW**

**File/function:** alpha_resolution_ingest.py: _normalise; ingest_settlements; lines 128, 146, 275, 282.

**Exact defect:** Missing/null/blank prediction_id and source fail strict_text and go only to rejected[], bypassing the explicitly requested incomplete-binding quarantine bucket.

**Reproduction:** `required_quarantine_prediction_id_missing`, `required_quarantine_prediction_id_null`, `required_quarantine_prediction_id_blank`, `required_quarantine_source_missing`, `required_quarantine_source_null`, `required_quarantine_source_blank`.

**Observed:** All six cases append zero, learn zero and preserve ledger bytes, but quarantined=[] and rejected has one row.

**Impact:** Operator classification/evidence workflow does not meet the requested quarantine contract; this is fail-closed rather than unsafe acceptance.

**Required remediation:** Classify absent required identifiers consistently as incomplete/quarantined while continuing to reject malformed supplied identifiers.

**Evidence index:** `evidence/binding-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-17 — MEDIUM**

**File/function:** alpha_service.py; alpha_ledger.py; alpha_resolution_ingest.py: AlphaShadowService._source_binding/_analyze_one; source_binding_for; verify_source_evidence; ingest_settlements.

**Exact defect:** A correctly hashed canonical record for contract A can be paired with a snapshot for contract B at the existing local analysis boundary. The binding copies B from the snapshot while retaining A source evidence; digest recomputation succeeds without semantic cross-check.

**Reproduction:** `service_wrong_pair_refused_before_publication`.

**Observed:** Synthetic _analyze_one(snapshot_B,record_A) publishes binding contract B with source_evidence.contract_id A; ingest then appends binding_verified=true and source_evidence_verified=true.

**Impact:** A caller pairing error can create a semantically false provenance chain even when cryptographic digest verification succeeds.

**Required remediation:** Verify source contract/schema/observation identity matches snapshot and persisted top-level/source-binding identities before prediction publication and at settlement replay.

**Qualification:** Fault injection at an internal method boundary; normal SpoolConsumer.pending constructs matching pairs. This does not show a current normal-cycle mispair or a digest-verification failure.

**Evidence index:** `evidence/binding-findings.json`. Raw outputs and rerunnable harnesses are included.

**V4-RA-18 — MEDIUM**

**File/function:** tools/astra_mutation_probe.py: _classify (682-704), main (769-784).

**Exact defect:** The runner equates pytest failed summary entries with behavioral kills. Diagnostic-only M06/M17 are reported as behavioral. Real unittest setUp failure is reported by pytest as failed, passes collection, and is incorrectly called KILLED even though the test body never ran. Mixed failures and errors are counted as killed via startswith(KILLED).

**Reproduction:** Run harness/mutation_classifier_probe.py using run_isolated.py, then inspect mutation-classifier.json. Run mutation_audit.py and inspect M06/M17 raw logs; mutation_probe.py independently confirms their remaining fail-closed behavior.

**Impact:** Hosted or local mutation summary can overstate independent safety evidence; no broker or CAPITAL authority is created by this classification defect.

**Required remediation:** Collect structured per-test phase results (including unittest setup/teardown), require an invariant-specific semantic witness/assertion, classify message-only differences separately, and do not count setup/import/infrastructure uncertainty as a behavioral kill.

**Evidence index:** `evidence/mutation-findings.json`. Raw outputs and rerunnable harnesses are included.

## C. CLOSURE MATRIX

| Prior RA | Status | Basis |
|---|---|---|
| RA-01 | **FAIL** | Malformed companion URL fixes pass; unknown keys and nested/empty member containers still disappear. |
| RA-02 | **FAIL** | Name/URL and delimiter conflicts fixed; unsupported source structures still collapse before hashing. |
| RA-03 | **FAIL** | Hashing/validation/serialization now run in the worker; malformed outer-observer DEBUG logging still blocks. |
| RA-04 | **FAIL** | Double-stat bug fixed; reserved scans still undercount existing occupancy during injected transient ENOENT. |
| RA-05 | **FAIL** | First-call errors refuse; retry forgets failed directory synchronization. |
| RA-06 | **FAIL** | Shared appender works; accounting uncertainty is lost on restart and malformed rows are not safely validated. |
| RA-07 | **FAIL** | Original-ID propagation fixed; failed receipts, incomplete receipt binding and orphan reconciliation remain. |
| RA-08 | **FAIL** | Persistent PREPARE fsync failure allows synthetic dispatch on the third poll. |
| RA-09 | **PASS** | Own-append cache invalidation preserves intervening writers, including failed own-append tests. |
| RA-10 | **FAIL** | New v4 refusals retry; historical committed BUDGET_EXHAUSTED rows remain indefinitely deferred. |
| RA-11 | **PARTIAL** | All required missing/null inputs safely exclude; six missing/blank identifier cases bypass quarantine classification. |
| RA-12 | **PASS** | Missing/changed preimage, invalid digest and post-pruning corruption refuse; fresh-process recomputation passes. |
| RA-13 | **FAIL** | Simple unqualified rows now exclude; invalid chronology and complete-but-mismatched bindings still score. |
| RA-14 | **FAIL** | Budget/default/configured guards fixed; actual custom Telemetry paths remain omitted. |
| RA-15 | **PARTIAL** | M26 and exact prior deletion variant are behaviorally killed; runner still overcounts diagnostic/setup failures. |

| AA / NEW control | Status | Basis |
|---|---|---|
| AA-01 | **PASS** | Observed versus derived quote controls preserved. |
| AA-02 | **FAIL** | Complete source-container validation remains incomplete. |
| AA-03 | **FAIL** | Unsupported source structures still lose distinctions. |
| AA-04 | **PASS** | Canonical checksums independently verified at intake. |
| AA-05 | **PASS** | Original false/unknown/malformed provenance controls preserved. |
| AA-06 | **PASS** | Explicit observation identity and fresh-process deduplication pass. |
| AA-07 | **PASS** | Readiness provenance/derived-quote refusal preserved. |
| AA-08 | **PASS** | Genuine optional absence remains consistent. |
| AA-09 | **PASS** | Malformed input does not poison following valid intake. |
| AA-10 | **FAIL** | Normal worker isolation fixed; outer observer error logging remains synchronous. |
| AA-11 | **FAIL** | Partial/crash/concurrent reservation pass; transient metadata uncertainty still undercounts. |
| AA-12 | **FAIL** | Healthy complete writes/torn-tail handling pass; retry durability and malformed accounting fail. |
| AA-13 | **FAIL** | Normal recovery passes; repeated failures, receipt identity and historical refusal recovery remain. |
| AA-14 | **PARTIAL** | Shared appends, uniqueness and own-append cache fixed; uncertain processed metadata and accounting remain unresolved. |
| AA-15 | **FAIL** | Retention/recomputation and complete fields improve; semantic qualification and chronology remain incomplete. |
| AA-16 | **FAIL** | Known budget paths now protected; custom telemetry overrides remain omitted. |
| AA-17 | **PARTIAL** | Zero effective survivors among 42 variants; diagnostic/setup failures still misclassified. |
| AA-18 | **PASS** | Exact final SHA hosted CI independently verified; code-to-final diff is documentation only. |
| NEW-01 | **PASS** | 10**500 produces structured refusal without OverflowError. |

## D. CLAIM MATRIX

PROVEN is limited to the stated reviewed-source and synthetic evidence, not a deployment guarantee.

| Claim | Status | Scope |
|---|---|---|
| truthful market facts | **FAILED** | Unsupported source structures are silently normalized into accepted evidence. |
| provenance integrity | **FAILED** | Correct hashes do not restore source information lost before canonicalization; caller-pair consistency is incomplete. |
| checksum verification | **PROVEN** | Intake and settlement recompute retained canonical content; this is not live source authentication. |
| consumer fail-closed | **PARTIAL** | Original contract checks pass; source normalization and processed metadata uncertainty remain open. |
| readiness fail-closed | **PROVEN** | Tested malformed provenance, derived quotes and huge integers produce structured refusal. |
| append-only prediction ledger | **PARTIAL** | Normal prefix preservation and concurrent writes pass; uncertain commit acknowledgment remains unsafe. |
| append-only resolution history | **PARTIAL** | Normal immutable history/conflict tests pass; complete semantic qualification and durability remain incomplete. |
| nonblocking research isolation | **FAILED** | Worker stalls isolate; a malformed-input outer logger can still block the observer. |
| budget accounting durability | **FAILED** | Post-spend uncertainty is memory-only; restart/malformed rows can restore admission incorrectly. |
| restart persistence | **FAILED** | Healthy process deaths recover, but repeated sync failures, orphan IDs and legacy refusals remain unresolved. |
| deterministic settlement binding | **PARTIAL** | Complete fields/digest checks pass; chronology, replay equality and internal source/snapshot consistency remain incomplete. |
| learning qualification | **FAILED** | Complete-looking but invalid outcomes enter learning and calibration. |
| SHADOW_ONLY | **PROVEN** | Static paths, runtime import denial, empty local cycle and observer-return independence pass within reviewed scope. |
| broker writes | **PROVEN** | Zero actual broker writes/requests during audit. |
| CAPITAL authority | **PROVEN** | No introduced authority or change; PROVEN describes its absence, not permission to enable it. |
| exact-SHA CI | **PROVEN** | Run 34703319374 completed/success on final SHA; only documentation differs from code SHA f3ffc74d10a6e9cd09512c4f68375dda875034da. |
| live exchange schema proof | **UNPROVEN** | No authenticated live exchange capture supplied or attempted. |
| Astra identity | **UNPROVEN** | No independently authenticated automated Astra identity established. |
| qualified settlement authority | **UNPROVEN** | Local synthetic allowlist tests do not establish a real qualified authority. |

Static review found no Alpha/economic import paths in either direction. Runtime probes imported all 22 Alpha modules with forbidden imports denied, constructed an Alpha service with providers=[], and ran an empty local cycle. Synthetic guard dictionaries were refused without changing actual authority settings. Twelve observer return/exception controls preserve accepted/rejected economic decisions even for execution-looking return values. Provider inference transport exists in the source but no real request was made.

## E. MUTATION RESULTS

Classifications are independent of the supplied runner summary. M07/M22/M25/M26 selectors were extended with existing canonical regressions where the first failure was a counter, structural assertion or exception. M03 fails a valid-input positive control; M08 directly detects a forbidden dependency without executing broker code. M06/M17 change diagnostics while other guards continue refusing input.

| Mutation | Change | Independent classification |
|---|---|---|
| M01 | re-introduce the substituted settlement source ("kalshi") | **KILLED_BEHAVIORALLY** |
| M02 | re-introduce the 0.0 volume default | **KILLED_BEHAVIORALLY** |
| M03 | use the close time as the expected resolution time | **KILLED_BEHAVIORALLY** |
| M04 | ignore per-field provenance (SURVIVED in the rejected candidate) | **KILLED_BEHAVIORALLY** |
| M05 | exclude provenance from the checksum | **KILLED_BEHAVIORALLY** |
| M06 | accept legacy v1 records | **DIAGNOSTIC_ONLY** |
| M07 | allow a missing book side | **KILLED_BEHAVIORALLY** |
| M07P | accept a complete record whose quotes are declared DERIVED | **KILLED_BEHAVIORALLY** |
| M08 | add a forbidden execution import to the producer | **KILLED_BEHAVIORALLY** |
| M09 | let a producer exception propagate into the decision cycle | **KILLED_BEHAVIORALLY** |
| M10 | overwrite historical bytes instead of appending | **KILLED_BEHAVIORALLY** |
| M11 | accept legacy while preserving the diagnostics (SURVIVED in the rejected candidate) | **KILLED_BEHAVIORALLY** |
| M12 | coerce a numeric settlement-source member into a name (AA-02) | **KILLED_BEHAVIORALLY** |
| M13 | file a contradiction as an ordinary absence (AA-03) | **KILLED_BEHAVIORALLY** |
| M14 | count only complete files against the spool bound (AA-11) | **KILLED_BEHAVIORALLY** |
| M15 | treat readable bytes as proof of a durable commit (AA-13) | **KILLED_BEHAVIORALLY** |
| M16 | accept a partial settlement binding as verified (AA-15) | **KILLED_BEHAVIORALLY** |
| M17 | trust an unqualified settlement source by default (AA-15) | **DIAGNOSTIC_ONLY** |
| M18 | let the processed store skip the durable append protocol (AA-12) | **KILLED_BEHAVIORALLY** |
| M19 | raise instead of failing closed on a malformed number (NEW-01) | **KILLED_BEHAVIORALLY** |
| M20 | dispatch to providers without a durable PREPARE (AA-13) | **KILLED_BEHAVIORALLY** |
| M21 | re-dispatch an analysis that was already committed (AA-13) | **KILLED_BEHAVIORALLY** |
| M22 | leave an unserialized appender on a shared file (AA-14) | **KILLED_BEHAVIORALLY** |
| M23 | protect a guessed processed-state path instead of the real one (AA-16) | **KILLED_BEHAVIORALLY** |
| M24 | drop the source evidence the digest describes (AA-15) | **KILLED_BEHAVIORALLY** |
| M25 | log synchronously on the engine's own thread (AA-10) | **KILLED_BEHAVIORALLY** |
| M26 | swallow the directory fsync failure, so an undurable NAME reads as a durable append (RA-05) | **KILLED_BEHAVIORALLY** |
| M27 | return on the first present settlement-source key, leaving the rest of the member unvalidated (RA-01) | **KILLED_BEHAVIORALLY** |
| M28 | compare settlement-source aliases by the flattened comma-joined names again (RA-02) | **KILLED_BEHAVIORALLY** |
| M29 | serialize, hash and validate the record on the engine's observer thread again (RA-03) | **KILLED_BEHAVIORALLY** |
| M30 | classify spool entries with a second, disagreeing stat again (RA-04) | **KILLED_BEHAVIORALLY** |
| M31 | let an unreadable budget row make recorded spend SMALLER instead of unknown (RA-06) | **KILLED_BEHAVIORALLY** |
| M32 | discard the durable prediction row the ledger returned and keep the generated id (RA-07) | **KILLED_BEHAVIORALLY** |
| M33 | treat readable PREPARE bytes as a durable announcement on a retry (RA-08) | **KILLED_BEHAVIORALLY** |
| M34 | patch the processed cache and re-stamp its generation AFTER the append lock is released (RA-09) | **KILLED_BEHAVIORALLY** |
| M35 | promote a recovered spend refusal to a terminal ANALYZED (RA-10) | **KILLED_BEHAVIORALLY** |
| M36 | make the environment and the contract version optional in a settlement binding again (RA-11) | **KILLED_BEHAVIORALLY** |
| M37 | qualify a settlement without recomputing the prediction's retained evidence (RA-12) | **KILLED_BEHAVIORALLY** |
| M38 | let the learning report score every resolution, qualified or not (RA-13) | **KILLED_BEHAVIORALLY** |
| M39 | let the Meta engine's calibration lookup weight unqualified outcomes (RA-13) | **KILLED_BEHAVIORALLY** |
| M40 | drop the budget ledger from the report-overwrite guard (RA-14) | **KILLED_BEHAVIORALLY** |
| M26P | Independent exact prior survivor: remove new-ledger parent-directory fsync call entirely | **KILLED_BEHAVIORALLY** |

Mandatory semantic witnesses:

| Witness | Candidate | Mutant |
|---|---|---|
| M07P, all four quotes present and derived | minted=0; predictions=0 | minted=1; predictions=1 |
| M26, directory fsync error | Append refuses | Append reports success |
| M26P, exact prior deletion of directory barrier | File + directory barrier | File barrier only |

Existing canonical service tests now kill M26 and M26P through actual acknowledgement/dispatch outcomes. An independent classifier witness causes a real unittest setUp failure: collection succeeds, the body never runs, yet the runner labels it KILLED. That result should be INCONCLUSIVE and is a framework witness, not an additional application mutation or effective survivor. No tested application variant was classified INCONCLUSIVE, SURVIVED or NOT_APPLIED.

```text
SURVIVING_EFFECTIVE_SAFETY_MUTATIONS = 0
```

This is a finite result for these 42 variants; it does not establish coverage of every safety defect. The reported 41 behavioral kills are overstated: the independent count among supplied variants is 39 behavioral and 2 diagnostic-only.

## F. FINAL SAFETY COUNTERS

```text
BROKER_WRITES_DURING_AUDIT = 0
REAL_PROVIDER_REQUESTS = 0
CAPITAL_CHANGES = 0
MAIN_CHANGES = 0
PRODUCTION_DEPLOYS = 0
RAILWAY_CHANGES = 0
CREDENTIAL_CHANGES = 0
HISTORICAL_LEDGER_REWRITES = 0
```

All overwrite/corruption-shaped fixtures were created for this audit; no real historical ledger was rewritten. No candidate change, remote write, merge, deployment or CAPITAL authorization occurred.

## G. FINAL ADVANCEMENT DECISION

**REJECTED**

RA-01 through RA-15 are not all PASS and HIGH findings remain. Zero effective tested mutation survivors and intact SHADOW_ONLY isolation do not close those defects. Do not advance this candidate to the next integration stage.
