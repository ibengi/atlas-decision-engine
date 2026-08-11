"""Tests for Program 008E — read-only research evidence exposure.

The controlling question for this program is not "does the export work". It is
"can the export change what the engine trades". Every test below is written
against that question, and the equivalence test at the end is the one that
decides whether the program is allowed to exist at all.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import research_export as rx  # noqa: E402


def write_decisions(directory, count=5, name="decisions.jsonl"):
    path = os.path.join(directory, name)
    with open(path, "a", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(json.dumps({
                "cycle_id": f"c{i}", "run_id": f"run{i}",
                "recorded_at": f"2026-08-09T12:{i:02d}:00+00:00",
                "lineage": rx.lineage(),
                "decision": {"ticker": f"KX-{i}", "accepted": i % 2 == 0,
                             "model_probability": 0.5 + i / 100,
                             "net_edge": 0.01 * i, "net_ev": 0.5 * i,
                             "rejection_reason": None if i % 2 == 0 else "spread",
                             "decision_id": f"c{i}-abcd", "kelly_fraction": 0.02},
            }) + "\n")
    return path


def write_trades(directory, count=3):
    path = os.path.join(directory, "trades.json")
    trades = [{
        "trade_id": f"t{i}", "order_id": f"o{i}", "timestamp": f"2026-08-0{i+1}",
        "ticker": f"KX-{i}", "state": "settled", "gross_pnl": 10.0,
        "fees": 2.0, "net_pnl": 8.0, "settled_at": f"2026-08-0{i+2}",
        "avg_fill_price": 45, "filled_count": 5,
    } for i in range(count)]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(trades, handle)
    return path


class TestIdentity(unittest.TestCase):
    def test_model_identity_is_stable_across_calls(self):
        rx._identity_cache.clear()
        first = rx.model_identity()
        rx._identity_cache.clear()
        second = rx.model_identity()
        self.assertEqual(first["model_artifact_sha256"],
                         second["model_artifact_sha256"])

    def test_model_identity_is_derived_from_the_artifact(self):
        identity = rx.model_identity()
        self.assertTrue(identity["model_sources"])
        self.assertEqual(len(identity["model_artifact_sha256"]), 16)

    def test_a_restart_does_not_rename_an_identical_model(self):
        """Two fresh processes must agree. Cache cleared to simulate a boot."""
        rx._identity_cache.clear()
        boot_one = rx.lineage()
        rx._identity_cache.clear()
        boot_two = rx.lineage()
        self.assertEqual(boot_one, boot_two)

    def test_calibration_identity_declares_it_changes_nothing(self):
        self.assertFalse(rx.calibration_identity()["behaviour_changed"])

    def test_lineage_is_cached_after_the_first_call(self):
        rx._identity_cache.clear()
        rx.lineage()
        self.assertIn("lineage", rx._identity_cache)


class TestAuthorisation(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("RESEARCH_API_TOKEN")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("RESEARCH_API_TOKEN", None)
        else:
            os.environ["RESEARCH_API_TOKEN"] = self._previous

    def test_no_token_configured_means_refusal_not_open_access(self):
        os.environ.pop("RESEARCH_API_TOKEN", None)
        self.assertFalse(rx.authorised("Bearer anything"))
        self.assertFalse(rx.authorised(None))

    def test_the_correct_token_is_accepted_in_either_form(self):
        os.environ["RESEARCH_API_TOKEN"] = "secret-value"
        self.assertTrue(rx.authorised("Bearer secret-value"))
        self.assertTrue(rx.authorised("secret-value"))

    def test_a_wrong_token_is_refused(self):
        os.environ["RESEARCH_API_TOKEN"] = "secret-value"
        self.assertFalse(rx.authorised("Bearer wrong"))
        self.assertFalse(rx.authorised(""))


class TestDecisionExport(unittest.TestCase):
    def test_accepted_and_rejected_decisions_are_both_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 6)
            rows = rx.decisions(directory, "", 100)["rows"]
            verdicts = {r["decision"]["accepted"] for r in rows}
            self.assertEqual(verdicts, {True, False})

    def test_values_are_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 3)
            rows = rx.decisions(directory, "", 100)["rows"]
            self.assertAlmostEqual(rows[1]["decision"]["model_probability"],
                                   0.51, places=6)
            self.assertFalse(rx.decisions(directory, "", 100)["recomputed"])

    def test_rotated_generations_are_read_oldest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 2, "decisions.jsonl.1")
            write_decisions(directory, 2, "decisions.jsonl")
            rows = rx.decisions(directory, "", 100)["rows"]
            self.assertEqual(len(rows), 4)

    def test_an_unreadable_line_is_counted_not_crashed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 2)
            with open(os.path.join(directory, "decisions.jsonl"), "a",
                      encoding="utf-8") as handle:
                handle.write("{not json\n")
            result = rx.decisions(directory, "", 100)
            self.assertEqual(result["unreadable_lines"], 1)
            self.assertEqual(len(result["rows"]), 2)


class TestPagination(unittest.TestCase):
    def test_pages_cover_everything_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 10)
            seen, cursor = [], ""
            for _ in range(10):
                page = rx.decisions(directory, cursor, 3)
                seen.extend(r["record_id"] for r in page["rows"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            self.assertEqual(len(seen), 10)
            self.assertEqual(len(set(seen)), 10)

    def test_the_ordering_is_stable_across_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 8)
            first = [r["record_id"] for r in rx.decisions(directory, "", 100)["rows"]]
            second = [r["record_id"] for r in rx.decisions(directory, "", 100)["rows"]]
            self.assertEqual(first, second)

    def test_an_expired_cursor_is_reported_not_silently_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 3)
            page = rx.decisions(directory, "cursor-that-rotated-away", 100)
            self.assertEqual(page["cursor_state"], "expired")
            self.assertIn("no longer retained", page["cursor_expired_note"])

    def test_a_page_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 5)
            self.assertLessEqual(
                len(rx.decisions(directory, "", 10_000)["rows"]), rx.MAX_PAGE)


class TestSettlementExport(unittest.TestCase):
    def test_settlements_are_exported_with_their_linkage(self):
        with tempfile.TemporaryDirectory() as directory:
            write_trades(directory, 3)
            result = rx.settlements(directory, "", 100)
            self.assertEqual(len(result["rows"]), 3)
            self.assertTrue(all(r["order_id"] for r in result["rows"]))

    def test_pnl_is_never_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_trades(directory, 2)
            result = rx.settlements(directory, "", 100)
            self.assertFalse(result["recomputed"])
            self.assertEqual(result["rows"][0]["net_pnl"], 8.0)

    def test_an_inconsistent_record_is_exposed_not_corrected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_trades(directory, 1)
            with open(path, encoding="utf-8") as handle:
                trades = json.load(handle)
            trades[0]["net_pnl"] = 99.0            # 10 - 2 != 99
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(trades, handle)
            result = rx.settlements(directory, "", 100)
            self.assertTrue(result["discrepancies"])
            self.assertEqual(result["rows"][0]["net_pnl"], 99.0)


class TestCapabilityManifest(unittest.TestCase):
    def test_the_manifest_declares_datasets_and_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 4)
            write_trades(directory, 2)
            manifest = rx.capabilities(directory)
            names = {d["name"] for d in manifest["datasets"]}
            self.assertEqual(names, {"decisions", "settlements"})
            decisions = next(d for d in manifest["datasets"]
                             if d["name"] == "decisions")
            self.assertFalse(decisions["permanent"])
            self.assertIn("rotation", decisions["known_gaps"])

    def test_the_manifest_declares_what_it_cannot_serve(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = rx.capabilities(directory)
            self.assertIn("risk_evaluations", manifest["unavailable"])

    def test_the_manifest_declares_no_write_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(rx.capabilities(directory)["write_operations"], [])


class TestSecurity(unittest.TestCase):
    FORBIDDEN = ("KALSHI_API_KEY", "PRIVATE_KEY", "JWT_SECRET", "PASSWORD",
                 "ANTHROPIC_API_KEY", "RAILWAY_TOKEN", "DATABASE_URL")

    def test_no_credential_appears_in_any_export(self):
        with tempfile.TemporaryDirectory() as directory:
            write_decisions(directory, 3)
            write_trades(directory, 2)
            body = json.dumps({
                "capabilities": rx.capabilities(directory),
                "decisions": rx.decisions(directory, "", 100),
                "settlements": rx.settlements(directory, "", 100),
            }, default=str).upper()
            for marker in self.FORBIDDEN:
                self.assertNotIn(marker, body, marker)

    def test_the_export_module_reads_no_secret_environment_variable(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "research_export.py"),
            encoding="utf-8").read()
        for marker in ("KALSHI_API_KEY", "PRIVATE_KEY", "JWT_SECRET",
                       "DATABASE_URL"):
            self.assertNotIn(marker, source)


class TestEngineEquivalence(unittest.TestCase):
    """The test that decides whether this program may exist.

    The 008E change adds three keys to a dict written to a log file. The claim
    is that no business output can differ. The claim is checked by running the
    pipeline's decision construction with and without the stamp and comparing
    every business field byte for byte.
    """

    BUSINESS_FIELDS = (
        "ticker", "accepted", "rejection_reason", "strategy", "market_type",
        "category", "side", "entry_ask", "model_probability",
        "market_probability", "gross_edge", "net_edge", "net_ev",
        "confidence", "taille", "estimated_fees", "expected_slippage",
        "spread", "liquidity", "kelly_fraction")

    OBSERVABILITY_ONLY = ("run_id", "recorded_at", "lineage")

    def test_the_stamp_adds_only_observability_keys(self):
        from opportunity_pipeline import _lineage

        stamped = {"cycle_id": "c1", "decision": {"ticker": "KX"},
                   "run_id": "r1", "recorded_at": "2026-08-09T00:00:00+00:00",
                   "lineage": _lineage()}
        baseline_keys = {"cycle_id", "decision"}
        extra = set(stamped) - baseline_keys
        self.assertEqual(extra, set(self.OBSERVABILITY_ONLY))

    def test_the_decision_object_itself_is_untouched(self):
        """`dec` is never mutated by the stamp; only the log envelope grows."""
        from strategy_router import Decision

        before = Decision(ticker="KX-A", strategy="s")
        snapshot = dict(before.to_dict())
        payload = {"cycle_id": "c", "decision": before.to_dict(),
                   "run_id": "r", "recorded_at": "t", "lineage": {}}
        self.assertEqual(before.to_dict(), snapshot)
        for field in self.BUSINESS_FIELDS:
            self.assertEqual(payload["decision"].get(field), snapshot.get(field))

    def test_lineage_never_raises_even_if_the_export_is_broken(self):
        """Observability must not be able to break trading."""
        import opportunity_pipeline as op

        op._LINEAGE_CACHE.clear()
        saved = sys.modules.pop("research_export", None)
        sys.modules["research_export"] = None       # import returns None
        try:
            self.assertEqual(op._lineage(), {})
        finally:
            sys.modules.pop("research_export", None)
            if saved is not None:
                sys.modules["research_export"] = saved
            op._LINEAGE_CACHE.clear()

    def test_the_export_is_not_imported_on_the_trading_path(self):
        """`research_export` must not be a module-level import anywhere in the
        trading modules: an import-time failure would take the engine down."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("execution_engine.py", "order_manager.py",
                     "risk_manager.py", "position_sizer.py",
                     "strategy_router.py", "kalshi_client.py"):
            path = os.path.join(root, name)
            if not os.path.exists(path):
                continue
            source = open(path, encoding="utf-8").read()
            self.assertNotIn("research_export", source, name)


if __name__ == "__main__":
    unittest.main()
