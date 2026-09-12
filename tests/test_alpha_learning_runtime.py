import json
import os
import tempfile
import unittest

from alpha_learning_runtime import (learning_snapshot, memory_context,
                                    write_learning_report)


class FakeLedger:
    def resolved(self):
        return [
            {
                "prediction_id": "p1",
                "contract_id": "c1",
                "market_class": "macro",
                "actual_outcome": 1,
                "invalidated": False,
                "snapshot": {"yes_ask": .50, "no_ask": .50,
                             "market_class": "macro"},
                "per_model": {
                    "gpt-astra-pro-max": {"p_yes": .80},
                    "atlas_quant": {"p_yes": .55},
                },
            },
            {
                "prediction_id": "p2",
                "contract_id": "c2",
                "market_class": "macro",
                "actual_outcome": 0,
                "invalidated": False,
                "snapshot": {"yes_ask": .45, "no_ask": .55,
                             "market_class": "macro"},
                "per_model": {
                    "gpt-astra-pro-max": {"p_yes": .82},
                    "atlas_quant": {"p_yes": .52},
                },
            },
        ]

    # RA-13: learning reads QUALIFIED settlements only. This double stands in
    # for a ledger whose settlements all came through the verified ingest
    # path, which is what these cases are about -- the arithmetic and the
    # shadow-only shape of the report, not the qualification rule. The
    # qualification rule itself is asserted against the real ledger in
    # `tests/test_astra_v4_remediation.py::RA13_*`.

    def qualified_resolved(self):
        return [dict(r, settlement_qualified=True) for r in self.resolved()]

    def unqualified_resolved(self):
        return []


class LearningRuntimeTests(unittest.TestCase):
    def test_snapshot_is_shadow_only(self):
        report = learning_snapshot(FakeLedger(), subscription_cost_usd=100)
        self.assertEqual(report["mode"], "SHADOW_ONLY")
        self.assertFalse(report["broker_authority"])
        self.assertEqual(report["astra"]["samples"], 2)
        self.assertEqual(report["learning_version"], "astra-alpha-learning-v1")
        self.assertNotEqual(
            report["astra_vs_baseline"]["baseline_pnl_usd"], 0.0,
            "default baseline selector must match the real atlas_quant provider")

    def test_report_is_durable_json(self):
        with tempfile.TemporaryDirectory() as d:
            report = write_learning_report(FakeLedger(), d,
                                           subscription_cost_usd=100)
            path = os.path.join(d, "alpha_learning_report.json")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                disk = json.load(fh)
            self.assertEqual(disk["astra"]["samples"], 2)
            self.assertEqual(disk["generated_at"], report["generated_at"])
            self.assertFalse(any(name.startswith(".alpha-learning-")
                                 for name in os.listdir(d)))

    def test_memory_context_contains_only_prior_cases(self):
        payload = json.loads(memory_context(FakeLedger(),
                                            market_class="macro", limit=1))
        self.assertEqual(payload["purpose"], "forecast_calibration_memory")
        self.assertEqual(len(payload["cases"]), 1)
        self.assertNotIn("side", payload)
        self.assertNotIn("size", payload)


if __name__ == "__main__":
    unittest.main()
