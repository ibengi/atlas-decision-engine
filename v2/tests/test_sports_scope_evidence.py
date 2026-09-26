"""Synthetic trust pins only; never provider or live scope evidence."""
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from atlas_v2 import sports_scope_evidence as e, sports_probe as p
from atlas_v2.domain import Refused, canonical

AT = "2026-09-26T17:00:00Z"
KEY = "synthetic-sports-key"


def manifest(**changes):
    return dict({"schema": "atlas-sports-provider-scope/1", "provider": "Kalshi",
        "source_url": "https://kalshi.com/account/profile", "artifact_sha256": "a"*64,
        "origin_receipt_sha256": "b"*64, "key_id_sha256": e.key_fingerprint(KEY),
        "scopes": ["read"], "write_allowed": False, "trade_allowed": False,
        "transfer_allowed": False, "observed_at": "2026-09-26T16:00:00Z",
        "expires_at": "2026-09-27T16:00:00Z"}, **changes)


class ScopeEvidenceTests(unittest.TestCase):
    def verify(self, body, key=KEY, at=AT, trusted=True):
        pins = {hashlib.sha256(canonical(body)).hexdigest()} if trusted else set()
        with patch.object(e, "REVIEWED_EVIDENCE_SHA256", frozenset(pins)):
            return e.verify_provider_evidence(json.dumps(body), key, at)

    def test_pinned_manifest_and_no_self_authorization(self):
        body = manifest()
        result = self.verify(body)
        self.assertEqual(result["scopes"], ["read"])
        self.assertEqual(result["evidence_manifest_sha256"], hashlib.sha256(canonical(body)).hexdigest())
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(e.REVIEWED_EVIDENCE_SHA256, frozenset())
        with self.assertRaisesRegex(Refused, "UNTRUSTED"): self.verify(body, trusted=False)
        pinned = hashlib.sha256(canonical(body)).hexdigest()
        body["artifact_sha256"] = "c"*64
        with patch.object(e, "REVIEWED_EVIDENCE_SHA256", {pinned}):
            with self.assertRaises(Refused): e.verify_provider_evidence(json.dumps(body), KEY, AT)

    def test_installed_key_binding(self):
        with self.assertRaisesRegex(Refused, "KEY_MISMATCH"): self.verify(manifest(), key="other-key")

    def test_permissions(self):
        for change in ({"scopes": ["read", "write"]}, {"scopes": []}, {"write_allowed": True},
                       {"trade_allowed": True}, {"transfer_allowed": True}, {"write_allowed": 0}):
            with self.subTest(change=change), self.assertRaises(Refused): self.verify(manifest(**change))

    def test_provider_and_hashes(self):
        for change in ({"provider": "User"}, {"source_url": "https://kalshi.com.evil/account/profile"},
                       {"source_url": "https://kalshi.com/account/profile?copied=true"},
                       {"artifact_sha256": "x"}, {"origin_receipt_sha256": None}, {"key_id_sha256": "x"}):
            with self.subTest(change=change), self.assertRaises(Refused): self.verify(manifest(**change))

    def test_expiry_future_and_utc(self):
        for change in ({"expires_at": AT}, {"observed_at": "2026-09-26T18:00:00Z"},
                       {"expires_at": "2026-09-28T16:00:00Z"}, {"observed_at": "2026-09-26T16:00:00"}):
            with self.subTest(change=change), self.assertRaises(Refused): self.verify(manifest(**change))

    def test_malformed_secret_plaintext_and_bounds(self):
        for encoded in (None, "READ ONLY confirmed by user", "x"*8193, "[]",
                        '{"schema":1,"schema":2}', '{"private_key":"-----BEGIN PRIVATE KEY-----"}'):
            with self.subTest(encoded=str(encoded)[:30]), self.assertRaises((Refused, ValueError)):
                e.verify_provider_evidence(encoded, KEY, AT)
        with self.assertRaises(Refused): self.verify(manifest(private_key="forbidden"))

    def call_scope(self, response=None, error=None, body=None):
        body = manifest() if body is None else body
        reader = Mock()
        reader.get.side_effect = error
        reader.get.return_value = response
        pins = {hashlib.sha256(canonical(body)).hexdigest()}
        with patch.object(e, "REVIEWED_EVIDENCE_SHA256", pins), patch.object(p, "now", return_value=AT):
            result = p.establish_scope(reader, SimpleNamespace(key_id=KEY), {e.ENV_NAME: json.dumps(body)})
        reader.get.assert_called_once()
        return result

    def test_fallback_only_when_unavailable(self):
        for error in ("REST_HTTP_403", "REST_HTTP_404", "REST_CONNECTION_FAILED"):
            result = self.call_scope(error=Refused(error))
            self.assertEqual(result["authority"], "release_pinned_provider_evidence")
        for scopes in ({}, {"scopes": None}):
            result = self.call_scope({"api_keys": [{"api_key_id": KEY, **scopes}]})
            self.assertEqual(result["api_scope_unavailable_reason"], "SCOPE_FIELD_UNAVAILABLE")
        for error in ("REST_HTTP_401", "REST_INCOMPLETE", "REST_BODY_REFUSED", "REST_DEADLINE"):
            with self.assertRaises(Refused): self.call_scope(error=Refused(error))

    def test_api_conflicts_cannot_be_overridden(self):
        for scopes in ([], ["write"], ["read", "write::trade"], "read"):
            with self.assertRaisesRegex(Refused, "CONFLICT"):
                self.call_scope({"api_keys": [{"api_key_id": KEY, "scopes": scopes}]})
        for records in ([], [{"api_key_id": "other"}], [{"api_key_id": KEY}]*2,
                        [{"api_key_id": KEY, "revoked": True}], [{"api_key_id": KEY, "disabled": True}], [None]):
            with self.assertRaises(Refused): self.call_scope({"api_keys": records})
        for field in ("partial", "errors", "cursor"):
            with self.assertRaises(Refused):
                self.call_scope({"api_keys": [{"api_key_id": KEY}], field: True})

    def test_api_read_precedence_and_missing_evidence_refusal(self):
        reader = Mock()
        reader.get.return_value = {"api_keys": [{"api_key_id": KEY, "scopes": ["read"]}]}
        with patch.object(p, "verify_provider_evidence", side_effect=AssertionError("fallback must not run")):
            self.assertEqual(p.establish_scope(reader, SimpleNamespace(key_id=KEY), {})["scopes"], ["read"])
        reader.get.side_effect = Refused("REST_HTTP_403")
        with self.assertRaisesRegex(Refused, "MISSING"):
            p.establish_scope(reader, SimpleNamespace(key_id=KEY), {})

    def test_startup_uses_fallback_before_membership(self):
        # Uses real run_probe path but no credentials, network, financial client or live data.
        import tempfile
        from test_sports_probe import credentials
        creds, env = credentials()
        body = manifest(key_id_sha256=e.key_fingerprint(creds.key_id))
        env[e.ENV_NAME] = json.dumps(body)
        with tempfile.TemporaryDirectory() as d, patch.object(p.Reader, "get", side_effect=Refused("REST_HTTP_403")), \
             patch.object(p, "now", return_value=AT), patch.object(e, "REVIEWED_EVIDENCE_SHA256", {hashlib.sha256(canonical(body)).hexdigest()}), \
             patch.object(p, "membership", side_effect=Refused("TEST_STOP_AFTER_SCOPE")) as membership:
            result = p.run_probe(d, "a"*40, env)
            self.assertTrue(result["scope_verified"])
            self.assertEqual(result["reason"], "TEST_STOP_AFTER_SCOPE")
            self.assertEqual(result["broker_writes"], 0)
            self.assertEqual(result["real_orders_submitted"], 0)
            membership.assert_called_once()
