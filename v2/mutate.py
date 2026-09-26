"""Each deliberate money-path defect must cause an assertion failure.

Mutations run in temporary source copies, never in a deployed service or ledger.
Syntax/import errors and timeouts are NOT accepted as killed invariants.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
MUTANTS = [
    ('authority_direct_fit_retired', 'training_protocol.py', 'def train_family(family, rows, at):\n    reject_mr_rows(rows)\n    governance.require_active(governance.PHASE2)', 'def train_family(family, rows, at):\n    reject_mr_rows(rows)', 'test_protocol_authority.AuthorityTests.test_direct_legacy_fit_and_oos_are_retired'),
    ('authority_overlap', 'protocol_authority.py', "if not set(a['markets']) & set(b['markets']): continue", 'if True: continue', 'test_protocol_authority.AuthorityTests.test_active_overlap_rejected_and_disjoint_allowed'),
    ('authority_startup', 'service.py', 'authority()  # before stores, network, probes or research startup', 'pass  # deliberate missing startup authority check', 'test_protocol_authority.AuthorityTests.test_startup_checks_authority_before_any_state_or_network'),
    ('authority_legacy_mr_rows', 'protocol_authority.py', 'for row in rows:', 'for row in []:', 'test_protocol_authority.AuthorityTests.test_superseded_protocol_cannot_consume_mr_rows'),
    ('authority_protocol_hash', 'protocol_authority.py', 'digest(plan)!=MR_HASH', 'False', 'test_protocol_authority.AuthorityTests.test_protocol_hash_mismatch_rejected'),
    ('authority_row_binding', 'protocol_authority.py', 'any(row.get(k)!=v for k,v in expected.items())', 'False', 'test_protocol_authority.MRDecisionAuthorityTests.test_decision_binding_and_row_protocol_checks'),
    ('authority_consumed_rows', 'protocol_authority.py', "row.get('source_protocol_id')!=MR or row.get('prior_candidate_uses')!=[]", 'False', 'test_protocol_authority.MRDecisionAuthorityTests.test_decision_binding_and_row_protocol_checks'),
    ('authority_stage_boundary', 'protocol_authority.py', 'if utc(start)<=point<utc(end): return stage', 'if utc(start)<=point<=utc(end): return stage', 'test_protocol_authority.AuthorityTests.test_exact_dates_no_overlap_and_boundary_ownership'),
    ('authority_oos_length', 'protocol_authority.py', 'start+timedelta(days=28)', 'start+timedelta(days=7)', 'test_protocol_authority.AuthorityTests.test_oos_next_midnight_strictly_after_lock_and_28_days'),
    ('authority_two_families', 'protocol_authority.py', "len(plan['candidates'])!=2 or {c['family']:c['id'] for c in plan['candidates']}!=FAMILIES", 'False', 'test_protocol_authority.AuthorityTests.test_only_two_family_multiplicity'),
    ('authority_retired_lifecycle', 'learning_phase2.py', 'return  # historical evidence retained; no new fitting, locks or OOS', 'pass  # deliberate reactivation', 'test_phase2.NativeCoordinatorTests.test_superseded_lifecycle_never_fits_or_locks'),
    ('shadow_pnl_missing_cost', '../tools/shadow_pnl.py', 'if value is None or isinstance(value,bool): return None', 'if value is None or isinstance(value,bool): value=0', 'test_protocol_authority.AuthorityTests.test_missing_fee_or_slippage_never_zero'),

    ('mr_model_hash', 'reconstruction.py', 'digest(artifact)!=model["model_artifact_sha256"]', 'False', 'test_reconstruction.ReconstructionTests.test_model_hash_and_no_approval_gate'),
    ('mr_no_approval', 'reconstruction.py', 'artifact["approved"] is not False', 'False', 'test_reconstruction.ReconstructionTests.test_model_hash_and_no_approval_gate'),
    ('mr_native_recompute', 'reconstruction.py', 'canonical(rebuilt)!=canonical(features)', 'False', 'test_reconstruction.ReconstructionTests.test_feature_receipts_cannot_be_replaced'),
    ('mr_drawdown_units', 'model_diagnosis.py', '(peak-equity)/peak*100', '(peak-equity)*100', 'test_reconstruction.ReconstructionTests.test_percentage_drawdown_is_not_dollars'),
    ('mr_missing_cost_zero', 'model_diagnosis.py', 'except Refused: fee=None', 'except Refused: fee=0', 'test_reconstruction.DiagnosisTests.test_missing_cost_is_not_zero_profit'),
    ('mr_fee_double_count', 'reconstruction.py', 'return count*outcome-decimal(economics["cost_bound"])', 'return count*outcome-decimal(economics["cost_bound"])-decimal(economics["fee_bound"])', 'test_reconstruction.ReconstructionTests.test_costs_recomputed_at_refresh_and_settlement_charged_once'),

    ('scope_artifact_trust', 'sports_scope_evidence.py', 'manifest_hash in REVIEWED_EVIDENCE_SHA256', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_pinned_manifest_and_no_self_authorization'),
    ('scope_artifact_key', 'sports_scope_evidence.py', 'hmac.compare_digest(body["key_id_sha256"], key_fingerprint(key_id))', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_installed_key_binding'),
    ('scope_artifact_source', 'sports_scope_evidence.py', 'body["provider"] == "Kalshi" and body["source_url"] in SOURCES', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_provider_and_hashes'),
    ('scope_artifact_permissions', 'sports_scope_evidence.py', 'body["write_allowed"] is False', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_permissions'),
    ('scope_artifact_trade', 'sports_scope_evidence.py', 'body["trade_allowed"] is False', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_permissions'),
    ('scope_artifact_transfer', 'sports_scope_evidence.py', 'body["transfer_allowed"] is False', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_permissions'),
    ('scope_artifact_expiry', 'sports_scope_evidence.py', 'observed <= current < expires', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_expiry_future_and_utc'),
    ('scope_artifact_validity', 'sports_scope_evidence.py', '0 < (expires-observed).total_seconds() <= MAX_VALIDITY_SECONDS', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_expiry_future_and_utc'),
    ('scope_artifact_hash', 'sports_scope_evidence.py', 'isinstance(body[field], str) and re.fullmatch(r"[0-9a-f]{64}", body[field])', 'True', 'test_sports_scope_evidence.ScopeEvidenceTests.test_provider_and_hashes'),
    ('scope_artifact_api_denial', 'sports_probe.py', 'require(record["scopes"] == ["read"], "SCOPE_EVIDENCE_CONFLICT")', 'return {"scopes": ["read"]}', 'test_sports_scope_evidence.ScopeEvidenceTests.test_api_conflicts_cannot_be_overridden'),
    ('scope_artifact_unavailable_only', 'sports_probe.py', 'if str(exc) not in {"REST_HTTP_403", "REST_HTTP_404", "REST_CONNECTION_FAILED"}:', 'if False:', 'test_sports_scope_evidence.ScopeEvidenceTests.test_fallback_only_when_unavailable'),
    ('sports_read_scope', 'sports_probe.py', 'matches[0].get("scopes") == ["read"]', 'True', 'test_sports_probe.SportsProbeTests.test_scope_requires_exact_matching_read_only_record'),
    ('sports_signer_path', 'sports_probe.py', 'path in {"/trade-api/v2/api_keys", "/trade-api/ws/v2"}', 'True', 'test_sports_probe.SportsProbeTests.test_signature_binding_and_mutation_paths_refused'),
    ('sports_secret_output', 'sports_probe.py', 'not any(s and s in encoded for s in self.secrets)', 'True', 'test_sports_probe.SportsProbeTests.test_secrets_never_persist_even_provider_echo'),
    ('sports_quote_age', 'sports_probe.py', 'uncertainty <= delta and delta+uncertainty <= AGE_MS', 'True', 'test_sports_probe.SportsProbeTests.test_quote_stale_future_clock_incomplete_and_close'),
    ('sports_clock_uncertainty', 'sports_probe.py', '0 <= uncertainty <= CLOCK_MS', 'True', 'test_sports_probe.SportsProbeTests.test_quote_stale_future_clock_incomplete_and_close'),
    ('sports_snapshot_complete', 'sports_probe.py', 'set(quotes) == set(members) and bool(members)', 'bool(members)', 'test_sports_probe.SportsProbeTests.test_snapshot_missing_skew_stale'),
    ('sports_snapshot_source_skew', 'sports_probe.py', 'max(native)-min(native)+2*uncertainty <= SYNC_MS', 'True', 'test_sports_probe.SportsProbeTests.test_snapshot_missing_skew_stale'),
    ('sports_snapshot_receive_skew', 'sports_probe.py', 'Decimal(str((max(received)-min(received)).total_seconds()))*1000+2*uncertainty <= SYNC_MS', 'True', 'test_sports_probe.SportsProbeTests.test_snapshot_missing_skew_stale'),
    ('sports_sequence_gap', 'sports_probe.py', 'type(sequence) is int and (self.seq is None or sequence == self.seq+1)', 'type(sequence) is int', 'test_sports_probe.SportsProbeTests.test_book_sequence_delta_baseline_and_unknown_messages'),
    ('sports_ack_required', 'sports_probe.py', 'channel in self.acks and body.get("sid") == self.acks[channel]', 'True', 'test_sports_probe.SportsProbeTests.test_ack_and_reconnect_clear_quotes_and_books'),
    ("phase2_daily_minimum", "training_protocol.py", 'counts[(utc(start)+timedelta(days=i)).date().isoformat()] < 30', 'counts[(utc(start)+timedelta(days=i)).date().isoformat()] < 0', "test_phase2.ProtocolTests.test_minimum_is_per_day_not_total_or_copies"),
    ("phase2_late_oos_prediction", "training_protocol.py", 'utc(r["prediction_recorded_at"]) < utc(r["close_at"])', 'utc(r["prediction_recorded_at"]) <= utc(r["close_at"])', "test_phase2.ProtocolTests.test_oos_must_be_future_and_prospectively_recorded"),
    ("phase2_frozen_source", "learning_phase2.py", 'freeze["payload"]["source_sha"] != observer.source_sha', 'False', "test_phase2.NativeCoordinatorTests.test_protocol_rejects_late_initialization_and_changed_source"),
    ("phase2_native_quote_binding", "learning_phase2.py", 'or matches != [p]', '', "test_phase2.NativeCoordinatorTests.test_raw_transport_cannot_be_replaced_by_normalized_quote"),
    ("learning_label_conflict", "learning.py", 'if (old["outcome"], old["settlement_at"]) != (label["outcome"], label["settlement_at"]):', 'if False:', "test_learning.LearningTests.test_conflict_permanently_invalidates_even_if_later_label_reverts"),
    ("learning_disqualification", "learning.py", 'if candidate in self.disqualified: reasons.append("CANDIDATE_DISQUALIFIED")', 'if False: reasons.append("CANDIDATE_DISQUALIFIED")', "test_learning.LearningTests.test_guard_rejection_is_not_bypass_and_bypass_survives_restart"),
    ("learning_write_flag", "service.py", 'if os.environ.get(name, "0") != "0":', 'if False:', "test_learning_service.LearningStartupTests.test_learning_requires_readonly_zero_writes_and_qualification"),
    ("learning_requires_sources", "service.py", 'if mode == "LIVE_MARKET_LEARNING" and os.environ.get("ATLAS_V2_QUALIFICATION_ON_START") != "1":', 'if False:', "test_learning_service.LearningStartupTests.test_learning_requires_readonly_zero_writes_and_qualification"),
    ("qualification_final_status", "qualification.py", 'm["status"] != "finalized"', 'False', "test_qualification.QualificationTests.test_final_settlement_requires_status_scope_time_payout_and_no_provisional"),
    ("qualification_provisional", "qualification.py", 'm.get("is_provisional", False) is not False', 'False', "test_qualification.QualificationTests.test_final_settlement_requires_status_scope_time_payout_and_no_provisional"),
    ("qualification_settlement_payout", "qualification.py", 'if decimal(m["settlement_value_dollars"]) != outcome:', 'if False:', "test_qualification.QualificationTests.test_final_settlement_requires_status_scope_time_payout_and_no_provisional"),
    ("qualification_raw_hash", "qualification.py", 'hashlib.sha256(raw).hexdigest() != p["body_sha256"]', 'False', "test_qualification.QualificationTests.test_receipt_transport_hash_and_scope_are_mandatory"),
    ("qualification_transport", "qualification.py", 'p["transport_complete"] is not True', 'False', "test_qualification.QualificationTests.test_receipt_transport_hash_and_scope_are_mandatory"),
    ("qualification_reference_time", "qualification.py", 'not 0 <= (received-at).total_seconds() <= 5', 'False', "test_qualification.QualificationTests.test_reference_native_time_and_exact_sixty_seconds_are_not_manufactured"),
    ("qualification_candle_closed", "qualification.py", 'end_at > utc(receipt["payload"]["started_at"])', 'False', "test_qualification.QualificationTests.test_candles_complete_closed_consecutive_precise"),
    ("qualification_refresh_time", "qualification.py", 'not 0 <= (utc(at)-utc(observation["observed_at"])).total_seconds() <= 5', 'False', "test_qualification.QualificationTests.test_refresh_requires_fresh_after_decision_quotes_and_positive_slippage"),
    ("qualification_ladder_terminal", "qualification.py", 'if (not cursor) != (i==len(receipts)-1):', 'if False:', "test_qualification.QualificationTests.test_ladder_rejects_partial_stale_incomparable_or_wrong_center"),
    ("qualification_ladder_direction", "qualification.py", 'm["strike_type"] != "greater"', 'False', "test_qualification.QualificationTests.test_ladder_rejects_partial_stale_incomparable_or_wrong_center"),
    ("qualification_zero_slippage", "qualification.py", 'slip = max(steps) + max(decimal(0), ask-decimal(observation["ask"]))', 'slip = decimal(0)', "test_qualification.QualificationTests.test_refresh_requires_fresh_after_decision_quotes_and_positive_slippage"),
    ("qualification_fee_autoapproval", "qualification.py", '"fee_qualified":False', '"fee_qualified":True', "test_qualification.QualificationTests.test_fee_is_nonzero_and_does_not_self_qualify"),
    ("alpha_external_probability", "alpha_lab.py", 'if "candidate_probability" in row or "period" in row:', 'if False:', "test_alpha_lab.AlphaLabTests.test_cohort_is_outcome_blind_unique_event_and_derived_day"),
    ("export_private_scope", "research_export.py", 'if event["kind"] not in PUBLIC_KINDS:', 'if False:', "test_alpha_lab.AlphaLabTests.test_export_bundle_deterministic_complete_and_private_scope_only"),
    ("alpha_market_baseline", "alpha_lab.py", "candidate, baseline = score(probabilities), score(baselines)", "candidate, baseline = score(probabilities), score(probabilities)", "test_alpha_lab.AlphaLabTests.test_market_baseline_is_paired_not_replaced_by_candidate"),
    ("alpha_drawdown_units", "alpha_lab.py", "fraction = max(fraction, (peak-equity)/peak)", "fraction = max(fraction, peak-equity)", "test_alpha_lab.AlphaLabTests.test_drawdown_units_are_explicit"),
    ("alpha_duplicate_event", "alpha_lab.py", 'elif row["event_id"] in seen:', 'elif False:', "test_alpha_lab.AlphaLabTests.test_cohort_is_outcome_blind_unique_event_and_derived_day"),
    ("persistent_volume_boundary", "service.py", 'if not data_dir.is_absolute() or data_dir.resolve() != Path("/data/atlas-v2"):', 'if data_dir.name != "atlas-v2":', "test_authority.ApprovalTests.test_persistent_directory_refuses_outside_relative_and_symlink_paths"),
    ("price_cap", "execution.py", "if ask > cap:", "if False:", "test_invariants.EconomicsTests.test_refresh_price_cap"),
    ("spread", "execution.py", "if ask - bid > spread_limit:", "if False:", "test_invariants.EconomicsTests.test_refresh_spread"),
    ("gross_edge", "execution.py", "if gross < min_gross:", "if False:", "test_invariants.EconomicsTests.test_refresh_gross_edge"),
    ("net_edge", "execution.py", "if net < min_net:", "if False:", "test_invariants.EconomicsTests.test_refresh_net_edge"),
    ("ev", "execution.py", "if ev <= min_ev:", "if False:", "test_invariants.EconomicsTests.test_ev_independent_stricter_threshold"),
    ("freshness", "execution.py", "if age < 0 or age > limits.max_age_seconds or utc(at) >= utc(quote.closes_at):", "if False:", "test_invariants.EconomicsTests.test_stale_future_closed_and_no_liquidity"),
    ("rounded_fee_economics", "execution.py", 'fee = fee.quantize(Decimal("0.01"), rounding=ROUND_CEILING)', "fee = fee", "test_invariants.EconomicsTests.test_rounded_cost_bound_also_drives_net_edge"),
    ("duplicate_intent", "execution.py", '"ticker": quote.ticker})', '"ticker": quote.ticker + quote.observed_at})', "test_invariants.IntentTests.test_duplicate_is_durable_across_restart"),
    ("risk_version_fence", "execution.py", 'if not control or control["hash"] != expected_control_hash:', "if not control:", "test_invariants.IntentTests.test_control_change_during_refresh_aborts"),
    ("drawdown_limit", "execution.py", 'if dd < 0 or dd >= Decimal("0.20"):', "if False:", "test_invariants.IntentTests.test_manual_unknown_scope_kill_and_drawdown_abort"),
    ("reuse_reserved_cash", "execution.py", 'decimal(e["payload"]["economics"]["cost_bound"])', 'Decimal(0)', "test_authority.ReservationBudgetTests.test_market_reservations_cannot_reuse_same_cash"),
    ("position_ceiling", "execution.py", "if len(reserved) >= 3:", "if False:", "test_authority.ReservationBudgetTests.test_position_ceiling_is_retained"),
    ("completeness", "domain.py", "page.transport_complete is not True", "False", "test_invariants.CompletenessTests.test_every_unknown_completeness_fails_closed"),
    ("scope", "domain.py", "page.scope != scope", "False", "test_invariants.CompletenessTests.test_every_unknown_completeness_fails_closed"),
    ("partial_range", "data.py", 'or response.get("content_range") is not None', "", "test_invariants.DataTests.test_partial_redirect_and_unknown_envelope_fail_closed"),
    ("cashflow_nav", "accounting.py", "units += amount / nav", "units = units", "test_invariants.AccountingTests.test_withdrawal_does_not_amplify_drawdown_or_erase_loss"),
    ("model_baseline_binding", "validation.py", 'if b != decimal(obs["baseline_probability"]):', "if False:", "test_invariants.ValidationTests.test_baseline_and_consumed_dataset_binding"),
    ("post_close_prediction", "validation.py", 'if utc(result["recorded_at"]) >= utc(obs["close_at"]):', "if False:", "test_research.PairedScoringTests.test_prediction_crossing_close_boundary_rolls_back"),
    ("signature", "approval.py", "Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(signature, canonical(payload))", "pass", "test_authority.ApprovalTests.test_signed_review_is_bound_and_never_financial_authority"),
    ("release_lineage", "service.py", 'or sha != manifest["sha"]', "", "test_authority.ApprovalTests.test_deployment_lineage_refuses_old_main"),
    ("settlement_authority", "accounting.py", 'if state == "SETTLED":', 'if state == "UNREACHABLE":', "test_invariants.IntentTests.test_ambiguous_partial_fill_restart_and_unknown_settlement"),
    ("fill_conservation", "accounting.py", 'if total != decimal(intent["payload"]["economics"]["count"]):', 'if False:', "test_invariants.IntentTests.test_fill_and_payout_conservation"),
    ("transaction_thread_ownership", "store.py", "self.mutex = threading.RLock()", 'self.mutex = __import__("contextlib").nullcontext()', "test_invariants.DurabilityTests.test_thread_cannot_join_another_threads_rollback"),
]


def main():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests")],
                              env=dict(env, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=30)
    if baseline.returncode:
        sys.stderr.write(baseline.stdout + baseline.stderr)
        return 1
    results = []
    for name, filename, before, after, test in MUTANTS:
        with tempfile.TemporaryDirectory(prefix="atlas-v2-mutation-") as directory:
            target = Path(directory)
            shutil.copytree(ROOT / "atlas_v2", target / "atlas_v2", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "tests", target / "tests", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(ROOT / "TRAINING_PROTOCOL.json", target / "TRAINING_PROTOCOL.json")
            tool_root = ROOT / "tools" if (ROOT / "tools").exists() else ROOT.parent / "tools"
            (target / "tools").mkdir()
            for tool in ("__init__.py","brier_oos.py","shadow_pnl.py"):
                shutil.copy2(tool_root / tool, target / "tools" / tool)
            source = target / "atlas_v2" / filename
            text = source.read_text()
            if text.count(before) != 1:
                raise RuntimeError("mutation is not unique: " + name)
            source.write_text(text.replace(before, after, 1))
            run = subprocess.run([sys.executable, "-m", "unittest", test], cwd=target / "tests",
                                 env=dict(env, PYTHONPATH=str(target)), capture_output=True, text=True, timeout=10)
            output = run.stdout + run.stderr
            killed = run.returncode != 0 and "FAIL:" in output and "AssertionError" in output and "ERROR:" not in output
            results.append({"mutation": name, "killed": killed, "test": test})
            if not killed:
                sys.stderr.write(name + "\n" + output)
    print(json.dumps({"baseline": "PASS", "mutations": results, "all_killed": all(r["killed"] for r in results)}, indent=2))
    return 0 if all(r["killed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
