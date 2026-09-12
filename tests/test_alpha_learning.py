import unittest

from alpha_learning import (build_memory, classify_error, incremental_value,
                            learning_report, score_model, similar_cases)


def row(pid, market_class, p_astra, p_quant, outcome, yes=.40, no=.60,
        invalidated=False):
    return {
        "prediction_id": pid,
        "contract_id": f"C-{pid}",
        "market_class": market_class,
        "actual_outcome": outcome,
        "invalidated": invalidated,
        "snapshot": {"yes_ask": yes, "no_ask": no,
                     "market_class": market_class},
        "per_model": {
            "gpt-astra-pro-max": {"p_yes": p_astra},
            "atlasquant-v1": {"p_yes": p_quant},
        },
    }


class FakeLedger:
    def __init__(self, rows):
        self._rows = rows

    def resolved(self):
        return list(self._rows)

    # RA-13: learning reads QUALIFIED settlements only. This double stands in
    # for a ledger whose settlements all came through the verified ingest
    # path, which is what these cases are about -- the arithmetic and the
    # shadow-only shape of the report, not the qualification rule. The
    # qualification rule itself is asserted against the real ledger in
    # `tests/test_astra_v4_remediation.py::RA13_*`.
    def qualified_resolved(self):
        return [dict(r, settlement_qualified=True) for r in self._rows]

    def unqualified_resolved(self):
        return []


class AlphaLearningTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("1", "macro", .80, .55, 1),
            row("2", "macro", .75, .55, 0),
            row("3", "crypto", .60, .52, 1),
            row("4", "macro", .99, .99, 0, invalidated=True),
        ]

    def test_score_model_ignores_invalidated(self):
        score = score_model(self.rows, "astra")
        self.assertEqual(score["samples"], 3)
        self.assertIsNotNone(score["brier"])
        self.assertIn("gpt-astra-pro-max", score["models_seen"])

    def test_category_score(self):
        score = score_model(self.rows, "astra", market_class="macro")
        self.assertEqual(score["samples"], 2)

    def test_error_taxonomy(self):
        self.assertEqual(classify_error(.90, 0), "overconfident_wrong")
        self.assertEqual(classify_error(.80, 1), "strong_correct")

    def test_memory_surfaces_mistakes_first(self):
        memory = build_memory(self.rows, "astra")
        cases = similar_cases(memory, market_class="macro", limit=2)
        self.assertEqual(cases[0]["error_class"], "confident_wrong")
        self.assertEqual(len(cases), 2)

    def test_incremental_value_accounts_for_subscription(self):
        report = incremental_value(self.rows, target_selector="astra",
                                   baseline_selector="atlasquant",
                                   subscription_cost_usd=100.0)
        self.assertIn("net_value_after_subscription_usd", report)
        self.assertEqual(report["subscription_cost_usd"], 100.0)

    def test_learning_report_is_shadow_only(self):
        report = learning_report(FakeLedger(self.rows), subscription_cost_usd=100)
        self.assertEqual(report["mode"], "SHADOW_ONLY")
        self.assertFalse(report["broker_authority"])
        self.assertEqual(report["astra"]["samples"], 3)


if __name__ == "__main__":
    unittest.main()
