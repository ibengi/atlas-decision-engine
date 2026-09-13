"""Versioned source integration, with genuine market and synthetic metadata.

The archived market capture is genuine read-only evidence. Every event/series
capture and every prediction/outcome below is explicitly synthetic. No fixture
is deployed or offered as authority qualification. No network is used.
"""
import ast
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from types import SimpleNamespace

from tests import _gates  # noqa: F401
from tests._candidate import valid_record
from candidate_contract import (canonical_content, canonical_json, compute_checksum,
                                validate_record, FEED_SCHEMA)
from research_source_contract_v4 import (BUNDLE_SCHEMA, CAPTURE_SCHEMA, ORIGIN,
                                        SourceContractError, build_record)
from alpha_consumer import ProcessedStore, SpoolConsumer
from alpha_feed_readiness import assess_record
from alpha_ledger import AlphaLedger
from alpha_resolution_ingest import ingest_settlements
from alpha_settlement_validation import (validate_source_snapshot,
                                         verify_source_evidence, settlement_qualification)
from readonly_research_producer import Producer, CaptureRefused, capture_public_source

ROOT = Path(__file__).resolve().parent.parent
CAPTURE = ROOT / "tests/fixtures/li02_market_capture_20260913.json"
EVENT = "KXBTC15M-26SEP131615"
SERIES = "KXBTC15M"
PREDICTED = "2026-09-13T20:10:30+00:00"
RESOLVED = "2026-09-13T20:21:00+00:00"


def synthetic_capture(body, endpoint, observed):
    raw = canonical_json(body).encode()
    row = {
        "schema": CAPTURE_SCHEMA, "source_authority": "kalshi-public-market-data",
        "source_scope": "public-market-data",
        "environment": "prod", "http_method": "GET", "http_status": 200,
        "source_url": endpoint, "transport_authentication": "TLS-server-certificate-verification",
        "emitted_at_utc": observed, "response_byte_count": len(raw),
        "response_bytes_base64": base64.b64encode(raw).decode(),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "test_fixture": "SYNTHETIC metadata, never real authority evidence",
    }
    row["record_sha256"] = compute_checksum(row)
    return row


def bundle_fixture():
    return {
        "schema": BUNDLE_SCHEMA, "market_capture": json.loads(CAPTURE.read_bytes()),
        "market_pointer": "/markets/0",
        "event_capture": synthetic_capture({"event": {
            "event_ticker": EVENT, "series_ticker": SERIES,
            "title": "SYNTHETIC identity metadata fixture"}},
            ORIGIN + "/events/" + EVENT, "2026-09-13T20:10:10+00:00"),
        "series_capture": synthetic_capture({"series": {
            "ticker": SERIES, "settlement_sources": [{
                "name": "SYNTHETIC settlement authority",
                "url": "https://synthetic.invalid/settlement"}]}},
            ORIGIN + "/series/" + SERIES, "2026-09-13T20:10:11+00:00"),
    }


