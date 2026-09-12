# Astra Alpha candidate feed — v4 remediation report

**Branch** `alpha/astra-candidate-feed-v4-remediation`
**Base SHA** `762c794538ab5b2daf1e92c1a6c7e46c88504d3d` (the v3 head Astra
counter-audited)
**Mode** `SHADOW_ONLY` throughout. No execution, broker or CAPITAL authority
was added, exercised or relaxed.

**Verdict** `SAFE_FOR_INDEPENDENT_REAUDIT`.

This report does **not** claim production readiness and does **not** claim
CAPITAL readiness. Neither claim is supported by anything below, and the two
external blockers the v3 report named are unchanged: no settlement authority
has been cryptographically authenticated, and no live shadow series long
enough to calibrate against exists.

---

## 1. What this remediation was asked to do

Astra re-audited `762c794` independently and returned fifteen findings,
RA-01 through RA-15. The instruction was to close all fifteen **while
preserving every previously passing control** — AA-01..AA-18 and NEW-01, all
of which had been brought to PASS in v3.

Every finding was reproduced as a **failing test from the counterexample
first**, in `tests/test_astra_v4_remediation.py`, and the reproductions were
kept rather than replaced by tests of the fix. A test that only describes the
fix cannot tell you whether the fix addressed the defect.

The reproductions are written in the counter-audit's own idiom: a synthetic
hostile witness built from the outside, asserting on what ends up in the
record, the spool, the ledger or the report — never on a counter or a log
line. That is the AA-17 lesson, and §5 below records the two places where
this work's own negative controls caught it being forgotten again.

---

## 2. RA-01..RA-15 closure matrix

