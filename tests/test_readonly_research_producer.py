"""LI-02: deterministic witnesses; loopback HTTP and synthetic bytes only."""
import ast
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
from _candidate import raw_market  # noqa: E402
from alpha_feed_readiness import assess_record  # noqa: E402
from candidate_contract import compute_checksum, validate_record  # noqa: E402
import readonly_research_producer as rp  # noqa: E402

OBSERVED = "2026-09-13T12:00:00.000001+00:00"
TOKEN = "synthetic-research-token-not-a-secret"


def capture(market=None):
    return json.dumps({"markets": [raw_market() if market is None else market],
                       "cursor": "another-page"}, indent=2).encode()


class ProducerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.producer = rp.Producer(self.temp.name,
                                   fetch=lambda: (capture(), OBSERVED))

    def record(self):
        self.producer.poll()
        return self.producer.page()["rows"][0]

    def test_valid_synthetic_raw_market_has_checksum_and_readiness(self):
        row = self.record()
        self.assertEqual(validate_record(row), [])
        self.assertTrue(assess_record(row)["ready"])
        self.assertTrue(self.producer.verify_capture_binding(row))

    def test_exact_wire_bytes_and_market_preimage_survive(self):
        raw = capture()
        self.producer.ingest(raw, OBSERVED)
        row = self.producer.page()["rows"][0]
        binding = row["source_capture"]
        evidence = binding["capture"]
        self.assertEqual(base64.b64decode(evidence["response_bytes_base64"]), raw)
        self.assertEqual(evidence["response_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads(binding["canonical_market"]), json.loads(raw)["markets"][0])
        self.assertFalse(evidence["complete_account_snapshot"])
        self.assertTrue(evidence["sample_has_more"])

    def test_restart_replays_same_identity(self):
        row = self.record()
        restarted = rp.Producer(self.temp.name, fetch=lambda: self.fail("network"))
        self.assertEqual(restarted.page()["rows"], [row])

    def test_capture_retained_when_no_candidate_is_eligible(self):
        result = self.producer.ingest(capture({"ticker": "REAL-FORMAT-INCOMPLETE"}), OBSERVED)
        self.assertEqual(result["written"], 0)
        self.assertEqual(result["refused"], 1)
        self.assertEqual(len(list(Path(self.producer.captures.directory).glob("*.json"))), 1)

    def test_fixed_point_strings_are_never_relabelled_as_observed_legacy_facts(self):
        market = raw_market()
        for name in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            market[name + "_dollars"] = str(market.pop(name) / 100)
        market["volume_fp"] = str(market.pop("volume"))
        result = self.producer.ingest(capture(market), OBSERVED)
        self.assertEqual(result["written"], 0)

    def test_event_source_is_not_substituted_for_market_source(self):
        market = raw_market()
        market.pop("settlement_sources")
        raw = json.dumps({"markets": [market], "event": {
            "settlement_sources": [{"name": "event-authority"}]}}).encode()
        self.assertEqual(self.producer.ingest(raw, OBSERVED)["written"], 0)

    def test_malformed_capture_members_and_duplicate_json_refused(self):
        values = [b'{"markets":{}}', b'{"markets":[true]}',
                  b'{"markets":[],"markets":[]}', b'{"markets":[],"cursor":false}',
                  b'{"markets":[],"other":NaN}', b'\xff', b'{}',
                  b"x" * (rp.MAX_CAPTURE_BYTES + 1)]
        for raw in values:
            with self.subTest(raw=raw[:60]), self.assertRaises(rp.CaptureRefused):
                self.producer.ingest(raw, OBSERVED)

    def test_recomputed_outer_checksum_cannot_hide_source_mismatch(self):
        row = self.record()
        row["question"] = "another economic observation"
        row["record_sha256"] = compute_checksum(row)
        self.assertEqual(validate_record(row), [])
        with self.assertRaises(rp.CaptureRefused):
            self.producer.verify_capture_binding(row)

    def test_capture_identity_and_market_pointer_are_semantically_bound(self):
        original = self.record()
        for changes in ({"source_url": "https://example.invalid"},
                        {"environment": "demo"}, {"http_status": True},
                        {"response_byte_count": True}):
            with self.subTest(changes=changes), self.assertRaises(rp.CaptureRefused):
                row = copy.deepcopy(original)
                evidence = row["source_capture"]["capture"]
                evidence.update(changes)
                evidence["record_sha256"] = compute_checksum(evidence)
                row["record_sha256"] = compute_checksum(row)
                self.producer.verify_capture_binding(row)
        row = copy.deepcopy(original)
        row["source_capture"]["market_pointer"] = "/markets/9"
        row["record_sha256"] = compute_checksum(row)
        with self.assertRaises(rp.CaptureRefused):
            self.producer.verify_capture_binding(row)

    def test_capture_fsync_failure_does_not_publish_candidate(self):
        with patch.object(self.producer.captures, "write", return_value=False):
            self.assertIsNone(self.producer.poll())
        self.assertEqual(self.producer.health()["candidates_durable"], 0)

    def test_readable_candidate_during_sync_failure_remains_unavailable(self):
        self.record()
        with patch.object(rp, "sync_path", side_effect=OSError("synthetic fsync")):
            for _ in range(6):
                with self.assertRaises(OSError):
                    self.producer.page()
            restarted = rp.Producer(self.temp.name)
            with self.assertRaises(OSError):
                restarted.page()
        self.assertEqual(len(restarted.page()["rows"]), 1)

    def test_metadata_uncertainty_is_not_empty_page(self):
        self.record()
        with patch.object(self.producer.spool, "_scan", side_effect=OSError("stat fault")):
            with self.assertRaises(OSError):
                self.producer.page()

    def test_modified_read_generation_is_not_published(self):
        self.record()
        with patch.object(rp, "sync_path", return_value=False):
            with self.assertRaises(rp.CaptureRefused):
                self.producer.page()

    def test_health_does_not_fetch_scan_or_write(self):
        with patch.object(self.producer, "fetch", side_effect=AssertionError), \
                patch.object(self.producer.spool, "_scan", side_effect=AssertionError), \
                patch.object(self.producer.captures, "write", side_effect=AssertionError):
            self.assertEqual(self.producer.health()["capture_attempts"], 0)

    def test_rolling_capacity_keeps_intake_operational_and_full_preimage(self):
        self.producer.captures.max_records = 2
        self.producer.spool.max_records = 3
        for minute in range(40):
            stamp = f"2026-09-13T12:{minute:02}:00.000001+00:00"
            self.assertEqual(self.producer.ingest(capture(), stamp)["written"], 1)
        rows = self.producer.page()["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(list(Path(self.producer.captures.directory).glob("*.json"))), 2)
        for row in rows:
            self.assertTrue(self.producer.verify_capture_binding(row))

    def test_partial_reservations_are_counted_and_never_evicted(self):
        directory = Path(self.producer.captures.directory)
        directory.mkdir(parents=True)
        partial = directory / f"inflight.{os.getpid()}.partial"
        partial.write_bytes(b"in-progress synthetic evidence")
        self.producer.captures.max_records = 1
        self.assertIsNone(self.producer.poll())
        self.assertEqual(partial.read_bytes(), b"in-progress synthetic evidence")
        self.assertEqual(self.producer.health()["candidates_durable"], 0)

    def test_failed_eviction_refuses_new_write_and_preserves_existing_capture(self):
        self.record()
        self.producer.captures.max_records = 1
        before = {p.name: p.read_bytes() for p in Path(self.producer.captures.directory).glob("*.json")}
        with patch.object(self.producer.captures, "_remove", return_value=False):
            self.assertIsNone(self.producer.poll())
        after = {p.name: p.read_bytes() for p in Path(self.producer.captures.directory).glob("*.json")}
        self.assertEqual(before, after)

    def test_eviction_then_failed_new_directory_barrier_needs_real_recovery(self):
        self.record()
        self.producer.spool.max_records = 2
        self.producer.ingest(capture(), "2026-09-13T12:01:00.000001+00:00")
        with patch.object(self.producer.spool, "_fsync_directory", side_effect=OSError("synthetic")):
            with self.assertRaises(rp.CaptureRefused):
                self.producer.ingest(capture(), "2026-09-13T12:02:00.000001+00:00")
        with patch.object(rp, "sync_path", side_effect=OSError("barrier still unavailable")):
            with self.assertRaises(OSError):
                self.producer.page()
        recovered = self.producer.page()["rows"]
        self.assertEqual(len(recovered), 2)
        self.assertTrue(all(self.producer.verify_capture_binding(row) for row in recovered))

    def test_byte_cap_is_enforced_during_rolling_eviction(self):
        self.record()
        self.producer.captures.max_bytes = 1
        self.assertIsNone(self.producer.poll())
        self.assertEqual(self.producer.captures.capacity()["used_bytes"], 0)


class HTTPCase(unittest.TestCase):
    def setUp(self):
        ProducerCase.setUp(self)
        ProducerCase.record(self)
        self.server = rp.BoundedHTTPServer(("127.0.0.1", 0),
                                          rp.handler_for(self.producer, TOKEN))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/research/v1/candidates", token=TOKEN):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None

    def test_authentication_refuses_absent_wrong_and_accepts_exact_token(self):
        self.assertEqual(self.request(token=None)[0], 401)
        self.assertEqual(self.request(token="wrong-token")[0], 401)
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(validate_record(body["rows"][0]), [])

    def test_mutation_methods_and_unknown_routes_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request(method)[0], 405)
        self.assertEqual(self.request(path="/api/orders")[0], 404)

    def test_bad_limit_duplicate_cursor_and_url_parameters_refused(self):
        for query in ("limit=0", "limit=101", "limit=10&limit=20", "source_url=x",
                      "cursor=../../x", "cursor=x&cursor=y"):
            with self.subTest(query=query):
                self.assertEqual(self.request(path="/api/research/v1/candidates?" + query)[0], 400)

    def test_storage_failure_returns_503_not_empty_success(self):
        with patch.object(self.producer, "page", side_effect=OSError("synthetic")):
            self.assertEqual(self.request()[0], 503)

    def test_http_export_never_triggers_source_capture(self):
        with patch.object(self.producer, "fetch", side_effect=AssertionError("network")):
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.producer.health()["capture_attempts"], 1)


class BoundaryCase(unittest.TestCase):
    def test_no_forbidden_import_or_mutation_primitive_in_new_modules(self):
        root = Path(rp.__file__).parent
        for file in (root / "readonly_research_producer.py", root / "tools/readonly_research_run.py"):
            tree = ast.parse(file.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
            self.assertFalse(imported & rp.FORBIDDEN_MODULES)

    def test_clean_interpreter_has_no_execution_modules_and_authority_names_refused(self):
        root = str(Path(rp.__file__).parent)
        code = ("import readonly_research_producer as p; "
                "assert p.assert_isolated({})['execution_imports'] == 0; "
                "print('ISOLATION_PROVEN_SYNTHETIC')")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": root,
               "DATA_DIR": tempfile.gettempdir()}
        result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ISOLATION_PROVEN_SYNTHETIC", result.stdout)
        for name in rp.FORBIDDEN_ENV:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                rp.assert_isolated({name: ""})

    def test_token_validation_is_fail_closed(self):
        for value in ("", "short", "abc defghijklmnopqrstuvwxyz", "é" * 20):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                rp.handler_for(None, value)

    def test_transport_has_fixed_get_verified_tls_no_proxy_or_redirect(self):
        with patch.object(rp.urllib.request, "build_opener") as build, \
                patch.object(rp.ssl, "create_default_context") as context:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.geturl.return_value = rp.SOURCE_URL
            response.headers.get_content_type.return_value = "application/json"
            response.headers.get.return_value = "identity"
            response.read.return_value = b'{"markets":[]}'
            raw, _observed = rp.capture_public_markets()
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, rp.SOURCE_URL)
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(raw, b'{"markets":[]}')
            context.assert_called_once_with()
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], rp._NoRedirect)

    def test_redirect_is_not_followed(self):
        with self.assertRaises(rp.CaptureRefused):
            rp._NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://example.invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
