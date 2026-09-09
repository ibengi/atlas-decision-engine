# -*- coding: utf-8 -*-
"""Alpha Gateway phase 2, sections 4 and 5 — pricing and cost budgets.

THE INVARIANTS
    1. An unpriced model is NOT called. An unpriced call costs zero, so
       every cap below would be unenforceable against it and "daily limit"
       would silently mean "unlimited".
    2. Budget exhaustion means NO PROVIDER CALL, and it is its own state.
       A billing limit must never become a fabricated probability, and it
       must be distinguishable from a model that had no opinion.
    3. Enough raw usage is stored to recompute costs when prices change, so
       today's prices are never baked into yesterday's conclusions.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider                    # noqa: E402

from alpha_cost import (REASON_BUDGET, REASON_UNPRICED,       # noqa: E402
                        BudgetGuard, BudgetLedger, PricingTable, recost)
from alpha_gateway import STATE_BUDGET_EXHAUSTED, AlphaGateway  # noqa: E402
from alpha_ledger import AlphaLedger                          # noqa: E402
from config import CFG                                        # noqa: E402


class BudgetCase(AlphaCase):

    def pricing(self, rates=(3.0, 15.0), models=None, version="test-1", **kw):
        from _alpha import write_pricing
        return PricingTable(write_pricing(
            os.path.join(self._tmp, "pricing.json"), rates=rates,
            models=models or ("grok", "gemini", "openai", "atlas_quant"),
            version=version, **kw))

    def guard(self, **kw):
        return BudgetGuard(pricing=kw.pop("pricing", None) or self.pricing(),
                           ledger=BudgetLedger(
                               os.path.join(self._tmp, "budget.jsonl")))


class UnpricedModelsAreNotCalled(BudgetCase):

    def test_a_model_with_no_rates_is_refused(self):
        table = self.pricing(rates=(None, None))
        verdict = self.guard(pricing=table).check("grok", "grok")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], REASON_UNPRICED)
        self.assertIn("unenforceable", verdict["detail"])

    def test_a_model_absent_from_the_table_is_refused(self):
        verdict = self.guard().check("newvendor", "newmodel")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], REASON_UNPRICED)

    def test_the_refusal_can_be_overridden_deliberately(self):
        table = self.pricing(rates=(None, None))
        with patch.object(CFG, "ALPHA_ALLOW_UNPRICED_CALLS", True):
            self.assertTrue(self.guard(pricing=table).check("grok", "grok")["allowed"])

    def test_zero_is_a_price_and_unknown_is_not(self):
        """`atlas_quant` is genuinely free; a null rate is unknown."""
        priced = self.pricing(rates=(0.0, 0.0))
        self.assertTrue(self.guard(pricing=priced).check("grok", "grok")["allowed"])
        unknown = self.pricing(rates=(None, None))
        self.assertFalse(self.guard(pricing=unknown).check("grok", "grok")["allowed"])

    def test_a_missing_pricing_file_refuses_everything(self):
        table = PricingTable(os.path.join(self._tmp, "absent.json"))
        self.assertFalse(table.loaded)
        verdict = self.guard(pricing=table).check("grok", "grok")
        self.assertFalse(verdict["allowed"])


class CapsAreEnforcedBeforeTheCall(BudgetCase):

    def test_the_per_analysis_cap_stops_the_next_provider(self):
        """Spend accumulates WITHIN one analysis: the fourth provider is
        refused because the first three have already committed the budget."""
        guard = self.guard()
        worst_case = guard.pricing.estimate("grok", "grok")["api_cost_usd"]
        self.assertGreater(worst_case, 0.0)
        with patch.object(CFG, "ALPHA_MAX_COST_PER_ANALYSIS_USD",
                          worst_case * 1.5):
            first = guard.check("grok", "grok", analysis_spent_usd=0.0)
            self.assertTrue(first["allowed"])
            later = guard.check("gemini", "gemini",
                                analysis_spent_usd=worst_case)
            self.assertFalse(later["allowed"])
            self.assertEqual(later["reason"], REASON_BUDGET)
            self.assertIn("per-analysis", later["detail"])

    def test_the_estimate_is_the_worst_case_not_an_average(self):
        """Section 5: the refusal must be made on the largest amount the
        call could cost, or a cap is breached by exactly the calls it exists
        to stop."""
        guard = self.guard()
        flat = guard.pricing.estimate("grok", "grok")
        long_prompt = guard.pricing.estimate("grok", "grok",
                                             prompt_chars=200_000)
        self.assertGreater(long_prompt["api_cost_usd"], flat["api_cost_usd"])
        self.assertTrue(flat["worst_case"])
        self.assertEqual(flat["output_tokens"],
                         int(CFG.ALPHA_MAX_OUTPUT_TOKENS))

    def test_the_provider_hourly_cap_is_per_provider(self):
        guard = self.guard()
        for _ in range(40):
            guard.ledger.record({"provider": "grok", "api_cost_usd": 0.05})
        with patch.object(CFG, "ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD", 1.0):
            self.assertFalse(guard.check("grok", "grok")["allowed"])
            self.assertTrue(guard.check("gemini", "gemini")["allowed"])

    def test_the_daily_cap_counts_every_provider(self):
        guard = self.guard()
        for provider in ("grok", "gemini", "openai"):
            guard.ledger.record({"provider": provider, "api_cost_usd": 5.0})
        # The hourly cap is raised out of the way: the point here is that
        # the DAILY total sums across providers, and the first cap hit wins.
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 10.0), \
             patch.object(CFG, "ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD", 100.0):
            verdict = guard.check("grok", "grok")
            self.assertFalse(verdict["allowed"])
            self.assertIn("daily", verdict["detail"])

    def test_a_zero_cap_disables_that_limit(self):
        guard = self.guard()
        guard.ledger.record({"provider": "grok", "api_cost_usd": 1000.0})
        with patch.object(CFG, "ALPHA_MAX_PROVIDER_COST_PER_HOUR_USD", 0.0), \
             patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 0.0):
            self.assertTrue(guard.check("grok", "grok")["allowed"])

    def test_spend_survives_a_restart(self):
        """An in-memory counter would let a restart reset the daily cap."""
        guard = self.guard()
        guard.ledger.record({"provider": "grok", "api_cost_usd": 9.99})
        fresh = BudgetGuard(pricing=guard.pricing,
                            ledger=BudgetLedger(guard.ledger.path))
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 10.0):
            self.assertFalse(fresh.check("grok", "grok")["allowed"])

    def test_an_unreadable_ledger_is_treated_as_exhausted(self):
        """We cannot prove we are under budget, so we are not."""
        guard = self.guard()
        with patch.object(BudgetLedger, "rows",
                          side_effect=RuntimeError("ledger unreadable")):
            verdict = guard.check("grok", "grok")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], REASON_BUDGET)


class ExhaustionIsNotAProbability(BudgetCase):

    def test_no_provider_is_called_when_the_budget_is_gone(self):
        providers = self.agreeing_providers()
        guard = self.guard()
        guard.ledger.record({"provider": "grok", "api_cost_usd": 1000.0})
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            opportunity = AlphaGateway(providers=providers,
                                       ledger=AlphaLedger()).analyze(
                self.snapshot(),
                gate=lambda p: guard.check(p.name, p.model))
        self.assertTrue(all(p.calls == 0 for p in providers),
                        "a provider was called past the budget")
        self.assertEqual(opportunity["state"], STATE_BUDGET_EXHAUSTED)
        self.assertIsNone(opportunity["p_meta"])
        self.assertNotEqual(opportunity["p_meta"], 0.5)
        self.assertIs(opportunity["executed"], False)

    def test_exhaustion_is_distinguishable_from_no_opinion(self):
        """INSUFFICIENT_DATA blames the models; BUDGET_EXHAUSTED does not."""
        broke = self.guard()
        broke.ledger.record({"provider": "grok", "api_cost_usd": 1000.0})
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            exhausted = AlphaGateway(
                providers=self.agreeing_providers(),
                ledger=AlphaLedger()).analyze(
                    self.snapshot(), record=False,
                    gate=lambda p: broke.check(p.name, p.model))
        silent = AlphaGateway(
            providers=[FakeProvider("grok", error="down")],
            ledger=AlphaLedger()).analyze(self.snapshot(), record=False)
        self.assertEqual(exhausted["state"], STATE_BUDGET_EXHAUSTED)
        self.assertEqual(silent["state"], "INSUFFICIENT_DATA")

    def test_a_gate_that_raises_refuses_rather_than_fails_open(self):
        providers = self.agreeing_providers()

        def broken_gate(provider):
            raise RuntimeError("budget service down")
        opportunity = AlphaGateway(providers=providers,
                                   ledger=AlphaLedger()).analyze(
            self.snapshot(), record=False, gate=broken_gate)
        self.assertTrue(all(p.calls == 0 for p in providers))
        self.assertEqual(opportunity["state"], STATE_BUDGET_EXHAUSTED)

    def test_a_partial_budget_still_uses_the_providers_it_can_afford(self):
        providers = self.agreeing_providers()
        allowed = {"grok", "gemini"}
        opportunity = AlphaGateway(providers=providers,
                                   ledger=AlphaLedger()).analyze(
            self.snapshot(), record=False,
            gate=lambda p: {"allowed": p.name in allowed,
                            "reason": REASON_BUDGET,
                            "detail": "daily cap", "estimated_cost_usd": 0.0})
        self.assertEqual(set(opportunity["per_model"]), allowed)
        self.assertIsNotNone(opportunity["p_meta"])


class UsageIsStoredWellEnoughToRecompute(BudgetCase):

    def test_a_cost_row_records_the_pricing_version_and_time(self):
        row = self.pricing(version="2026-09-a").price("grok", "grok",
                                                      1_000_000, 1_000_000)
        self.assertTrue(row["cost_priced"])
        self.assertAlmostEqual(row["api_cost_usd"], 18.0, places=6)
        self.assertEqual(row["pricing_version"], "2026-09-a")
        self.assertEqual(row["pricing_asof"], "2026-01-01T00:00:00+00:00")
        self.assertTrue(row["priced_at"])
        self.assertEqual(row["input_per_mtok"], 3.0)

    def test_tool_and_search_usage_are_recorded(self):
        from alpha_providers import _cost_row
        with patch("alpha_providers.pricing_table", return_value=self.pricing()):
            row = _cost_row("grok", "grok", 100, 50, tool_calls=3,
                            search_queries=2, latency_ms=420)
        self.assertEqual(row["tool_calls"], 3)
        self.assertEqual(row["search_queries"], 2)
        self.assertEqual(row["latency_ms"], 420)

    def test_history_can_be_recosted_under_new_prices(self):
        """Section 4: today's prices must not be baked into yesterday's
        conclusions."""
        rows = [{"provider": "grok", "model": "grok",
                 "input_tokens": 1_000_000, "output_tokens": 1_000_000}]
        cheap = recost(rows, self.pricing(rates=(1.0, 2.0), version="old"))
        dear = recost(rows, self.pricing(rates=(10.0, 20.0), version="new"))
        self.assertAlmostEqual(cheap["total_usd"], 3.0, places=6)
        self.assertAlmostEqual(dear["total_usd"], 30.0, places=6)
        self.assertTrue(cheap["complete"])
        self.assertEqual(cheap["pricing_version"], "old")

    def test_recosting_reports_what_it_could_not_price(self):
        rows = [{"provider": "mystery", "model": "x",
                 "input_tokens": 100, "output_tokens": 100}]
        result = recost(rows, self.pricing())
        self.assertFalse(result["complete"])
        self.assertEqual(result["unpriced_models"], ["mystery/x"])
        self.assertEqual(result["total_usd"], 0.0)

    def test_actual_spend_is_recorded_after_the_call(self):
        guard = self.guard()
        guard.record_actual({"provider": "grok", "model": "grok",
                             "input_tokens": 1000, "output_tokens": 500,
                             "api_cost_usd": 0.0105, "cost_priced": True,
                             "pricing_version": "test-1", "latency_ms": 900,
                             "outcome": "VALID"})
        rows = guard.ledger.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 1000)
        self.assertEqual(rows[0]["pricing_version"], "test-1")
        self.assertAlmostEqual(guard.ledger.spent_today(), 0.0105, places=6)


if __name__ == "__main__":
    import unittest
    unittest.main()
