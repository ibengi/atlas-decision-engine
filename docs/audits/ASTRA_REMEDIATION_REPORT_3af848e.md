# Astra remediation report

Machine-readable block first; the per-finding proof follows. Nothing in this
document authorizes CAPITAL, a deployment or a broker write.

```text
OLD_REJECTED_SHA=508899bce96ea3197690b296152880b88e555cc9
NEW_CANDIDATE_SHA=3af848e6aebc8769ea59f878d68674f89e04af0d
CAPITAL_ENABLED=NO
PROD_ACCESS_MODE=READ_ONLY
BROKER_WRITES=0

A01_STATUS=REMEDIATED
A02_STATUS=REMEDIATED
A03_STATUS=REMEDIATED_WITH_STATED_LIMITATION
A04_STATUS=REMEDIATED
A05_STATUS=REMEDIATED
A06_STATUS=REMEDIATED
A07_STATUS=REMEDIATED
A08_STATUS=REMEDIATED
A09_STATUS=REMEDIATED
A10_STATUS=REMEDIATED
A11_STATUS=REMEDIATED
A12_STATUS=REMEDIATED

A01_ROOT_CAUSE=The only record of durably observed history was journal_watermark INSIDE equity_ledger.json, so the evidence was exactly as rewindable as the state it protected; a .sha256 sidecar written beside the same bytes rewinds with them.
A01_FIX=continuity.py: append-only, hash-chained, strictly validated evidence log in a separate file, used as a FLOOR (journal, ledger and consumed tokens must all be at or above it); blocking recovery state that clamps reported equity to the lowest evidenced value and clears only on verified reconstruction; ledger fencing generation refusing stale writers; evidence appended BEFORE the state that depends on it.
A02_ROOT_CAUSE=KalshiClient.get_positions unwrapped with a silent [] default and ignored the cursor, so an unknown or renamed envelope, a null block, ambiguous envelopes and an unfollowed page-two position all read as a flat portfolio.
A02_FIX=get_positions_proof: explicit envelope allow-list, no `or []`, rejected null/conflicting envelopes and malformed rows, full cursor pagination with loop detection and a page cap that RAISES; PositionManager demands collection-complete evidence and cannot answer MATCH without it.
A03_ROOT_CAUSE=The authorization hash bound three files, never bound orders or pending intents, and nothing re-read anything at commit; two sequential broker GETs were treated as a transaction.
A03_FIX=bound_state() fingerprints every local file the preconditions read plus the ledger generation; the broker position read is bracketed by two order listings; apply_rebase re-runs the collection and re-verifies fingerprints immediately before a fenced commit. Broker-side atomic absence remains unprovable and is not claimed; see LIMITATION below.
A04_ROOT_CAUSE=Nothing enforced economic identity, so a replayed profitable row grew the count, kept the digest self-consistent and cut the drawdown from 30.769% to 7.692%.
A04_FIX=TradeLogger.event_keys defines identity (trade_id, settlement_id, correction_id); duplicate appends are refused; EquityLedger.duplicate_events detects any that arrive by other means; the watermark refuses to extend over them; conservative equity counts each identity once; GUARD_JOURNAL_INTEGRITY blocks CAPITAL. Corrections and reversals keep working through their own correction_id.
A05_ROOT_CAUSE=_strategy_mode tested mode == "strategy", so any other string silently selected the cash denominator, which a deposit repairs.
A05_FIX=Recognized enum ("strategy", "cash"); only "strategy" is CAPITAL-admissible; an unrecognized value keeps the loss-preserving computation AND raises GUARD_ACCOUNTING_MODE.
A06_ROOT_CAUSE=An unexplained adverse balance movement left the status RECONCILED with no accounting guard, so the global gates returned (True, None).
A06_FIX=GUARD_RESIDUAL_UNEXPLAINED from the FIRST observation beyond the rounding epsilon; OBSERVABLE, RECONCILED and CAPITAL_ADMISSIBLE separated; the rounding tolerance is never an economic-loss tolerance.
A07_ROOT_CAUSE=apply_seed compared the operator hash with proposal["sha256"], a field inside the mutable object it authenticated, and never re-derived the proposal from live state.
A07_FIX=The hash is recomputed from the proposal's own data; the proposal binds the source state root, settlement frontier, ledger generation, cash evidence, schema and continuity head; it is re-derived from live state immediately before applying and rejected on any drift.
A08_ROOT_CAUSE=`ran` was never read, NaN and future timestamps both read as fresh, absent fields were treated as defaults, and nothing bound the artifacts to the code or model they described.
A08_FIX=Strict schema, tests_run > 0, tests_passed == tests_run, finite timestamps bounded in both directions, model/test timestamp coherence, and two identity bindings (code_identity, model_validation_sha256) written by the process that ran the suite.
A09_ROOT_CAUSE=place_and_track discarded the return value of _record_intent, so a real filesystem error let the POST reach the broker adapter with nothing on disk able to ask whether the order exists.
A09_FIX=_record_intent returns a checked boolean and VERIFIES by re-reading the file; place_and_track aborts before transport with no retry and no silent continuation; pending_intents.json is a CRITICAL basename, so the failure also trips the persistence sentinel and the engine global gate.
A10_ROOT_CAUSE=apply_rebase mutated the authoritative in-memory state (HWM, consumed token, capital hold) and only then attempted the write, returning False with the mutation already visible.
A10_FIX=PREPARE on a copy, VALIDATE, RE-VALIDATE, COMMIT durably, PUBLISH; _commit restores the previous state on any failure; the single-use token is burned in the chain before the state that spends it, so a crash leaves the safe half of the pair.
A11_ROOT_CAUSE=An unclassified flow does not enter flows_cum, so the expected balance never moved and the same residual was recorded as a new economic event every k quiet cycles.
A11_FIX=accounted_flows_cum separates "already written down" from "counted as external"; _append_flow updates an open unclassified row (first/last seen, observation count, balance snapshot) instead of appending a second one.
A12_ROOT_CAUSE=EquityLedger.__init__ reconciles and SAVES, so building one to print a status rewrote the ledger and rotated its backups.
A12_FIX=EquityLedger.load_readonly, whose every durable write is a logged refusal, used by tools/equity_ledger_tool.py; tests compare file digests before and after each sub-command.

ORIGINAL_TESTS=1042
ASTRA_REGRESSIONS=135
NEW_ADVERSARIAL_TESTS=135
TOTAL_TESTS=1177
FAILURES=0
ERRORS=0
SKIPS=0
SUBTESTS=338

RACE_SUITE=PASS (14 cases: the 10 required interleavings, contract shape, quiescence, positive control, restart)
CRASH_CONSISTENCY=SPECIFIED_AND_TESTED (docs/design/continuity-and-crash-semantics.md; injection at temp write, fsync, atomic replace, checksum write, directory fsync, backup write, state-root update, journal commit, ledger commit, intent commit; restart after every point)
BROKER_POSITION_PAGINATION=PASS (full cursor, loop detection, page cap raises, strict envelope, completeness proof required for MATCH)
INTENT_PERSISTENCE_BEFORE_SUBMIT=PASS (transport tripwire: broker adapter call count 0 on every injected persistence failure)
TOKEN_ROLLBACK_PROTECTION=PASS (consumed tokens recorded in the append-only chain; replay refused after snapshot restore and after a failed commit)
MIGRATION_ATOMICITY=PASS (hash recomputed from the applied data, proposal re-derived from live state, idempotent, crash-safe)
READ_ONLY_TOOL_IMMUTABILITY=PASS (byte-for-byte digests unchanged, including the restored-journal case)

DOCKER_BUILD=NOT_RUN_LOCALLY (image registry blocked by this environment egress policy: production.cloudfront.docker.com -> 403. Dockerfile, requirements.txt and requirements-dev.txt are BYTE-IDENTICAL to 508899b; the build-stage guards were executed locally and pass. CI must run the real build.)
GATEKEEPER=CORRECTLY_REFUSES (NO_LIVE_PROMOTION=1, MODEL_APPROVED_FOR_LIVE absent, model_validation.json approved=false, 7 unmet criteria)
BROKER_WRITE_PROBE=PASS (10/10 mutating or unclassifiable verbs refused before transport; transport adapter calls 0)
STATIC_CHECKS=PASS (compileall clean; pyflakes clean on every file this change touches)
RESTART_RECOVERY_SIMULATION=PASS (tools/restart_harness.py 17/17)

SOURCE_TREE_HASH=c1da0ad0c8a2082030b2e265db08ff32d08252a6
CODE_IDENTITY=e78e05842717fecdbc993a58ac13bb792eb8054c06768b2a96956e2a18c79cb6
CI_TREE_HASH=NOT_YET_BUILT
IMAGE_DIGEST=NOT_YET_BUILT
DEPLOYED=NO
REMAINING_BLOCKERS=INDEPENDENT_ASTRA_REVIEW_OF_3af848e; CI_DOCKER_BUILD_AND_IMAGE_DIGEST; CANDIDATE_RUNTIME_ATTESTATION; INDEPENDENT_STRATEGY_VALIDATION; WHOLESALE_DATA_DIR_REWIND_NEEDS_AN_EXTERNAL_AUTHORITY

RESULT=REMEDIATION_COMPLETE_AWAITING_INDEPENDENT_REVIEW
```

