#!/usr/bin/env python3
"""Disposable, semantic mutations for the explicit v4 source contract.

Uses the audited structured phase classifier: setup, collection, import and
infrastructure errors cannot count as a kill. Run through offline_tests.py.
"""
import argparse
import json
from tools import astra_mutation_probe as probe

TEST = "tests/test_li02_source_contract_v4.py::SourceV4Tests::"
SOURCE = "research_source_contract_v4.py"


def definitions():
    # ID, invariant, file, unique mutation anchor, replacement, semantic test,
    # exact assertion fragment reviewed as evidence of that invariant.
    return [
        ("V4M01", "normalized values must replay original bytes", SOURCE,
         '        expected = build_record(binding.get("bundle"))', '        expected = record',
         "test_rehashed_normalized_tampering_is_refused_at_every_shared_gate", "self.assertTrue(validate_record(row))"),
        ("V4M02", "market event must equal captured event", SOURCE,
         '    if type(event) is not dict or event.get("event_ticker") != event_id:', '    if type(event) is not dict:',
         "test_wrong_event_series_identity_and_superficial_label_matches_refuse", "with self.assertRaises(SourceContractError)"),
        ("V4M03", "event series must equal captured series", SOURCE,
         '    if type(series) is not dict or series.get("ticker") != series_id:', '    if type(series) is not dict:',
         "test_wrong_event_series_identity_and_superficial_label_matches_refuse", "with self.assertRaises(SourceContractError)"),
        ("V4M04", "expected time must remain distinct from final expiration", SOURCE,
         '"expected_resolution_time_utc": "expected_expiration_time"}', '"expected_resolution_time_utc": "expiration_time"}',
         "test_expected_expiration_is_distinct_and_source_preimage_keeps_all_times", "self.assertEqual(record['expected_resolution_time_utc'], '2026-09-13T20:20:00Z')"),
        ("V4M05", "fractional counts cannot be truncated", SOURCE,
         '    return number\n', '    return int(number) if places == 2 else number\n',
         "test_complete_synthetic_join_retains_real_facts_without_legacy_paths", "self.assertEqual(record['volume'], 727690.67)"),
        ("V4M06", "dollar quotes cannot be rescaled as cents", SOURCE,
         '    return number\n', '    return number / 100 if places == 4 else number\n',
         "test_complete_synthetic_join_retains_real_facts_without_legacy_paths", "self.assertEqual([record[key] for key in ('yes_bid', 'yes_ask', 'no_bid', 'no_ask')], [0.41, 0.42, 0.58, 0.59])"),
        ("V4M07", "supplied identity aliases cannot contradict", SOURCE,
         '        if alias in obj and (type(obj[alias]) is not str or obj[alias] != canonical):', '        if False:',
         "test_independent_review_identity_alias_and_collection_witnesses", "with self.assertRaises(SourceContractError)"),
        ("V4M08", "optional parent market evidence needs a valid collection", SOURCE,
         '    if "markets" in event_body:', '    if False:',
         "test_independent_review_identity_alias_and_collection_witnesses", "with self.assertRaises(SourceContractError)"),
        ("V4M09", "mixed unsupported numeric evidence cannot be ignored", SOURCE,
         '    if unsupported.intersection(market):', '    if False:',
         "test_independent_review_identity_alias_and_collection_witnesses", "with self.assertRaises(SourceContractError)"),
        ("V4M10", "joined capture age must satisfy declared skew", SOURCE,
         '    if (max(times.values()) - min(times.values())).total_seconds() > MAX_CAPTURE_SKEW_SECONDS:', '    if False:',
         "test_metadata_skew_clock_and_environment_are_bound", "with self.assertRaises(SourceContractError)"),
        ("V4M11", "prediction environment must equal captured environment", "alpha_settlement_validation.py",
         '            if binding["environment"] != source["environment"]:', '            if False:',
         "test_source_snapshot_prediction_environment_and_metadata_time_consistency", "self.assertFalse(verify_source_evidence(altered)['verified'])"),
        ("V4M12", "all four derived quote values remain inadmissible", SOURCE,
         '        expected = build_record(binding.get("bundle"))',
         '        expected = build_record(binding.get("bundle"))\n        expected["quote_observation"] = record["quote_observation"]\n        expected["record_sha256"] = compute_checksum(expected)',
         "test_all_four_derived_quotes_are_rejected_before_mint", "self.assertFalse(assess_record(record)['ready'])"),
        ("V4M13", "same-second source chronology requires exact prediction time", "alpha_gateway.py",
         '            "prediction_time": now.isoformat(timespec=precision),',
         '            "prediction_time": now.isoformat(timespec="seconds"),',
         "test_gateway_same_second_capture_preserves_exact_prediction_chronology",
         "self.assertEqual(observed['prediction_time'], now.isoformat(timespec='microseconds'))"),
        ("V4M14", "public market capture cannot assert account completeness", SOURCE,
         '    if "complete_account_snapshot" in capture and capture["complete_account_snapshot"] is not False:',
         '    if False:',
         "test_capture_scope_completeness_and_continuation_cannot_contradict_raw",
         "with self.assertRaises(SourceContractError)"),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir")
    parser.add_argument("--only", action="append")
    args = parser.parse_args()
    results = []
    for key, invariant, filename, old, new, method, assertion in definitions():
        if args.only and key not in args.only:
            continue
        selector = TEST + method
        probe.MUTATIONS[key] = (invariant, filename, old, new, [selector])
        probe.SEMANTIC_WITNESSES[key] = [{"node": selector, "assertion": assertion,
                                          "invariant": invariant}]
        result = probe.run_one(key, evidence_dir=args.evidence_dir)
        results.append(result)
        print(key + "=" + result["status"], flush=True)
    summary = probe.summarize(results)
    print(json.dumps(summary, indent=2))
    return 0 if summary["gate_passed"] and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
