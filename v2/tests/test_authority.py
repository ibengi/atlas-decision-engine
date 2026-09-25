import base64
import json
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from atlas_v2.approval import verify_review
from atlas_v2.domain import Refused, canonical
from atlas_v2.service import release_identity, persistent_directory
from test_invariants import WithStore, H, quote
from atlas_v2.execution import reserve_shadow
from test_invariants import SCOPE


class ApprovalTests(unittest.TestCase):
    def test_persistent_directory_refuses_outside_relative_and_symlink_paths(self):
        with patch.dict("os.environ", {"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, clear=True):
            self.assertEqual(persistent_directory(), Path("/data/atlas-v2"))
            for bad in ("/tmp/atlas-v2", "atlas-v2", "/data/state5", "/data/../tmp/atlas-v2"):
                with self.subTest(path=bad), patch.dict("os.environ", {"ATLAS_V2_DATA_DIR": bad}), self.assertRaises(Refused):
                    persistent_directory()
            # Simulate an operator-created symlink at the permitted spelling.
            # Resolution is the OS trust boundary; do not alter the real /data.
            with patch.object(Path, "resolve", return_value=Path("/tmp/atlas-v2")), self.assertRaises(Refused):
                persistent_directory()
            with patch.dict("os.environ", {"RAILWAY_VOLUME_MOUNT_PATH": "/other"}), self.assertRaises(Refused):
                persistent_directory()

    def test_signed_review_is_bound_and_never_financial_authority(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        bindings = {k: H for k in ("source", "model", "features", "thresholds", "config", "dataset", "lock", "validation", "independent_review", "cost_receipts")}
        payload = {"purpose": "ATLAS_V2_INDEPENDENT_RESEARCH_REVIEW", "bindings": bindings,
                   "issued_at": "2026-09-25T00:00:00Z", "expires_at": "2026-09-26T00:00:00Z"}
        envelope = {"payload": payload, "signature": base64.b64encode(key.sign(canonical(payload))).decode()}
        result = verify_review(envelope, public, bindings, "2026-09-25T12:00:00Z")
        self.assertTrue(result["review_authenticated"])
        self.assertFalse(result["financial_authority"])
        for field in bindings:
            changed = dict(bindings, **{field: "b" * 64})
            with self.subTest(field=field), self.assertRaises(Refused):
                verify_review(envelope, public, changed, "2026-09-25T12:00:00Z")
        for badkey in (None, b"x" * 32):
            with self.assertRaises(Refused):
                verify_review(envelope, badkey, bindings, "2026-09-25T12:00:00Z")
        with self.assertRaises(Refused):
            verify_review(envelope, public, bindings, "2026-09-26T00:00:00Z")
        envelope["payload"]["expires_at"] = "2027-01-01T00:00:00Z"
        with self.assertRaises(Refused):
            verify_review(envelope, public, bindings, "2026-09-25T12:00:00Z")

    def test_deployment_lineage_refuses_old_main(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "atlas_v2").mkdir()
            Path(directory, "atlas_v2", "example.py").write_text("# fixture\n")
            Path(directory, "release.json").write_text(json.dumps({"sha": "a" * 40,
                "source_hashes": {"atlas_v2/example.py": hashlib.sha256(b"# fixture\n").hexdigest()}}))
            with patch.dict("os.environ", {"RAILWAY_GIT_COMMIT_SHA": "b" * 40}), self.assertRaises(Refused):
                release_identity(directory)
            with patch.dict("os.environ", {"RAILWAY_GIT_COMMIT_SHA": "a" * 40}):
                self.assertEqual(release_identity(directory)["sha"], "a" * 40)
                Path(directory, "atlas_v2", "example.py").write_text("# tampered\n")
                with self.assertRaises(Refused):
                    release_identity(directory)


class ReservationBudgetTests(WithStore):
    def test_market_reservations_cannot_reuse_same_cash(self):
        self.lock(); c = self.control(available_budget="0.50")
        reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], lambda: quote(ticker="A"), "0.65", "0.01", "0.01")
        with self.assertRaises(Refused):
            reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], lambda: quote(ticker="B"), "0.65", "0.01", "0.01")

    def test_position_ceiling_is_retained(self):
        self.lock(); c = self.control()
        for ticker in ("A", "B", "C"):
            reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], lambda: quote(ticker=ticker), "0.65", "0.01", "0.01")
        with self.assertRaises(Refused):
            reserve_shadow(self.store, SCOPE, "lock:hyp", c["hash"], lambda: quote(ticker="D"), "0.65", "0.01", "0.01")