| # | Finding | Where it lived | What was wrong | What it is now | Reproduction | Negative control |
|---|---|---|---|---|---|---|
| RA-01 | A valid name short-circuited validation of the rest of the member | `research_feed._settlement_source_name` | The loop `for key in ("name", "url")` returned on the first key that was present and readable, so `{"name": "CF Benchmarks RTI", "url": 8080}` was accepted as an authority with the malformed half of the same object never read. AA-02's rule — one malformed member taints the collection — was correct and simply never reached. | `settlement_source_identity` validates **every** identity key on every member and the whole container shape before normalizing. No early return. A container with no recognised identity is MALFORMED, not quietly empty. An explicit JSON `null` still reads as absent, stated as its own test. | `RA01_ValidNameSkippedTheRestOfTheContainer` | `M27` |
| RA-02 | Structured source identities were flattened before comparison | `research_feed`, via `resolve_alias`'s comparator | The comparator was the flattened `", ".join(names)` rendering. `[{"name":"A"},{"name":"B"}]` (two authorities) and `[{"name":"A, B"}]` (one comma-named authority) compared **equal**, as did one authority published at two different URLs, so AA-03's contradiction detection reported real disagreements as agreement — and the URL never reached the record at all. | Comparison is on the canonical **structured** identity. The text form is injective by construction (escaped `name <url>` members joined by ` \| `), so the defect cannot reappear in the rendering after being closed in the comparison. Conflicts still land in `contradictory_fields`, inside the digest, and the record is refused. | `RA02_StructuredSourceIdentitiesWereFlattenedBeforeComparison` | `M28` |
| RA-03 | The observer thread still hashed and validated | `research_feed.ResearchFeed._build` | AA-10 moved `write`, `fsync` and `prune` off the decision cycle, and its re-audit moved `log` off too. The CPU work stayed: every candidate of every cycle was serialized with `json.dumps`, sha256-hashed and walked field-by-field against the full contract on the engine's own thread, and a refusal then interpolated the whole error list into a diagnostic string before handing it over. | The producer is split. `_admit` — type checks and a shallow copy — is all the observer runs. Assembly, hashing, validation and every diagnostic are the writer's. `_finalize` is **total**: on the writer's thread a raise would become an uncounted drop, which is NEW-01 arriving by a different route. | `RA03_TheObserverThreadStillHashedAndValidated` | `M29`, and `M25` (see §5) |
| RA-04 | The spool scan took a second look and believed it | `research_spool.BoundedSpool._scan` | It stat-ed every entry — failing closed on an unreadable stat, correctly — and then asked `os.path.isfile(path)`, a **second** observation that swallows every `OSError` and can disagree with the first. Either way the bound lost a file that was occupying the volume. | The `st_mode` already in hand is used. An entry carrying a spool suffix that is not a regular file makes capacity **fail closed** rather than being skipped. | `RA04_TheSecondStatCouldDisagreeWithTheFirst` | `M30` |
| RA-05 | Unknown durability was reported as success | `durable_append` | `append_line` promises "no outcome in which a caller is told 'written' without durability having been attempted AND confirmed", and three paths broke it: `tail_is_torn` returned `False` on any `OSError`, and the directory `open` and directory `fsync` failures were both swallowed. Everything above it — PREPARE, the COMMIT receipt, the processed mark — is built on that sentence being true. | All three raise `DurabilityUnknown` (an `OSError` subclass, so existing error handling still applies). `BoundedSpool` routes its own directory fsync through the same function and reports such a write as **failed**. | `RA05_UnknownDurabilityWasReportedAsSuccess` | `M26` |
| RA-06 | The budget ledger had its own, weaker append | `alpha_cost.BudgetLedger` | AA-12 and AA-14 were applied to two of the three append-only files. This one kept its own `os.open` plus a **single** `os.write` plus `fsync`, with no short-write loop, no torn-tail separation and no writer lock — in the one file every cost cap is enforced against. `rows()` then dropped an unparsable row, so recorded spend came out **smaller**, and a swallowed `record_actual` failure made money that had been spent invisible. | `serialized_append`, like every other ledger. An unreadable row makes the total **unknown** rather than smaller. A spend whose row could not be made durable sets a sticky flag that blocks every further provider call. | `RA06_TheBudgetLedgerBypassedTheDurableProtocol` | `M31` |
| RA-07 | The acknowledgement named an id the ledger never wrote | `alpha_gateway.analyze`, `alpha_service._analyze_one` | `record_prediction` has a recovery branch — a PREDICTION row with no COMMIT receipt is completed and the **original** row returned, so one analysis keeps one identity — and the gateway discarded the return value. So `opportunity["prediction_id"]` stayed the freshly generated id, the snapshot was acknowledged with it, and follow-up observations were scheduled against it. `prediction_is_committed` said True throughout and was right: a prediction for that snapshot IS committed, just not the one being talked about. | The gateway adopts the durable row's id and keeps the generated one as `generated_prediction_id`, a diagnostic. The service takes the id it acknowledges and observes from a **fresh read** of the committed ledger row. | `RA07_TheGeneratedIdWasAcknowledgedInsteadOfTheDurableOne` | `M32` (see §5) |
| RA-08 | PREPARE durability was not re-checked on a retry | `alpha_ledger.prepare` | Idempotent by **lookup**: find a PREPARE row, return it. That row is read from the file — what AA-13's re-audit established is not proof of durability. An append whose `write` landed and whose `fsync` failed left bytes that read back perfectly, so the next poll declared the dispatch precondition satisfied and paid every provider against a ledger that was still unwritable. | PREPARE carries its own `PREPARE_COMMIT` receipt. A retry that finds a receipt-less row **finishes** its durability instead of trusting it, and `_analyze_one` gates dispatch on `prepare_is_durable`, re-read from the ledger. | `RA08_PrepareDurabilityWasNotRecheckedOnRetry` | `M33` |
| RA-09 | The processed cache advanced past another writer's row | `alpha_consumer.ProcessedStore.mark` | The cache was patched and the generation re-stamped **after** the append lock was released. A row another writer appended in between was inside the stamped generation and outside the cache, so the cache looked fresh — size, mtime and inode all matching — while missing a row on disk. A missing processed row reads as "never analysed", so the service pays for an analysis another writer already committed: the AA-13 double-spend through the cache instead of through a crash. | The cache is **invalidated inside the lock**, before the generation can move. Asserted by a same-process witness, a second-OS-process witness, and statically on the ordering. | `RA09_TheProcessedCacheAdvancedPastAnotherWritersRow` | `M34` |
| RA-10 | A budget refusal became a completed analysis on recovery | `alpha_service._acknowledge_recovered` | It marked `STATUS_ANALYZED` unconditionally. A spend refusal — DEFERRED on the pass that produced it — became terminal on the next pass that recovered it, and `seen()` then meant the observation was never retried: a cap meant to defer work had silently discarded it. | Two changes. A refusal in which no provider was asked now records **no prediction row at all** (the gateway takes a predicate, so it still does not interpret spend policy), so the snapshot stays genuinely retryable and the PREPARE trail still says "announced, then deferred". And recovery republishes the state the recovered row actually has, so the rows already in append-only history stay retryable too. | `RA10_ABudgetRefusalTurnedTerminalOnRecovery` | `M35` |
| RA-11 | "Required binding" stopped short of the identity it needs | `alpha_resolution_ingest` | `environment` and `contract_schema` were optional because they "narrow a match when present". Written out: a settlement from DEMO could be attached to a prediction made in PROD, and a settlement could be attached across a contract-version boundary — the exact reason v2 records are refused rather than migrated. Two more were required by nothing: absent `resolved_at` meant `resolve()` filled in the **ingestion** time, so every time-ordered statistic measured when a script ran; absent `settlement_evidence_id` meant the outcome could never be traced to the document that established it. | All five binding fields plus the resolution instant and the evidence identity are required. Missing or null is a **quarantine** with the missing names reported — the row is not malformed, it simply cannot be tied to the prediction it names. A present-but-unparseable value is still a malformed **reject**, as AA-15 asked. | `RA11_TheRequiredSettlementBindingWasIncomplete` (7 fields × 3 absence shapes) | `M36` |
| RA-12 | The retained evidence was never recomputed before qualifying | `alpha_resolution_ingest` | The check compared the settlement's `source_record_sha256` against the prediction's `record_sha256` — **two copies of the same claim**. Agreement says the settlement quoted the digest correctly and says nothing about whether that digest describes the evidence the prediction carries, which is the only question the retained copy exists to answer. `verify_source_evidence` existed since v3 and was never called from here. | The recomputation is a precondition of qualification, with its own `evidence_unverified` bucket, and its verdict travels into the resolution row as `source_evidence_verified` plus the recomputed digest. | `RA12_RetainedEvidenceWasNotVerifiedAtSettlement` | `M37` |
| RA-13 | Learning consumed unqualified settlements | `alpha_ledger.resolved`/`calibration`, `alpha_learning.learning_report` | `resolved()` joins every RESOLUTION row it finds, and everything downstream consumed that list — including `calibration()`, which the Meta engine uses to **weight** each model. A row appended with no binding, no trusted authority and no evidence verification carried `binding_verified: False` and `source_trusted: False` and was still scored, still weighted, still reported as the model's calibration. The trust metadata added in v3 was used for nothing. | `settlement_qualification` derives one verdict per row, **recomputing** the evidence digest at read time rather than trusting a stored flag. `qualified_resolved()` is what learning and calibration read; `resolved()` keeps everything, because deleting history would be the retroactive edit this subsystem forbids; and `metrics()` reports both series plus the counts, so the gap between them is visible. | `RA13_LearningConsumedUnqualifiedSettlements` | `M38`, `M39` |
| RA-14 | The report guard knew about three files out of five | `alpha_learning_runtime._protected_source_paths` | The prediction ledger, the cost ledger and the processed store were protected. `alpha_budget_ledger.jsonl` — append-only, and the file every cost cap is enforced against — was not, and neither was the telemetry file. `write_learning_report` ends in `os.replace(tmp, target)` and `filename` is caller-supplied, so a report published as that name destroys every recorded dollar in one syscall, after which `spent_today()` returns 0.0 and every cap silently means "unlimited". | Both are protected, by the object's real path, by the configured path resolved the way the object resolves it, and by the report-directory reading — the same three spellings AA-16's re-audit settled on. Exact, relative, `..`, symlink and hard-link aliases are each asserted, and the three previously protected files are re-asserted. | `RA14_TheBudgetLedgerWasNotProtectedFromTheReport` | `M40` |
| RA-15 | The directory-fsync barrier had no semantic test, and a non-zero exit was reported as a kill | `tests/`, `tools/astra_mutation_probe.py` | The RA-05 barrier was protected by assertions about an exception type and nothing else. And the runner reported `KILLED` for `returncode != 0` — which pytest also returns for a collection error, a usage error, an internal error or a fixture that raised in setUp. That is AA-17's own failure class pointed at the negative control instead of at the code: an assertion that fires for the wrong reason is worth less than no assertion, because it is believed. | `M26` exists and restores the swallowing; `RA15_TheDirectoryFsyncBarrierIsAssertedBySemantics` asserts the **outcomes** — no commit reported, no dispatch, no acknowledgement, nothing spooled — so the mutation fails on behaviour. The runner now confirms the detecting tests still **collect** under the mutation, then requires a test **body** to have failed with no collection or setup errors; everything else is `INCONCLUSIVE_*` and counts as a survivor. `surviving_effective_safety_mutations` is the number CI asserts. | `RA15_*`, `RA15b_TheRunnerDistinguishesBehaviouralKills` | the classification is itself unit-tested |