`NEW_CANDIDATE_SHA` names the commit carrying the code and the regression
suite. This report is committed on top of it as a documentation-only
change, which is why it can quote its own subject: `git diff --stat
3af848e6aebc8769ea59f878d68674f89e04af0d..HEAD` touches this file and
nothing else, and `CODE_IDENTITY` — computed over the production modules
only — is identical at both commits. Review either; they are the same code.

## Before / after

`tools/astra_before_after_probe.py` runs the same twelve scenarios against
whichever tree it sits in, using only APIs present in both. It was executed
in a detached checkout of `508899b` and in this tree. No network, no
credentials, no broker: every scenario uses a synthetic in-process client
and a throwaway `DATA_DIR`.

| ID | `508899b` (BEFORE) | `3af848e` (AFTER) |
|---|---|---|
| A01 | UNSAFE — `capital_eligible=True drawdown_pct=0.0` | SAFE — `capital_eligible=False drawdown_pct=30.0` |
| A02 | UNSAFE — `fabricated_absences=['empty_envelope','null_block'] page_two_position_hidden=True` | SAFE — `fabricated_absences=[] page_two_position_hidden=False` |
| A03 | UNSAFE — `rebase_applied=True hwm 10.0 -> 7.0` | SAFE — `rebase_applied=False hwm 10.0 -> 10.0` |
| A04 | UNSAFE — `drawdown_pct 30.769 -> 7.692 capital_eligible=True` | SAFE — `drawdown_pct 30.769 -> 30.769 capital_eligible=False` |
| A05 | UNSAFE — `unknown_mode_drawdown_pct=3.0 capital_eligible=True` | SAFE — `unknown_mode_drawdown_pct=30.0 capital_eligible=False` |
| A06 | UNSAFE — `capital_eligible=True guards=[]` | SAFE — `capital_eligible=False guards=['cash_residual_unexplained']` |
| A07 | UNSAFE — `modified_proposal_applied=True (hwm=5.0) stale_proposal_applied=True (drawdown_pct=0.0)` | SAFE — both `False` |
| A08 | UNSAFE — `accepted_invalid_evidence=['zero_tests','nan_timestamp','future_timestamp']` | SAFE — `accepted_invalid_evidence=[]` |
| A09 | UNSAFE — `broker_calls_after_failed_intent_persistence=1` | SAFE — `0`, `result=blocked:intent_unwritable` |
| A10 | UNSAFE — `applied=False hwm 10.0 -> 7.0 hold=True` | SAFE — `applied=False hwm 10.0 -> 10.0 hold=False` |
| A11 | UNSAFE — `unclassified_flows_for_one_movement=3` | SAFE — `1` |
| A12 | UNSAFE — `files_modified_by_status=['equity_ledger.json','equity_ledger.json.bak3','equity_ledger.json.sha256']` | SAFE — `[]` |

