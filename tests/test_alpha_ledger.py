# -*- coding: utf-8 -*-
"""Alpha Gateway sections 12, 13, 14 and 21 — cost and calibration ledgers.

THE INVARIANTS
    1. A prediction is persisted BEFORE resolution and is never rewritten
       afterwards. A resolution is a SECOND row referring to it.
    2. Every model invocation records tokens, cost and latency -- including
       the ones that failed, because asking costs something too.
    3. A net-of-inference-cost figure is WITHHELD while token prices are
       unset, rather than reported from a placeholder zero.

WHY 1 IS THE ONE THAT MATTERS
    A prediction row updated in place after the outcome is known cannot be
    distinguished from one that was always right, and every calibration
    number derived from that file becomes unfalsifiable. This is the same
    discipline `continuity.py` applies to the equity ledger, after the same
    kind of audit finding.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider                    # noqa: E402

from alpha_gateway import AlphaGateway                        # noqa: E402
from alpha_ledger import (AlphaLedger, LedgerError,           # noqa: E402
                          score_prediction)
from config import CFG                                        # noqa: E402


class LedgerCase(AlphaCase):

    def gateway(self, providers=None, **kw):
        return AlphaGateway(providers=providers or self.agreeing_providers(),
                            ledger=AlphaLedger(), **kw)

    def one_prediction(self, **kw):
        snapshot = self.snapshot(**kw)
        return self.gateway().analyze(snapshot), snapshot


class PredictionsAreImmutable(LedgerCase):

    def test_a_prediction_is_persisted_before_resolution(self):
        opportunity, _ = self.one_prediction()
        ledger = AlphaLedger()
        row = ledger.find_prediction(opportunity["prediction_id"])
        self.assertIsNotNone(row)
        self.assertIsNone(row["actual_outcome"])
        self.assertEqual(row["kind"], "PREDICTION")

    def test_resolution_is_a_new_row_and_leaves_the_prediction_untouched(self):
        opportunity, _ = self.one_prediction()
        ledger = AlphaLedger()
        before = json.dumps(ledger.find_prediction(opportunity["prediction_id"]),
                            sort_keys=True)
        ledger.resolve(opportunity["prediction_id"], 1, source="kalshi")
        after = json.dumps(ledger.find_prediction(opportunity["prediction_id"]),
                           sort_keys=True)
        self.assertEqual(before, after,
                         "the prediction row was rewritten at resolution")
        resolution = ledger.find_resolution(opportunity["prediction_id"])
        self.assertEqual(resolution["actual_outcome"], 1)
        self.assertEqual(resolution["kind"], "RESOLUTION")

    def test_the_prediction_row_bytes_are_unchanged_after_resolution(self):
        """Byte-level, not field-level: an append-only file is only
        append-only if the earlier bytes are still there."""
        opportunity, _ = self.one_prediction()
        path = os.path.join(self._tmp, CFG.ALPHA_LEDGER_FILE)
        before = open(path, "rb").read()
        AlphaLedger().resolve(opportunity["prediction_id"], 0)
        after = open(path, "rb").read()
        self.assertTrue(after.startswith(before),
                        "the ledger was rewritten, not appended to")
        self.assertGreater(len(after), len(before))

    def test_a_second_resolution_is_refused(self):
        opportunity, _ = self.one_prediction()
        ledger = AlphaLedger()
        ledger.resolve(opportunity["prediction_id"], 1)
        with self.assertRaises(LedgerError):
            ledger.resolve(opportunity["prediction_id"], 0)

    def test_resolving_an_unknown_prediction_is_refused(self):
        with self.assertRaises(LedgerError):
            AlphaLedger().resolve("pred-does-not-exist", 1)

    def test_a_non_binary_outcome_is_refused(self):
        opportunity, _ = self.one_prediction()
        for outcome in (0.5, 2, -1, "YES", None):
            with self.subTest(outcome=outcome):
                with self.assertRaises(LedgerError):
                    AlphaLedger().resolve(opportunity["prediction_id"], outcome)

    def test_the_same_prediction_cannot_be_recorded_twice(self):
        opportunity, _ = self.one_prediction()
        with self.assertRaises(LedgerError):
            AlphaLedger().record_prediction(opportunity)

    def test_a_torn_last_row_does_not_hide_the_rows_before_it(self):
        opportunity, _ = self.one_prediction()
        path = os.path.join(self._tmp, CFG.ALPHA_LEDGER_FILE)
        with open(path, "a") as fh:
            fh.write('{"kind": "PREDICTION", "prediction_i')
        self.assertEqual(len(AlphaLedger().predictions()), 1)

    def test_a_corrupt_middle_row_is_skipped_not_treated_as_eof(self):
        """Stopping at a bad line would silently shorten the history, which
        is how a calibration number quietly improves."""
        self.one_prediction()
        path = os.path.join(self._tmp, CFG.ALPHA_LEDGER_FILE)
        rows = open(path).read().splitlines()
        with open(path, "w") as fh:
            fh.write(rows[0] + "\n" + "GARBAGE\n" + rows[0].replace(
                '"prediction_id":"pred-', '"prediction_id":"pred-x') + "\n")
        self.assertEqual(len(AlphaLedger().predictions()), 2)


class CostsAreRecorded(LedgerCase):

    def test_every_invocation_is_costed_including_the_failures(self):
        """Section 12: a failed call still consumed a deadline, and often
        still consumed tokens."""
        providers = [FakeProvider("grok"),
                     FakeProvider("gemini", error="HTTP 500"),
                     FakeProvider("openai", behaviour="not json"),
                     FakeProvider("atlas_quant")]
        self.gateway(providers).analyze(self.snapshot())
        rows = AlphaLedger().cost_log.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["provider"] for r in rows},
                         {"grok", "gemini", "openai", "atlas_quant"})
        for row in rows:
            for field in ("provider", "model", "input_tokens", "output_tokens",
                          "api_cost_usd", "latency_ms", "outcome"):
                self.assertIn(field, row)
        outcomes = {r["provider"]: r["outcome"] for r in rows}
        self.assertEqual(outcomes["gemini"], "EXCLUDED")
        self.assertEqual(outcomes["grok"], "VALID")

    def test_latency_is_recorded_per_model(self):
        providers = [FakeProvider("grok", latency_ms=340),
                     FakeProvider("gemini", latency_ms=1250)]
        opportunity = self.gateway(providers).analyze(self.snapshot())
        self.assertEqual(opportunity["model_latency_ms"]["grok"], 340)
        self.assertEqual(opportunity["model_latency_ms"]["gemini"], 1250)
        rows = {r["provider"]: r for r in AlphaLedger().cost_log.rows()}
        self.assertEqual(rows["gemini"]["latency_ms"], 1250)

    def test_token_counts_are_recorded(self):
        providers = [FakeProvider("grok", tokens=(1234, 567))]
        self.gateway(providers).analyze(self.snapshot())
        row = AlphaLedger().cost_log.rows()[0]
        self.assertEqual(row["input_tokens"], 1234)
        self.assertEqual(row["output_tokens"], 567)

    def test_unpriced_tokens_are_marked_unpriced(self):
        """A made-up price would silently answer the one question this
        subsystem exists to ask.

        The shipped `alpha_pricing.json` leaves every vendor rate null, so
        the three LLM rows are unpriced. `atlas_quant` is priced at zero on
        purpose -- it is in-process and genuinely free, which is a different
        statement from "we do not know what it costs" -- and the distinction
        is exactly what this asserts.
        """
        self.gateway().analyze(self.snapshot())
        rows = {r["provider"]: r for r in AlphaLedger().cost_log.rows()}
        for vendor in ("grok", "gemini", "openai"):
            self.assertFalse(rows[vendor]["cost_priced"], vendor)
        self.assertTrue(rows["atlas_quant"]["cost_priced"])
        self.assertEqual(rows["atlas_quant"]["api_cost_usd"], 0.0)
        # One unpriced provider is enough to withhold the net figure.
        metrics = AlphaLedger().metrics()
        self.assertFalse(metrics["cost_priced"])
        self.assertIsNone(metrics["net_pnl_after_inference_cost"])
        self.assertIn("withheld", metrics["net_pnl_note"])

    def test_priced_tokens_produce_a_cost(self):
        """Rates now come from the versioned pricing table, so the cost row
        also records WHICH version produced the figure."""
        import json as _json
        from alpha_cost import PricingTable
        path = os.path.join(self._tmp, "pricing.json")
        with open(path, "w") as fh:
            _json.dump({"schema": "atlas-alpha-pricing-v1",
                        "version": "test-v1", "asof": "2026-09-09T00:00:00Z",
                        "models": {"grok/grok-4.6": {
                            "input_per_mtok": 3.0,
                            "output_per_mtok": 15.0}}}, fh)
        table = PricingTable(path)
        row = table.price("grok", "grok-4.6", 1_000_000, 1_000_000)
        self.assertTrue(row["cost_priced"])
        self.assertAlmostEqual(row["api_cost_usd"], 18.0, places=6)
        self.assertEqual(row["pricing_version"], "test-v1")
        self.assertTrue(row["priced_at"])

    def test_an_unpriced_model_is_marked_unpriced_not_free(self):
        import json as _json
        from alpha_cost import PricingTable
        path = os.path.join(self._tmp, "pricing.json")
        with open(path, "w") as fh:
            _json.dump({"schema": "atlas-alpha-pricing-v1", "version": "v",
                        "models": {"grok/grok-4.6": {
                            "input_per_mtok": None,
                            "output_per_mtok": None}}}, fh)
        row = PricingTable(path).price("grok", "grok-4.6", 1000, 1000)
        self.assertFalse(row["cost_priced"])
        self.assertEqual(row["api_cost_usd"], 0.0)
        self.assertIn("no rates configured", row["pricing_missing_reason"])


class MetricsAreDerived(LedgerCase):

    def resolve_many(self, outcomes):
        ledger = AlphaLedger()
        ids = []
        for i, outcome in enumerate(outcomes):
            snapshot = self.snapshot(contract_id=f"KX-{i}")
            opportunity = AlphaGateway(providers=self.agreeing_providers(),
                                       ledger=ledger).analyze(snapshot)
            ledger.resolve(opportunity["prediction_id"], outcome)
            ids.append(opportunity["prediction_id"])
        return ledger, ids

    def test_scores_are_computed_at_read_time_not_stored(self):
        """A stored score is a score that can drift from the prediction it
        grades."""
        opportunity, _ = self.one_prediction()
        self.assertNotIn("brier_score", opportunity)
        AlphaLedger().resolve(opportunity["prediction_id"], 1)
        resolved = AlphaLedger().resolved()[0]
        self.assertIn("brier_score", resolved)
        self.assertIn("log_loss", resolved)

    def test_brier_and_log_loss_are_arithmetically_right(self):
        scored = score_prediction({"p_meta": 0.75, "side": "yes",
                                   "entry_price": 0.60}, 1)
        self.assertAlmostEqual(scored["brier_score"], 0.0625, places=9)
        self.assertAlmostEqual(scored["log_loss"], 0.2876820724, places=6)
        self.assertAlmostEqual(scored["hypothetical_pnl"], 0.40, places=9)
        lost = score_prediction({"p_meta": 0.75, "side": "yes",
                                 "entry_price": 0.60}, 0)
        self.assertAlmostEqual(lost["hypothetical_pnl"], -0.60, places=9)

    def test_metrics_break_down_by_model_category_horizon_and_confidence(self):
        ledger, _ = self.resolve_many([1, 0, 1, 1])
        metrics = ledger.metrics()
        self.assertEqual(metrics["predictions_resolved"], 4)
        for section in ("by_model", "by_category", "by_horizon",
                        "by_latency_class", "by_confidence_bucket"):
            self.assertTrue(metrics[section], f"{section} is empty")
        self.assertEqual(set(metrics["by_model"]),
                         {"grok", "gemini", "openai", "atlas_quant"})
        for stats in metrics["by_model"].values():
            self.assertEqual(stats["samples"], 4)
            self.assertIsNotNone(stats["brier"])
            self.assertIsNotNone(stats["calibration_error"])

    def test_calibration_lookup_feeds_the_meta_engine(self):
        ledger, _ = self.resolve_many([1, 1, 1])
        record = ledger.calibration("grok", "MEDIUM")
        self.assertEqual(record["samples"], 3)
        self.assertIsNotNone(record["brier"])
        self.assertIsNone(ledger.calibration("nonexistent-model", "MEDIUM"))

    def test_unresolved_predictions_are_excluded_from_metrics(self):
        self.one_prediction()
        metrics = AlphaLedger().metrics()
        self.assertEqual(metrics["predictions_recorded"], 1)
        self.assertEqual(metrics["predictions_resolved"], 0)
        self.assertEqual(metrics["ensemble"]["samples"], 0)

    def test_provider_failure_rate_is_reported(self):
        providers = [FakeProvider("grok"),
                     FakeProvider("gemini", error="HTTP 503")]
        self.gateway(providers).analyze(self.snapshot())
        rates = AlphaLedger().metrics()["provider_failure_rate"]
        self.assertEqual(rates["grok"]["failure_rate"], 0.0)
        self.assertEqual(rates["gemini"]["failure_rate"], 1.0)
        self.assertIn("provider_failure", rates["gemini"]["reasons"])


if __name__ == "__main__":
    import unittest
    unittest.main()