### Previously passing controls: all still PASS

`AA-01`, `AA-02`, `AA-03`, `AA-04`, `AA-05`, `AA-06`, `AA-07`, `AA-08`,
`AA-09`, `AA-10`, `AA-11`, `AA-12`, `AA-13`, `AA-14`, `AA-15`, `AA-16`,
`AA-17`, `AA-18` and `NEW-01`. `tests/test_astra_aa01_aa18_remediation.py`
and `tests/test_astra_v3_remediation.py` are unchanged in intent and run as
their own CI steps. Where their **fixtures** had to change — because RA-11
widened the required binding and RA-12 made qualification recompute the
retained evidence — the inputs were corrected and the assertions were left
alone, with the reason written at each site. §4 lists every such change.

---

## 3. Evidence

| Item | Value |
|---|---|
| Branch | `alpha/astra-candidate-feed-v4-remediation` |
| Base | `762c794538ab5b2daf1e92c1a6c7e46c88504d3d` |
| Full suite | see §6 |
| Mutation probe | see §6 |
| Hosted CI | see §6 |
| Broker writes | **0** |
| CAPITAL changes | **0** |
| Railway changes | **0** |
| `main` changes | **0** |
| Production deploys | **0** |
| Historical ledger rewrites | **0** |
| `SHADOW_ONLY` | true, unchanged |
| Alpha → broker/execution imports | none (`test_research_feed_boundary`, `test_alpha_safety_boundary`, CI "Safety boundary" step) |
| Execution/risk/order code consuming Alpha output | none |