Exit code: 1 on `508899b`, 0 here.

## Per-finding proof

Every regression family exercises the production classes (`TradeLogger`,
`PositionManager`, `OrderManager`, `EquityLedger`, `RiskManager`,
`KalshiClient`, `ExecutionEngine`) on an isolated `DATA_DIR`, and every
family carries at least one **positive control**: a suite where nothing can
ever pass proves nothing.

| ID | Regression file | Cases | Production path exercised | State mutation asserted | Broker transport | Survives restart |
|---|---|---|---|---|---|---|
| A01 | `tests/test_astra_a01_continuity.py` | 22 | `_load`, `_check_continuity`, `save`, `_commit`, `token_consumed`, `guards`, `ExecutionEngine._evaluate_global_guards` | HWM never lowered; conservative equity clamped to the evidenced floor; drawdown stays at or above 30% | not reached | yes — the block is re-asserted on every load and clears only on verified reconstruction |
| A02 | `tests/test_astra_a02_positions.py` | 13 (+16 subtests) | `KalshiClient.get_positions_proof`, `PositionManager._collect_broker_positions` / `verify_against_broker` / `reconcile_with_broker`, `equity_rebase_context` | HWM unchanged, no `capital_hold`, token unconsumed | synthetic `_req` only, no socket | n/a (per call) |
| A03 | `tests/test_astra_a03_rebase_races.py` | 14 | `equity_rebase_context`, `bound_state`, `rebase_preconditions`, `apply_rebase`, `_commit`, fenced `JsonStore.save` | HWM and `capital_hold` unchanged in memory **and** on disk | synthetic client | yes — the positive control re-reads its result after a reload |
| A04 | `tests/test_astra_a04_a08_accounting.py` | 8 | `TradeLogger._reject_duplicate`, `duplicate_events`, `_advance_journal_watermark`, `settled_unique`, `guards` | drawdown never improves; watermark refuses to extend | not reached | yes |
| A05 | same file | 5 | `RiskManager._strategy_mode` / `rolling_drawdown_pct`, `accounting_mode*`, `guards` | drawdown 30% in every mode; CAPITAL blocked outside "strategy" | not reached | n/a |
| A06 | same file | 5 | `observe`, `_unexplained_residual`, `guards`, `rebase_preconditions` | CAPITAL blocked while the residual is open | not reached | yes |
| A07 | same file | 7 | `propose_seed`, `seed_proposal_sha`, `apply_seed` | seed refused, ledger stays unseeded, HWM unchanged | not reached | yes — idempotence re-checked after a reload |
| A08 | same file | 15 (+7 subtests) | `model_gatekeeper.check_live_allowed`, `code_identity`, `file_sha256` | none (pure evaluation) | not reached | n/a |
| A09 | `tests/test_astra_a09_a12_execution.py` | 10 | `OrderManager._record_intent` / `_verify_intent_durable` / `place_and_track`, `PersistenceSentinel` | no in-memory intent survives a failed write | **tripwire: adapter call count 0** on every failure; 1 in the positive control, with the intent already on disk at that instant | yes — the intent is reloaded by a second `OrderManager` |
| A10 | same file | 5 | `apply_rebase`, `_commit`, `_record_consumed_token` | HWM, hold, tokens and rebase list unchanged; ledger bytes identical | not reached | yes — a failed commit is a non-event after a reload |
| A11 | same file | 5 | `observe`, `_append_flow`, `_open_unclassified_like`, `accounted_flows_cum`, `classify_flow` | exactly one flow row per movement; conservative equity not deepened | not reached | yes |
| A12 | same file | 6 (+2 subtests) | `EquityLedger.load_readonly`, `tools/equity_ledger_tool.py` in a subprocess | sha256 of every file under `DATA_DIR` unchanged | not reached | n/a |
| persistence | `tests/test_astra_persistence_injection.py` | 19 (+10 subtests) | `JsonStore.save/load`, `ContinuityChain.append`, `_record_intent`, `state_restore` | no injected failure ever reports a smaller loss than was evidenced | tripwire 0 | restart after every interruption point |