def rewrite_capture(bundle, role, mutate):
    """Only disposable in-memory copies are mutated; archived bytes stay fixed."""
    capture = bundle[role + "_capture"]
    body = json.loads(base64.b64decode(capture["response_bytes_base64"]))
    mutate(body)
    raw = canonical_json(body).encode()
    capture.update(response_bytes_base64=base64.b64encode(raw).decode(),
                   response_byte_count=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
    capture["record_sha256"] = compute_checksum(capture)


def prediction(record):
    snapshot = SpoolConsumer.mint(None, record)
    return {
        "schema": "atlas-alpha-ledger-v1", "kind": "PREDICTION",
        "prediction_id": "SYNTHETIC-v4-prediction", "prediction_time": PREDICTED,
        "contract_id": snapshot.contract_id, "market_snapshot_id": snapshot.market_snapshot_id,
        "snapshot": snapshot.as_dict(),
        "source_binding": {"record_sha256": record["record_sha256"],
            "source_evidence": canonical_content(record), "digest_verified": True,
            "contract_id": snapshot.contract_id, "market_snapshot_id": snapshot.market_snapshot_id,
            "contract_schema": record["schema"], "environment": "prod"},
        "state": "INSUFFICIENT_DATA", "p_meta": .6, "confidence": .7,
        "per_model": {"synthetic": {"p_yes": .6}}, "market_class": "MEDIUM",
    }


def incoming(pred):
    binding = pred["source_binding"]
    return {
        "prediction_id": pred["prediction_id"], "outcome": 1,
        "source": binding["source_evidence"]["resolution_source"],
        "resolved_at": RESOLVED, "settlement_evidence_id": "SYNTHETIC-outcome-receipt",
        "contract_id": binding["contract_id"], "market_snapshot_id": binding["market_snapshot_id"],
        "source_record_sha256": binding["record_sha256"],
        "environment": "prod", "contract_schema": binding["contract_schema"],
    }


class SourceV4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atlas-source-v4-")
        self.addCleanup(self.tmp.cleanup)
        clock = patch("alpha_settlement_validation._utc_now", return_value=
                      datetime(2026, 9, 13, 20, 30, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)

    def refused(self, bundle):
        with self.assertRaises(SourceContractError):
            build_record(bundle)

    def test_actual_capture_bytes_are_permanent_and_missing_metadata_refuses(self):
        exact = CAPTURE.read_bytes()
        self.assertEqual(len(exact), 3600)
        capture = json.loads(exact)
        raw = base64.b64decode(capture["response_bytes_base64"], validate=True)
        self.assertEqual(len(raw), 2145)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "5450d82401be7f8f6a90cf774c4419c15c095bf2fafd7f192cba84eabc864d8e")
        self.assertEqual(compute_checksum(capture), "469739d2d736287c262ac4962eb132bf9103853ca0ab1949fe26a09e97e94add")
        bundle = bundle_fixture()
        bundle["event_capture"] = None
        bundle["series_capture"] = None
        self.refused(bundle)
        self.assertEqual(CAPTURE.read_bytes(), exact)

    def test_complete_synthetic_join_retains_real_facts_without_legacy_paths(self):
        record = build_record(bundle_fixture())
        self.assertEqual(validate_record(record), [])
        self.assertTrue(assess_record(record)["ready"])
        self.assertEqual([record[key] for key in ("yes_bid", "yes_ask", "no_bid", "no_ask")], [.41, .42, .58, .59])
        self.assertEqual(record["volume"], 727690.67)
        self.assertEqual(record["open_interest"], 246115.69)
        self.assertIn("/yes_bid_dollars[usd-binary-unit-payout-4dp]", record["field_provenance"]["yes_bid"])
        self.assertNotIn("(cents)", record["field_provenance"]["yes_bid"])
        self.assertIn("#/series/settlement_sources[", record["field_provenance"]["resolution_source"])
        self.assertEqual(record["source_binding_v4"]["settlement_authority_qualification"], "NOT_ESTABLISHED")

    def test_expected_expiration_is_distinct_and_source_preimage_keeps_all_times(self):
        record = build_record(bundle_fixture())
        self.assertEqual(record["expected_resolution_time_utc"], "2026-09-13T20:20:00Z")
        self.assertEqual(record["contradictory_fields"], {})
        raw = json.loads(base64.b64decode(record["source_binding_v4"]["bundle"]["market_capture"]["response_bytes_base64"]))["markets"][0]
        self.assertEqual(raw["expiration_time"], "2026-09-20T20:15:00Z")
        self.assertEqual(raw["latest_expiration_time"], "2026-09-20T20:15:00Z")
        self.assertEqual(raw["close_time"], "2026-09-13T20:15:00Z")
        self.assertEqual(raw["occurrence_datetime"], "2026-09-13T20:20:00Z")

    def test_missing_expected_time_is_not_replaced_from_other_observed_times(self):
        for change in (lambda m: m.pop("expected_expiration_time"),
                       lambda m: m.update(expected_expiration_time=None),
                       lambda m: m.update(expected_expiration_time=True)):
            bundle = bundle_fixture()
            rewrite_capture(bundle, "market", lambda body: change(body["markets"][0]))
            self.refused(bundle)

    def test_exact_decimal_profile_rejects_coercion_truncation_and_non_unit_payout(self):
        for key, bad in [("yes_bid_dollars", v) for v in (True, .41, "0.41", " 0.4100", "4.1e-1", "NaN", "-0.4100", "1.1000", "0.41001")] + [
                ("volume_fp", "727690.671"), ("volume_fp", "10000000000000.01"),
                ("open_interest_fp", True), ("notional_value_dollars", "2.0000")]:
            with self.subTest(key=key, bad=bad):
                bundle = bundle_fixture()
                rewrite_capture(bundle, "market", lambda body: body["markets"][0].update({key: bad}))
                self.refused(bundle)

    def test_missing_quote_side_never_reconstructed(self):
        for key in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars"):
            bundle = bundle_fixture()
            rewrite_capture(bundle, "market", lambda body: body["markets"][0].pop(key))
            self.refused(bundle)

    def test_wrong_event_series_identity_and_superficial_label_matches_refuse(self):
        for role, change in [
            ("event", lambda body: body["event"].update(event_ticker="ANOTHER-EVENT")),
            ("event", lambda body: body["event"].pop("series_ticker")),
            ("series", lambda body: body["series"].update(ticker="ANOTHER-SERIES")),
            ("market", lambda body: body["markets"][0].update(event_id="ANOTHER-EVENT")),
            ("market", lambda body: body["markets"][0].update(series_ticker="ANOTHER-SERIES")),
        ]:
            bundle = bundle_fixture()
            rewrite_capture(bundle, role, change)
            self.refused(bundle)

    def test_missing_structured_source_and_unknown_extensions_refuse(self):
        for sources in (None, [], "CF Benchmarks BRTI", [True],
                        [{"name": "SYNTHETIC", "extension": {"authority": "other"}}],
                        [{"name": "SYNTHETIC", "url": 1}]):
            bundle = bundle_fixture()
            rewrite_capture(bundle, "series", lambda body: body["series"].update(settlement_sources=sources))
            self.refused(bundle)

    def test_conflicting_parent_source_not_silently_substituted(self):
        bundle = bundle_fixture()
        rewrite_capture(bundle, "event", lambda body: body["event"].update(
            settlement_sources=[{"name": "OTHER synthetic authority"}]))
        self.refused(bundle)

    def test_metadata_skew_clock_and_environment_are_bound(self):
        for field, value in (("emitted_at_utc", "2026-09-13T21:10:10+00:00"),
                             ("environment", "demo"), ("http_status", True),
                             ("source_url", ORIGIN + "/series/OTHER")):
            bundle = bundle_fixture()
            capture = bundle["series_capture"]
            capture[field] = value
            capture["record_sha256"] = compute_checksum(capture)
            self.refused(bundle)

    def test_source_updated_after_its_capture_is_refused(self):
        bundle = bundle_fixture()
        rewrite_capture(bundle, "event", lambda body: body["event"].update(
            updated_time="2026-09-13T20:10:11+00:00"))
        self.refused(bundle)

    def test_rehashed_normalized_tampering_is_refused_at_every_shared_gate(self):
        original = build_record(bundle_fixture())
        snap = SpoolConsumer.mint(None, original)
        for field, value in (("yes_bid", .40), ("volume", 727690),
                             ("expected_resolution_time_utc", "2026-09-20T20:15:00Z"),
                             ("contract_id", "OTHER"), ("resolution_source", "CF Benchmarks BRTI")):
            row = copy.deepcopy(original)
            row[field] = value
            row["record_sha256"] = compute_checksum(row)
            self.assertTrue(validate_record(row))
            self.assertFalse(assess_record(row)["ready"])
            self.assertFalse(validate_source_snapshot(row, snap)[0])
            consumer = SpoolConsumer(source=type("Local", (), {"records": lambda self: [row]})(),
                                     store=ProcessedStore(os.path.join(self.tmp.name, "processed.jsonl")))
            self.assertEqual(consumer.pending(), [])

    def test_rehashed_binding_preimage_pointer_and_unit_tampering_refuse(self):
        original = build_record(bundle_fixture())
        changes = [
            lambda row: row["source_binding_v4"].update(normalization="trust-caller"),
            lambda row: row["source_binding_v4"]["bundle"].update(market_pointer="/markets/1"),
            lambda row: row["field_provenance"].update(yes_bid="raw_book.yes_bid(cents)"),
            lambda row: row["source_binding_v4"]["bundle"].pop("event_capture"),
            lambda row: row["quote_observation"].update(no_ask="derived"),
        ]
        for change in changes:
            row = copy.deepcopy(original)
            change(row)
            row["record_sha256"] = compute_checksum(row)
            self.assertTrue(validate_record(row))

    def test_rehashed_raw_change_cannot_keep_old_normalized_observation(self):
        row = build_record(bundle_fixture())
        bundle = row["source_binding_v4"]["bundle"]
        rewrite_capture(bundle, "market", lambda body: body["markets"][0].update(yes_bid_dollars="0.4000"))
        row["source_binding_v4"]["bundle_sha256"] = hashlib.sha256(canonical_json(bundle).encode()).hexdigest()
        row["record_sha256"] = compute_checksum(row)
        self.assertTrue(validate_record(row))

    def test_source_snapshot_prediction_environment_and_metadata_time_consistency(self):
        pred = prediction(build_record(bundle_fixture()))
        self.assertTrue(verify_source_evidence(pred)["verified"])
        for changes in ({"prediction_time": "2026-09-13T20:10:10+00:00"},):
            altered = copy.deepcopy(pred); altered.update(changes)
            self.assertFalse(verify_source_evidence(altered)["verified"])
        altered = copy.deepcopy(pred)
        altered["source_binding"]["environment"] = "demo"
        self.assertFalse(verify_source_evidence(altered)["verified"])
        altered = copy.deepcopy(pred)
        altered["source_binding"]["contract_schema"] = FEED_SCHEMA
        self.assertFalse(verify_source_evidence(altered)["verified"])

    def test_synthetic_prediction_and_settlement_replay_after_restart_retains_bytes(self):
        record = build_record(bundle_fixture())
        pred = prediction(record)
        ledger = AlphaLedger(os.path.join(self.tmp.name, "predictions.jsonl"),
                             os.path.join(self.tmp.name, "cost.jsonl"))
        stored = ledger.record_prediction(pred)
        result = ingest_settlements(ledger, [incoming(stored)],
                                    trusted_sources=[record["resolution_source"]])
        self.assertEqual(result["appended"], 1)
        before = Path(ledger.log.path).read_bytes()
        restarted = AlphaLedger(ledger.log.path, ledger.cost_log.path)
        self.assertEqual(len(restarted.qualified_resolved()), 1)
        self.assertTrue(verify_source_evidence(restarted.predictions()[0])["verified"])
        self.assertEqual(Path(ledger.log.path).read_bytes(), before)

    def test_learning_replays_source_join_and_excludes_tampered_history(self):
        record = build_record(bundle_fixture())
        ledger = AlphaLedger(os.path.join(self.tmp.name, "predictions.jsonl"),
                             os.path.join(self.tmp.name, "cost.jsonl"))
        pred = ledger.record_prediction(prediction(record))
        ingest_settlements(ledger, [incoming(pred)], trusted_sources=[record["resolution_source"]])
        resolution = [row for row in ledger.rows() if row["kind"] == "RESOLUTION"][0]
        bad = copy.deepcopy(pred)
        bad["source_binding"]["source_evidence"]["yes_bid"] = .1
        bad["source_binding"]["record_sha256"] = compute_checksum(bad["source_binding"]["source_evidence"])
        self.assertFalse(settlement_qualification(bad, resolution)[0])
        before = Path(ledger.log.path).read_bytes()
        self.assertEqual(Path(ledger.log.path).read_bytes(), before)

    def test_new_bundle_interface_keeps_collector_legacy_and_barriers_fail_closed(self):
        producer = Producer(self.tmp.name, fetch=lambda: self.fail("network forbidden"))
        with patch.object(producer.spool, "write", return_value=False):
            with self.assertRaises(ValueError):
                producer.ingest_bundle(bundle_fixture())
        self.assertEqual(producer.health()["candidates_durable"], 0)
        producer.ingest_bundle(bundle_fixture())
        page = producer.page()
        self.assertEqual(page["schema_version"], "atlas-research-candidate-v4")
        self.assertEqual(page["record_schema_versions"], ["atlas-research-candidate-v4"])
        row = page["rows"][0]
        self.assertEqual(validate_record(row), [])
        with patch("readonly_research_producer.sync_path", side_effect=OSError("synthetic durability failure")):
            for _ in range(6):
                with self.assertRaises(OSError): producer.page()
        restarted = Producer(self.tmp.name, fetch=lambda: self.fail("network forbidden"))
        self.assertEqual(restarted.page()["rows"], [row])

    def test_original_v3_rules_and_unknown_schema_refusals_remain_unchanged(self):
        record = valid_record()
        self.assertEqual(validate_record(record), [])
        self.assertEqual(record["schema"], FEED_SCHEMA)
        record["quote_observation"]["no_bid"] = "derived"
        record["record_sha256"] = compute_checksum(record)
        self.assertTrue(validate_record(record))
        modern = build_record(bundle_fixture())
        modern["schema"] = "atlas-research-candidate-v999"
        modern["record_sha256"] = compute_checksum(modern)
        self.assertTrue(validate_record(modern))

    def test_offline_qualifier_never_claims_authority_or_live_authenticity(self):
        from tools.alpha_live_schema_qualify import qualify_bundle
        verdict = qualify_bundle(bundle_fixture())
        self.assertEqual(verdict["semantic_source_contract"], "PASS")
        self.assertEqual(verdict["status"], "LIVE_SCHEMA_UNPROVEN")
        self.assertEqual(verdict["settlement_authority"], "NOT_QUALIFIED_BY_OFFLINE_VERIFIER")

    def test_opt_in_collector_fetches_only_exact_joined_public_endpoints(self):
        bundle = bundle_fixture()
        called = []
        def metadata(url):
            called.append(url)
            role = "event" if "/events/" in url else "series"
            cap = bundle[role + "_capture"]
            self.assertEqual(url, cap["source_url"])
            return base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]
        cap = bundle["market_capture"]
        producer = Producer(self.tmp.name, source_contract="market-event-series-v4",
            fetch=lambda: (base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]),
            metadata_fetch=metadata)
        self.assertEqual(producer.poll()["written"], 1)
        self.assertEqual(called, [ORIGIN + "/events/" + EVENT, ORIGIN + "/series/" + SERIES])
        row = producer.page()["rows"][0]
        self.assertEqual(validate_record(row), [])
        self.assertEqual(producer.health()["source_contract"], "market-event-series-v4")
        self.assertEqual(row["source_binding_v4"]["settlement_authority_qualification"], "NOT_ESTABLISHED")

    def test_collector_default_never_fetches_parent_metadata(self):
        cap = bundle_fixture()["market_capture"]
        producer = Producer(self.tmp.name,
            fetch=lambda: (base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]),
            metadata_fetch=lambda url: self.fail("legacy unexpectedly requested metadata"))
        self.assertEqual(producer.source_contract, "legacy-v3")
        self.assertEqual(producer.poll()["written"], 0)
        self.assertEqual(producer.health()["candidates_durable"], 0)

    def test_opt_in_collector_refuses_403_without_alternate_request_or_candidate(self):
        cap = bundle_fixture()["market_capture"]
        called = []
        def denied(url):
            called.append(url)
            raise urllib.error.HTTPError(url, 403, "synthetic refusal", {}, None)
        producer = Producer(self.tmp.name, source_contract="market-event-series-v4",
            fetch=lambda: (base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]),
            metadata_fetch=denied)
        self.assertIsNone(producer.poll())
        self.assertEqual(called, [ORIGIN + "/events/" + EVENT])
        self.assertEqual(producer.health()["last_source_http_status"], 403)
        self.assertEqual(producer.health()["candidates_durable"], 0)

    def test_opt_in_collector_reports_unknown_parent_schema_without_inference(self):
        cap = bundle_fixture()["market_capture"]
        producer = Producer(self.tmp.name, source_contract="market-event-series-v4",
            fetch=lambda: (base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]),
            metadata_fetch=lambda url: (b'{"unexpected":{"settlement_source":"CF BRTI"}}', "2026-09-13T20:10:10+00:00"))
        self.assertIsNone(producer.poll())
        self.assertIn("unsupported fields", producer.health()["last_schema_refusal"])
        self.assertEqual(producer.health()["candidates_durable"], 0)

    def test_fixed_transport_refuses_host_path_query_and_credentials_before_network(self):
        with patch("readonly_research_producer.urllib.request.build_opener", side_effect=AssertionError("network forbidden")):
            for url in ("http://external-api.kalshi.com/trade-api/v2/events/A", "https://example.invalid/events/A",
                        ORIGIN + "/events/../markets", ORIGIN + "/events/A?next=x", ORIGIN + "/events/A%2FB",
                        "https://name:secret@external-api.kalshi.com/trade-api/v2/events/A"):
                with self.subTest(url=url), self.assertRaises(CaptureRefused):
                    capture_public_source(url)

    def test_metadata_identifier_cannot_create_an_unapproved_request(self):
        bundle = bundle_fixture()
        rewrite_capture(bundle, "market", lambda body: body["markets"][0].update(event_ticker="../markets?x=y"))
        cap = bundle["market_capture"]
        producer = Producer(self.tmp.name, source_contract="market-event-series-v4",
            fetch=lambda: (base64.b64decode(cap["response_bytes_base64"]), cap["emitted_at_utc"]),
            metadata_fetch=lambda url: self.fail("invalid identifier reached transport"))
        self.assertIsNone(producer.poll())
        self.assertEqual(producer.health()["candidates_durable"], 0)

    def test_explicit_source_mode_refuses_unknown_or_ambiguous_values(self):
        for mode in ("v4", "true", True, "", "market-event-series-v4 "):
            with self.subTest(mode=mode), self.assertRaises(CaptureRefused):
                Producer(self.tmp.name, source_contract=mode)

    def test_pure_new_contract_has_no_financial_or_io_imports(self):
        tree = ast.parse((ROOT / "research_source_contract_v4.py").read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom): imports.add((node.module or "").split(".")[0])
        self.assertFalse(imports & {"execution_engine", "order_manager", "risk_manager", "kalshi_client", "requests", "urllib", "os", "config", "logging"})

    def test_independent_review_identity_alias_and_collection_witnesses(self):
        cases = [
            ("event", lambda body: body.update(markets="not a collection")),
            ("event", lambda body: body.update(markets=[True])),
            ("event", lambda body: body.update(markets=[{"ticker": "KXBTC15M-26SEP131615-15", "event_ticker": "OTHER-EVENT"}])),
            ("event", lambda body: body["event"].update(event_id="OTHER-EVENT")),
            ("series", lambda body: body["series"].update(series_ticker="OTHER-SERIES")),
            ("market", lambda body: body["markets"][0].update(contract_id="OTHER-CONTRACT")),
            ("market", lambda body: body["markets"][0].update(yes_bid=89)),
        ]
        for role, change in cases:
            bundle = bundle_fixture()
            rewrite_capture(bundle, role, change)
            self.refused(bundle)

    def test_neighboring_alias_types_duplicates_and_mixed_units_refuse(self):
        for field in ("contract_id", "market_ticker", "event_id"):
            for bad in (True, 1, {}, [], None, " OTHER "):
                bundle = bundle_fixture()
                rewrite_capture(bundle, "market", lambda body: body["markets"][0].update({field: bad}))
                self.refused(bundle)
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest"):
            bundle = bundle_fixture()
            rewrite_capture(bundle, "market", lambda body: body["markets"][0].update({field: 0}))
            self.refused(bundle)
        bundle = bundle_fixture()
        member = {"ticker": "KXBTC15M-26SEP131615-15", "event_ticker": EVENT}
        rewrite_capture(bundle, "event", lambda body: body.update(markets=[member, member]))
        self.refused(bundle)

    def test_optional_parent_collection_is_not_atomic_quote_or_completeness_proof(self):
        for members in ([], [{"ticker": "KXBTC15M-26SEP131615-15", "event_ticker": EVENT,
                             "yes_bid_dollars": "0.4300", "yes_ask_dollars": "0.4400"}]):
            bundle = bundle_fixture()
            rewrite_capture(bundle, "event", lambda body: body.update(markets=members))
            record = build_record(bundle)
            self.assertEqual(record["yes_bid"], .41)
            self.assertFalse(record["source_binding_v4"]["atomic_exchange_snapshot"])

    def test_all_four_derived_quotes_are_rejected_before_mint(self):
        record = build_record(bundle_fixture())
        record["quote_observation"] = {key: "derived" for key in ("yes_bid", "yes_ask", "no_bid", "no_ask")}
        record["record_sha256"] = compute_checksum(record)
        self.assertFalse(assess_record(record)["ready"])
        self.assertTrue(validate_record(record))

    def test_capture_scope_completeness_and_continuation_cannot_contradict_raw(self):
        for field, value in (("source_scope", "complete-account-state"),
                             ("complete_account_snapshot", True),
                             ("complete_account_snapshot", 0),
                             ("sample_has_more", True), ("sample_has_more", 0),
                             ("arbitrary_trust_extension", {"qualified": True})):
            bundle = bundle_fixture()
            cap = bundle["market_capture"]
            cap[field] = value
            cap["record_sha256"] = compute_checksum(cap)
            self.refused(bundle)

    def test_gateway_same_second_capture_preserves_exact_prediction_chronology(self):
        from alpha_gateway import AlphaGateway
        bundle = bundle_fixture()
        cap = bundle["series_capture"]
        cap["emitted_at_utc"] = "2026-09-13T20:10:11.100000+00:00"
        cap["record_sha256"] = compute_checksum(cap)
        record = build_record(bundle)
        pred = prediction(record)
        snapshot = SpoolConsumer.mint(None, record)
        for micros, qualifies in ((900000, True), (100000, True), (99999, False)):
            now = datetime(2026, 9, 13, 20, 10, 11, micros, tzinfo=timezone.utc)
            observed = AlphaGateway._record(None, snapshot,
                SimpleNamespace(signals=[], as_dict=lambda: {}), {}, {}, {},
                "INSUFFICIENT_DATA", "SYNTHETIC", 0, now,
                source_binding=pred["source_binding"])
            self.assertEqual(observed["prediction_time"], now.isoformat(timespec="microseconds"))
            self.assertEqual(verify_source_evidence(observed)["verified"], qualifies)
        legacy = AlphaGateway._record(None, snapshot,
            SimpleNamespace(signals=[], as_dict=lambda: {}), {}, {}, {},
            "INSUFFICIENT_DATA", "SYNTHETIC", 0, now,
            source_binding={"contract_schema": FEED_SCHEMA})
        self.assertEqual(legacy["prediction_time"], now.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()