### Tests the counter-audit asked for

* **Full canonical suite** — `pytest tests/ -q`.
* **All previous AA regressions** — `test_astra_aa01_aa18_remediation.py`,
  `test_astra_v3_remediation.py`, `test_astra_mutation_regression.py`.
* **Independent-style synthetic witnesses per finding** —
  `test_astra_v4_remediation.py`, one class per RA finding, each opening with
  the counterexample.
* **Crash and restart** — `RA08_*` (a PREPARE whose fsync failed, retried),
  `RA07_*` (a PREDICTION row with no receipt, recovered), `RA15_*` (a ledger
  whose directory entry cannot be persisted).
* **Short write / zero write / EINTR / EIO** — `durable_append.write_all` and
  `research_spool.write_all` under `AA12_*`; `EIO` on `getsize`, on the tail
  read, on the directory `open` and on the directory `fsync` under `RA05_*`;
  `EIO` on every regular-file `fsync` under `RA08_*`.
* **Stale-cache multiprocess** —
  `RA09_*::test_a_concurrent_writer_in_another_process_is_seen` spawns a real
  second interpreter.
* **Source conflict** — `RA02_*`, including the two shapes that used to
  compare equal and the two that must still agree.
* **Settlement-binding missing/null matrix** —
  `RA11_*::test_every_required_identity_field_is_required`, seven fields
  against three absence shapes, each on its own ledger.
* **Report-path overwrite** — `RA14_*`: exact, default, relative, symlink,
  hard link, telemetry, plus the three files AA-16 already protected.
* **Budget-ledger durability** — `RA06_*`.
* **Observer blocking** — `RA03_*` (thread identity, timing, and the static
  reachability check) plus the AA-10 blocking-handler cases, now re-pointed
  at the one branch the observer can still speak from.
* **Mutation probe including M26** — `tools/astra_mutation_probe.py`.

---

## 4. Tests whose fixtures changed, and why

Each of these corrected an **input**, never an assertion. The distinction
matters: a suite that adjusts its assertions to match new behaviour has
stopped testing anything.

