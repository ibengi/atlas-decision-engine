# Model-reconstruction protocol authority

**MR-20260926-1 is the sole active authority for new KXBTC15M model reconstruction.** This code change closes the protocol-governance conflict in the remediation candidate. It does not change the deployed release or claim runtime rollout.

| Artifact | Authority for MR |
|---|---|
| `atlas_v2/CHALLENGER_REGISTRY.json` / MR-20260926-1 | ACTIVE; sole authority |
| `TRAINING_PROTOCOL.json` / PHASE2-20260926-1 | SUPERSEDED_FOR_MODEL_RECONSTRUCTION; historical evidence only |
| `atlas_v2/PROTOCOL_AUTHORITY.json` | Release-pinned scope/authority catalog |

The historical PHASE2 payload and its canonical hash `751652ff4e2f5a9e9aa223952a655a34cc598dcc8d179f564d597c18002f41df` are preserved. Supersession metadata is outside that payload. No old observations, labels, locks, outcomes or ledgers are rewritten or deleted.

PHASE2 **MUST NOT** contribute TRAIN, CALIBRATION, VALIDATION or OOS rows to MR. It **MUST NOT** fit, calibrate, select, lock or promote any MR candidate. Its old calculations are preserved for historical reproduction, are labeled superseded/unadmitted, and reject MR-tagged inputs. Public legacy fit and OOS entry points also require active authority and are now refused, even for otherwise valid legacy inputs. The PHASE2 coordinator's automatic fitting, challenger capture, locking and OOS lifecycle are disabled under the new authority. Historical arithmetic and lifecycle regression tests explicitly emulate the retired authority inside synthetic test fixtures; production has no such bypass.

## Fail-closed enforcement

`protocol_authority.authority()` verifies the release-pinned catalog, the historical wrapper/payload binding and the unchanged MR registry hash. Exactly one active KXBTC15M authority is required. Two ACTIVE claims with intersecting market scopes and overlapping decision intervals raise exactly `PROTOCOL_AUTHORITY_CONFLICT`. Half-open adjacent intervals do not overlap. An unbounded end covers prospective OOS and prevents a second authority silently claiming later dates.

The startup check runs before release initialization, stores, credentials, network or probes. MR fitting, validation, prediction and settlement-link entry points also check authority. Reactivating the historical JSON wrapper cannot override the catalog. Hash mismatches fail closed rather than enrolling an edited protocol.

## Frozen dates and families

| Stage | UTC start inclusive | UTC end exclusive |
|---|---|---|
| TRAIN | 2026-09-27T00:00:00Z | 2026-10-11T00:00:00Z |
| CALIBRATION | 2026-10-11T00:00:00Z | 2026-10-18T00:00:00Z |
| VALIDATION | 2026-10-18T00:00:00Z | 2026-11-01T00:00:00Z |
| FUTURE_OOS | Next UTC midnight strictly after immutable lock | 28 complete UTC days after that midnight |

No stage overlap is allowed. Lock cannot precede the validation end. Even a lock at exactly midnight starts OOS the following midnight. There is no current MR lock and no OOS start timestamp.

Exactly two active families remain: `MR-STRUCTURAL-1` and `MR-REGIME-1`. Bonferroni remains over those two families, with the existing 97.5% block intervals. Deferred families are not added to the active search. No formula, grid, cohort, risk limit, date or acceptance threshold changes.

Unchanged MR canonical protocol hash:
`ae644e7fa113b7d5177626d43408cd6ac4f6f3913d5a679a076ec1793a534074`.

## Decision custody and contamination

Every new MR prediction persists `protocol_id=MR-20260926-1`, `protocol_hash`, `candidate_family`, `feature_schema`, and the stage derived from its decision timestamp. The record also binds the original source protocol, consumption history and an immutable lock hash when applicable. Model and validation artifacts identify the protocol/family too.

MR row admission requires exact expected stage/family/protocol bindings, original MR source and empty prior-candidate-use history. It reconstructs native features and requires the original append-only `MR_PREDICTION` record, recorded before close, with identical lineage and features. A PHASE2 `L_DECISION` cannot be relabeled into that record. Known PHASE2 event/market exposure in the supplied ledger is conservatively rejected. Current receipt reconstruction and five-second prospective-write freshness prevent retrospective relabeling of old PHASE2 decisions.

Externally supplied ledger exports still need complete, authentic custody/anchor evidence. Omitting another store's consumption history does not establish pristine data. The code does not invent proof of absence across undisclosed ledgers; qualification must reject incomplete custody. No existing PHASE2 export is authorized for MR admission by this change.

## Missing-cost PnL

The historical `tools/shadow_pnl.py` audit path now returns unknown PnL if fee or slippage is missing, malformed, nonfinite, negative or boolean. Missing rows are retained in counts; they are not dropped to improve totals. Daily and aggregate PnL stay unavailable for incomplete cohorts, and the verdict is `PROFITABILITY_UNAVAILABLE_MISSING_COSTS`. Explicit recorded zero is distinguishable from a missing value, but legacy cost estimates never establish qualified profitability. Positive estimated replay is labeled unqualified, not PASS or approval. Forecast and selected-trade replay use the same chronological full-row partition boundaries.

The strict MR diagnostic already rejects unqualified slippage/cost coverage and continues to do so. Neither tool can promote a model.

## Verification and operational limits

Permanent tests cover active-authority overlap, startup ordering, attempted historical reactivation, unchanged historical/MR hashes, MR/PHASE2 row exclusion, native-record binding, consumption, exact date boundaries, stage disjointness, strict next-midnight 28-day OOS, exactly two-family multiplicity and missing-cost profitability. Behavioral mutations remove each main guard and must fail assertions; import errors, crashes and timeouts do not count as killed safeguards.

The evidence package records exact candidate SHA/tree, test/mutation results and exact-SHA hosted CI. PR80 remains draft/unmerged. No deployment, merge, financial authority, model approval, historical relabeling, ledger rewrite or model promotion occurs. The currently deployed learning SHA remains outside this change; runtime enforcement would require a separate authorized rollout, not an implicit migration.

CAPITAL=OFF; BROKER_WRITES=0; REAL_ORDERS_SUBMITTED=0; PROD_ACCESS_MODE=READ_ONLY.
