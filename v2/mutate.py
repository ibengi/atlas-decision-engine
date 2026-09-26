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
