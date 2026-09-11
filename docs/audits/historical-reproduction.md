# Historical BEFORE / AFTER reproduction

The 59 previously reported unsafe cases are executable from committed source.
The missing link was the original 190-case fixture and harness: the prior branch
kept their remediated versions but omitted the exact baseline versions. Those
original files are now preserved, byte for byte, under
`tools/astra_regressions/baseline_3af848e/`.
The frozen `fixture.py` retains its original final blank line; its pinned source
hash takes precedence over normalizing that historical whitespace.

The replay executes 304 scenarios on each selected tree: 108 original Astra
scenarios, 190 reliability scenarios, two global controls, and four additional
interactions. It selects the same 59 named safety assertions from those results.
The seven formerly inconclusive cases remain separately identified and are not
counted as reproduced unsafe cases.

## Reproduce locally

Both Git objects and the test dependencies must already be available locally.
The runner never fetches code, downloads packages, contacts brokers, or enables
CAPITAL. It launches the supplied repository classes with temporary economic
state, in-memory broker fixtures, a credential-free environment, and socket
denial in the main interpreter and inherited subprocesses.

```sh
git worktree add --detach /tmp/atlas-3af-review 3af848e6aebc8769ea59f878d68674f89e04af0d
python tools/astra_regressions/reproduce_historical.py \
  --before /tmp/atlas-3af-review --after . \
  --output /tmp/atlas-historical-reproduction
```

Set `ATLAS_AUDIT_DEPENDENCIES` to an existing local site-packages directory if
the selected Python cannot otherwise import the repository test dependencies.
No particular scratch directory or installation path is embedded in the runner.
Use `--phase before` or `--phase after` to run one side separately. Both reviewed
trees must be clean; `--allow-dirty-after` exists only for development and explicitly
marks the evidence as uncommitted. Final evidence must omit that switch.

The output contains fresh per-suite logs and JSON reports, the source commit and
tree, Python source hashes, harness hashes, and a row for each of the 59 claims.
It verifies that imported production modules belong to the requested tree and
that the source did not change during execution. Baseline harness source hashes
are pinned in `historical_cases.json`. A prior result file is removed before its
suite runs, so interrupted execution cannot reuse stale evidence.

A BEFORE reproduction passes only if the named case actually fails its original
safety assertion, with the same assertion text. A fixture exception, setup failure,
missing case, duplicate identifier, incomplete collection, or source change does
not demonstrate the old defect. An AFTER phase requires all 304 scenarios to pass
and every suite to exit successfully. Baseline subprocess exit codes of 1 or 2
are expected because they report reproduced defects and the seven separately
recorded uncertainties; the comparison runner returns success only after checking
the actual results.

## Fixture and assertion provenance

| Suite | Preserved BEFORE source | AFTER source | Scenarios | Unsafe claims |
|---|---|---|---:|---:|
| original108 | `original108_contract_adapted.py` | `original108_remediated.py` | 108 | 9 |
| reliability190 | `baseline_3af848e/reliability_review.py` | `reliability_review.py` | 190 | 44 |
| global2 | `baseline_3af848e/confirm_global_guards.py` | `confirm_global_guards.py` | 2 | 2 |
| interactions4 | `before_after_interactions.py` | `before_after_interactions.py` | 4 | 4 |

Paths in that table are relative to `tools/astra_regressions/`. The baseline
reliability and global-control scripts import their adjacent original `fixture.py`;
they cannot accidentally import the remediated fixture. The 108-case baseline
script has its own fixture. The four interaction cases run identical source on
both trees. No production method is changed to manufacture a baseline result.

The AFTER fixtures declare the newer identity and continuity contracts, recover
authority through the explicit recovery interface, and respect detached journal
rows and transaction ownership. Filesystem fault injection still changes actual
temporary files. Where a mutation is now refused before an old observation point,
the AFTER assertion checks the conservative outcome and retained authority.
These are contract adaptations; the baseline source remains available for direct
comparison. The runtime report contains the exact AFTER script hashes used.
The four crash workers explicitly configure the same test-host public-key pins
in their new process before constructing managers. Their independent provider
retains its existing checkpoint; it is never rebuilt from local economic state.
This adaptation exercises the intended crash boundary after the new process-bound
trust guard, while preserving every restart assertion.

## Exact claim mapping

Each identifier below exists in both its preserved BEFORE suite and its AFTER
suite. The required transition is **original assertion FAIL → safety assertion
PASS**. Identifiers are scoped by suite to prevent name collisions.