## Limitations, stated rather than resolved

1. **A01 — wholesale rewind.** A restore that also removes or rewinds
   `equity_continuity.log` cannot be detected from inside the process. No
   purely local artefact can outrank a wholesale rewind of the disk it
   lives on. Every rollback that leaves the chain in place fails closed,
   and `state_restore` refuses to write economic state onto a volume whose
   chain already evidences history the restore does not carry. Closing the
   remaining case needs an authority outside this filesystem.
2. **A03 — broker atomicity.** The exchange offers no primitive that proves
   "I hold nothing" atomically, and two GETs never become one transaction.
   The position read is bracketed by two order listings and any
   disagreement refuses, which narrows the window to zero observed orders
   on both sides of the read. That is not atomicity and is not presented as
   such. The mandatory post-rebase `capital_hold`, which no rebase can
   clear by itself, remains the second line.
3. **A10 — token burn ordering.** A crash between burning the token and
   committing the state leaves a token that can never be replayed and a
   rebase that did not happen; the operator issues a new action id. The
   alternative ordering leaves a token that survives its own consumption.
4. **Docker.** The image was not built here: this environment's egress
   policy denies the image registry. The Dockerfile and both requirements
   files are byte-identical to `508899b`, and the build-stage guards were
   run locally and pass. The image digest must come from CI.
5. **Runtime attestation and strategy validation** are unchanged from the
   audit's findings: neither is claimed, and both remain blockers.

## Exit

`REMEDIATION_COMPLETE_AWAITING_INDEPENDENT_REVIEW`

CAPITAL is not ready and is not claimed to be. A fresh independent Astra
review is required on `3af848e6aebc8769ea59f878d68674f89e04af0d`; the
rejected `508899b` must not be re-reviewed, and this candidate must not be
deployed before that review.