| Test | Fixture change | Why it is a correction |
|---|---|---|
| `test_alpha_candidate_truth.AnAbsentFactIsNeverReconstructed.assertRefused` | Stopped reading the refusal off `emit_candidate`'s return value | RA-03 makes that value report ADMISSION. The refusal is asserted on the spool and the refusal counters, which is where a refusal is observable — a return value can be True while a record is quietly written; an empty spool cannot. |
| `test_alpha_candidate_truth.MalformedObservationsFailClosed` | Same, via a new `assertRefused` helper | As above. |
| `test_alpha_candidate_truth.EveryFailurePathLeavesTheEngineUntouched` | Asserts on the spool and the broker count rather than the return value | As above; the broker count was always the point of that case. |
| `test_alpha_automatic_feed.TheProducerCannotHurtTheEngine.test_emit_never_raises` | Patches `_admit` (the observer's half) instead of `_build` | `_build` is the writer's half now. A companion case was **added** for it. |
| `test_astra_mutation_regression.M09_*` | Patches `_admit`; the checksum-failure case drains the writer | The digest is computed on the writer's thread; the assertion moves with it. |
| `test_astra_v3_remediation.AA10_*` (`emit_path`) | `_admit`, `_finalize`, `_finalize_record` and the new identity helpers added to the allow-set | Strictly stronger: the set is what may not log, and it grew. |
| `test_astra_v3_remediation._BlockingHandler` | `self.release` renamed to `self.let_go` | `release` **shadowed** `logging.Handler.release`, so every emit through this handler raised `TypeError` after appending the record and killed the writer thread. The v3 AA-10 assertions held for the wrong reason. |
| `test_astra_aa01_aa18_remediation.AA13_*::test_prepare_is_durable_before_the_prediction` | Exact row list `["PREPARE"]` → `["PREPARE", "PREPARE_COMMIT"]`, plus a direct `prepare_is_durable` assertion | RA-08 added the receipt. The property — an announcement exists, it is durable, no prediction is committed yet — is now asserted directly instead of through an exact list. |
| `test_astra_aa01_aa18_remediation.AA15_*`, `AA14_*` | Settlement fixtures carry the full versioned binding and a real retained record | RA-11/RA-12. A synthetic `"c"*64` digest cannot be re-derived from anything, so those rows would land in the evidence quarantine and the cases would assert on refusals that happen for the wrong reason. |
| `test_astra_v3_remediation.AA15_*`, `AA15b_*` | Same | Same. `AA15_*::test_each_required_binding_field_is_individually_required` was also **widened** from three fields to five. |
| `test_alpha_resolution_ingest`, `test_alpha_candidate_truth.ResolutionIngestion*` | Same | Same. |
| `test_alpha_learning.FakeLedger`, `test_alpha_learning_runtime.FakeLedger` | Gained `qualified_resolved`/`unqualified_resolved` | Those doubles stand in for a ledger whose settlements came through the verified path, which is what those cases are about. The qualification rule itself is asserted against the real ledger in `RA13_*`. |
| `test_alpha_ledger.MetricsAreDerived.resolve_many` | Resolves through `ingest_settlements` instead of `ledger.resolve()` | RA-13: `calibration()` reads qualified settlements only. A fixture writing resolutions no production path can produce would be testing calibration on rows calibration is no longer allowed to read. |
| CI "Resolution ingest CLI boundary smoke test" | Fixture built by the production producer and `source_binding_for`; a new incomplete-binding refusal asserted first | RA-11/RA-12. This step is exactly where a library/CLI divergence has to be caught — it caught one twice in v3. |

---

## 5. Defects this work found in itself

Both were found by the negative controls, not by reading.

**The AA-10 control stopped covering the code it was written for.** RA-03
moved the refusal diagnostics onto the writer thread, which left `_admit`'s
"this candidate carries no provenance container" as the only thing the
observer's thread still says. AA-10's timing cases drive the refusal path, so
after RA-03 they no longer exercised `_note` at all — and `M25`, which
replaces `self.writer.note(...)` with a synchronous `log.log(...)`,
**SURVIVED** the entire suite as a result. A control that stops covering its
subject is not a control. Closed by driving the surviving branch against the
blocking handler, and by adding the v4 class to `M25`'s detecting selectors.

**The RA-07 gateway fix was untested independently of the service fix.**
`_analyze_one` reads the committed row back from the ledger, so the
service-level assertions pass whether or not the gateway propagates the
durable id — and `M32`, which restores the discarded return value,
**SURVIVED**. RA-07 asks for the identity to travel *through* the gateway,
which is what every other caller of `AlphaGateway.analyze` sees, so the fix
stays and a direct gateway-level assertion was added. This is the same shape
as v3's M19: a fix that was only covered through the hole it plugged.

Neither was visible in a green suite. Both are the reason the probe exists.

---

## 6. Final numbers

All figures from runs on this branch, Python 3.11, no network (`tests/_netblock.py`).

### Test counts

| Suite | Result |
|---|---|
| Full repository suite | **2020 passed, 699 subtests passed**, 0 failed, 0 errors, 0 skipped |
| `tests/test_astra_v4_remediation.py` (RA-01..RA-15) | 103 passed, 59 subtests passed |
| `tests/test_astra_v3_remediation.py` (AA-02/03/10..17, NEW-01) | 92 passed, 23 subtests passed |
| `tests/test_astra_aa01_aa18_remediation.py` (AA-01..AA-18) | 121 passed, 148 subtests passed |
| `tests/test_astra_mutation_regression.py` (effect-based) | 26 passed, 23 subtests passed |

The suite grew from **1916** at the v3 head to **2020**: +104 tests, of which
103 are the RA reproductions and the hosted-CI checks, and 1 is the
writer-side companion added to `test_alpha_automatic_feed` (§4).

### Mutation probe

`python tools/astra_mutation_probe.py --json`

| Metric | Value |
|---|---|
| Mutations run | **41** |
| Behavioural kills | **41** |
| Kills with setup errors | 0 |
| Inconclusive | 0 |
| Not applied (anchor drifted) | 0 |
| **`SURVIVING_EFFECTIVE_SAFETY_MUTATIONS`** | **0** |

The probe found two survivors on its first full run against this work, both
of which were real gaps and both of which are recorded in §5. The zero above
is the run after they were closed, not the run that was hoped for.

`M01`..`M11`, `M07P`, `M12`..`M25` are the v1–v3 set, unchanged. `M26` is
RA-15's named addition — the directory-fsync barrier. `M27`..`M40` add one
mutation per remaining RA finding:

| Mutation | Restores |
|---|---|
| `M26` | RA-05 — swallow the directory `open`/`fsync` failure |
| `M27` | RA-01 — return on the first present settlement-source key |
| `M28` | RA-02 — compare aliases by the flattened comma-joined names |
| `M29` | RA-03 — serialize, hash and validate on the observer thread |
| `M30` | RA-04 — classify spool entries with a second, disagreeing stat |
| `M31` | RA-06 — let an unreadable budget row make spend smaller |
| `M32` | RA-07 — discard the durable prediction row the ledger returned |
| `M33` | RA-08 — trust readable PREPARE bytes on a retry |
| `M34` | RA-09 — patch the cache after the append lock is released |
| `M35` | RA-10 — promote a recovered spend refusal to terminal |
| `M36` | RA-11 — make environment and contract version optional again |
| `M37` | RA-12 — qualify without recomputing the retained evidence |
| `M38` | RA-13 — let the learning report score every resolution |
| `M39` | RA-13 — let calibration weight unqualified outcomes |
| `M40` | RA-14 — drop the budget ledger from the report guard |

Every anchor is asserted present before its mutation is applied, so a
refactor that moves the code makes the probe fail loudly rather than
silently reporting a kill for a mutation it never managed to apply
(`AA17_*::test_every_mutation_names_the_tests_that_detect_it`).

### Safety counters

| Counter | Value |
|---|---|
| `BROKER_WRITES` | **0** |
| `CAPITAL_CHANGES` | **0** |
| `RAILWAY_CHANGES` | **0** |
| `MAIN_CHANGES` | **0** |
| `PRODUCTION_DEPLOYS` | **0** |
| `HISTORICAL_LEDGER_REWRITES` | **0** |
| `CREDENTIAL_CHANGES` | **0** |
| `SHADOW_ONLY` | `true` |
| `SURVIVING_EFFECTIVE_SAFETY_MUTATIONS` | **0** |

### Hosted CI

Run **34702602076** on `fc23f15` **FAILED**, and that is recorded here rather
than quietly re-run: `RA_HostedCITargetsThisBranch` parsed the workflow with
`pyyaml`, which `requirements-dev.txt` did not name, so the case raised
`ModuleNotFoundError` in CI while passing locally. This is the third time in
this remediation's history that hosted CI has caught something the local
suite structurally could not see (v3 caught a library/CLI divergence twice).
The fix names the dependency, keeps an always-running textual half, and skips
the parse loudly rather than failing on a bare environment — because the v3
AA-18 case skipped silently, which is why the gap existed at all.

**Run `34703125714` is GREEN on
`f3ffc74d10a6e9cd09512c4f68375dda875034da`.** All eighteen steps succeeded.
From the runner's own log:

```
2020 passed, 699 subtests passed in 103.10s          # full repository suite
SHADOW boundary PASS: candidate_contract.py, research_spool.py, ...
memory CLI PASS
durable report CLI PASS
CI settlement fixture built from the production producer
incomplete versioned binding QUARANTINED, as designed   # RA-11
unqualified settlement source REFUSED, as designed      # AA-15
resolution ingest CLI PASS                              # RA-11/12/13 asserted
feed readiness CLI fail-closed PASS
```

The mutation-probe step asserted
`surviving_effective_safety_mutations == 0`, `inconclusive == 0`,
`not_applied == 0` and `behavioural_kills == mutations_run` on the runner, not
only locally.

| Run | SHA | Conclusion |
|---|---|---|
| `34702602076` | `fc23f15` | **failure** — the pyyaml gap, see above |
| `34702857036` | `6257461` | success |
| `34703125714` | `f3ffc74` | **success** — the run this report is evidence for |

`release_evidence.json` at the repository root is NOT updated by this
branch: it is the release record for a DEMO deployment on a different branch
lineage, and nothing here is deployed. Writing a v4 CI result into it would
conflate a code audit with a release.

A note on the final SHA. Recording a CI run id inside the commit the run
tested is impossible, so the commit that adds this section is
documentation-only and its own CI run is reported to the operator rather than
written here. The diff between `f3ffc74` and this branch's head is exactly
this file.
The workflow triggers on this branch (`AA-18`, re-asserted for v4 by
`RA_HostedCITargetsThisBranch`) and its mutation-probe step asserts
`surviving_effective_safety_mutations == 0` and `inconclusive == 0` rather
than only a zero exit code.

---

## 7. What is still not true

* **No settlement authority is cryptographically authenticated.** The chain
  proves a settlement is consistent with the prediction it names, that its
  source is one an operator explicitly qualified, and that the prediction's
  retained evidence independently recomputes. It does **not** prove the
  settlement came from the exchange. `settlement_authority` remains an
  external blocker.
* **`flock` is advisory and per-host.** Two machines sharing one ledger are
  not made safe by anything here. See `docs/design/alpha-writer-model.md`.
* **No calibrated live series exists.** Nothing in this branch produces one,
  and no claim below depends on one.
* **Unqualified history is excluded from learning, not repaired.** Rows that
  cannot be tied to a market stay in `resolved()` as audit evidence and are
  counted as excluded. That is deliberate: repairing them would be the
  retroactive edit this subsystem exists to prevent.
* **RA-10 makes `prepared_without_acknowledgement` a busier list.** A spend
  refusal now records no prediction, so "announced but not committed" is the
  ordinary shape of a deferral as well as the shape of a crash that lost one.
  `reconcile_processed` therefore carries the processed status and detail with
  each entry, so the two are distinguishable rather than merging into one
  growing list of apparent losses.
* **RA-05 changed a runtime behaviour, deliberately, and it is worth
  stating.** `fsync_directory` now raises on every failure, including ones a
  platform might consider benign — an `EINVAL` from a filesystem that cannot
  fsync a directory at all. On such a filesystem a spool write and a
  first-append to a ledger would now REFUSE rather than silently report
  success. That is the required direction ("must never be reported as
  successful durable append") and it is the honest one: a name that cannot be
  persisted has not been persisted. It is called out here because it is a
  behaviour change on a path that used to be silent, and because nothing on
  this branch is deployed (`PRODUCTION_DEPLOYS = 0`), so the first place it
  would be observed is a deployment an operator chooses to make.
* **Settlement qualification recomputes a sha256 per resolved row, at read
  time.** That is on purpose — a stored flag is a claim, and RA-12 is about
  the difference — and it makes `resolved()` proportionally more expensive.
  For a shadow ledger of this size it is not measurable; for a much larger
  one it would want a per-row memo keyed on the row's own digest.
* **Not production-ready. Not CAPITAL-ready.** The only verdict this report
  supports is `SAFE_FOR_INDEPENDENT_REAUDIT`.