| # | Suite | Exact case identifier |
|---:|---|---|
| 1 | original108 | `J_restore_both_journal_and_ledger` |
| 2 | original108 | `J_restored_pair_plus_deposit_passes_global_guards` |
| 3 | original108 | `CRASH_after_journal_before_watermark_then_restore` |
| 4 | original108 | `R_none_listing` |
| 5 | original108 | `R_missing_local_file_orders_state.json` |
| 6 | original108 | `R_missing_local_file_positions_state.json` |
| 7 | original108 | `R_missing_local_file_pending_intents.json` |
| 8 | original108 | `R_malformed_pending_intent_disappears_on_restart` |
| 9 | original108 | `R_unknown_local_position_state` |
| 10 | reliability190 | `C_all_old` |
| 11 | reliability190 | `C_chain_corrupt_suffix` |
| 12 | reliability190 | `P_duplicate_conflicting_ticker` |
| 13 | reliability190 | `P_cross_page_conflicting_ticker` |
| 14 | reliability190 | `P_bool_quantity` |
| 15 | reliability190 | `R_continuity_after_context` |
| 16 | reliability190 | `R_local_after_final_validation` |
| 17 | reliability190 | `R_broker_after_final_query` |
| 18 | reliability190 | `R_settlement_during_prepare` |
| 19 | reliability190 | `E_same_order_new_trade` |
| 20 | reliability190 | `E_same_order_new_trade_restart` |
| 21 | reliability190 | `E_production_duplicate_order` |
| 22 | reliability190 | `CASH_not_quiet` |
| 23 | reliability190 | `CASH_settlement_change` |
| 24 | reliability190 | `CASH_grown_tolerance` |
| 25 | reliability190 | `M_late_settlement` |
| 26 | reliability190 | `M_failed_save` |
| 27 | reliability190 | `G_raw_failure` |
| 28 | reliability190 | `G_count_collection_mismatch` |
| 29 | reliability190 | `G_duplicate_model_key` |
| 30 | reliability190 | `G_duplicate_test_key` |
| 31 | reliability190 | `I_changed_fields` |
| 32 | reliability190 | `I_after_readback` |
| 33 | reliability190 | `F_two_equal` |
| 34 | reliability190 | `N13_two_process_generation_check_then_write` |
| 35 | reliability190 | `N14_prepared_state_visible_during_commit` |
| 36 | reliability190 | `N15_failed_hold_release` |
| 37 | reliability190 | `N15_failed_attestation` |
| 38 | reliability190 | `N15_failed_hold_release_stale_generation` |
| 39 | reliability190 | `N16_cash_nan` |
| 40 | reliability190 | `N16_cash_pos_inf` |
| 41 | reliability190 | `N16_cash_neg_inf` |
| 42 | reliability190 | `N16_persisted_pending_nan` |
| 43 | reliability190 | `N16_hwm_nan` |
| 44 | reliability190 | `N16_hwm_inf` |
| 45 | reliability190 | `N16_unrecognized_flow` |
| 46 | reliability190 | `N16_unknown_position_state` |
| 47 | reliability190 | `N17_restart_intent_missing_id` |
| 48 | reliability190 | `N17_restart_intent_corrupt` |
| 49 | reliability190 | `N17_restart_intent_missing` |
| 50 | reliability190 | `N18_short_append_success` |
| 51 | reliability190 | `N18_directory_fsync_failure` |
| 52 | reliability190 | `N19_stale_reader_does_not_refresh_continuity` |
| 53 | reliability190 | `CRASH_journal_data_rename` |
| 54 | global2 | `CONFIRM_nan_balance_global_gates` |
| 55 | global2 | `CONFIRM_stale_hold_release_global_gates` |
| 56 | interactions4 | `incomplete_settlement` |
| 57 | interactions4 | `unknown_journal` |
| 58 | interactions4 | `subcent_settlement` |
| 59 | interactions4 | `concurrent_risk_claims` |

## Seven original uncertainties

These rows were not part of the 59 proven failures. Their historical status is
retained; a final AFTER replay still requires their current assertions to pass.

| Suite | Identifier | Historical status |
|---|---|---|
| original108 | `J_digest` | ERROR |
| original108 | `R_malformed` | ERROR |
| reliability190 | `CASH_quiet_positive` | NEEDS_REVIEW |
| reliability190 | `G_unknown_model_version` | NEEDS_REVIEW |
| reliability190 | `G_missing_criteria` | NEEDS_REVIEW |
| reliability190 | `G_malformed_criteria` | NEEDS_REVIEW |
| reliability190 | `N20_environment_state_binding` | NEEDS_REVIEW |

## Interpretation limits

The replay demonstrates local safety properties using deterministic data and
synchronized process/crash boundaries. It does not certify filesystem guarantees
on an untested deployment, independent authority infrastructure, broker-side
atomicity, or live profitability. The in-memory transport adapters can observe
synthetic method calls; no request leaves the process. Real broker writes remain
zero and CAPITAL remains OFF.
