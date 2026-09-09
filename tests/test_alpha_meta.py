# -*- coding: utf-8 -*-
"""Alpha Gateway sections 8-11 and 15 — the Meta Alpha Engine.

THE INVARIANTS
    1. No permanent equal weighting, and no single LLM dominating before it
       has earned it. Weights are capped and floored.
    2. Disagreement REDUCES ensemble confidence and is never smoothed away.
    3. A wide probability interval contributes less actionable confidence
       than a narrow one at the same point estimate (section 11).
    4. Specialization is LEARNED from resolved history, never hard-coded,
       and history below the sample threshold earns nothing.
    5. Raw edge and shadow net edge are both reported: the gap between them
       is the finding.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, valid_payload                   # noqa: E402

from alpha_meta import (best_side, dispersion, ensemble,       # noqa: E402
                        interval_factor, model_weights,
                        calibration_multiplier, shadow_edge)
from alpha_schema import validate_signal                       # noqa: E402
from config import CFG                                         # noqa: E402


class _History:
    """A stand-in calibration store. Mirrors `AlphaLedger.calibration`."""

    def __init__(self, table):
        self.table = table

    def calibration(self, model, category):
        return self.table.get((model, category)) or self.table.get(model)


class MetaCase(AlphaCase):

    def signals(self, snapshot, spec):
        """spec: {model: (p, low, high, confidence, evidence, completeness)}"""
        out = []
        for model, values in spec.items():
            p, low, high = values[0], values[1], values[2]
            confidence = values[3] if len(values) > 3 else 0.7
            evidence = values[4] if len(values) > 4 else 0.8
            completeness = values[5] if len(values) > 5 else 0.9
            signal = validate_signal(
                valid_payload(snapshot, p_yes=p, low=low, high=high,
                              confidence=confidence, evidence=evidence,
                              completeness=completeness, model=model),
                snapshot, provider=model, model=model)
            self.assertTrue(signal.valid, signal.rejected_detail)
            out.append(signal)
        return out


class WeightsAreCappedAndFloored(MetaCase):

    def test_no_single_model_exceeds_the_cap(self):
        """Section 8: until enough history exists, weights stay conservative
        and capped so no single LLM dominates."""
        snapshot = self.snapshot()
        signals = self.signals(snapshot, {
            "grok": (0.60, 0.59, 0.61, 0.99, 1.0, 1.0),   # tightest + loudest
            "gemini": (0.55, 0.30, 0.80, 0.20, 0.3, 0.3),
            "openai": (0.58, 0.35, 0.75, 0.20, 0.3, 0.3),
            "atlas_quant": (0.57, 0.40, 0.70, 0.20, 0.3, 0.3)})
        weights = model_weights(signals, snapshot, datetime.now(timezone.utc))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        for model, weight in weights.items():
            self.assertLessEqual(weight, float(CFG.ALPHA_MAX_MODEL_WEIGHT) + 1e-6,
                                 f"{model} dominates at {weight}")

    def test_no_model_is_weighted_out_of_existence(self):
        snapshot = self.snapshot()
        signals = self.signals(snapshot, {
            "grok": (0.60, 0.59, 0.61, 0.99, 1.0, 1.0),
            "gemini": (0.55, 0.00, 1.00, 0.01, 0.0, 0.0)})   # as bad as it gets
        weights = model_weights(signals, snapshot, datetime.now(timezone.utc))
        self.assertGreaterEqual(weights["gemini"],
                                min(float(CFG.ALPHA_MIN_MODEL_WEIGHT), 0.5) - 1e-6)

    def test_weighting_is_not_permanently_equal(self):
        """Two models differing only in interval width must not weigh the
        same -- that is the whole point of section 11."""
        snapshot = self.snapshot()
        signals = self.signals(snapshot, {
            "tight": (0.52, 0.49, 0.55),
            "wide": (0.52, 0.38, 0.66)})
        weights = model_weights(signals, snapshot, datetime.now(timezone.utc))
        self.assertGreater(weights["tight"], weights["wide"])

    def test_a_wide_interval_contributes_less_than_a_narrow_one(self):
        """The exact example from section 11."""
        snapshot = self.snapshot()
        tight, wide = self.signals(snapshot, {
            "tight": (0.52, 0.49, 0.55), "wide": (0.52, 0.38, 0.66)})
        self.assertGreater(interval_factor(tight), interval_factor(wide))


class SpecializationIsLearnedNotHardCoded(MetaCase):

    def test_below_the_sample_threshold_history_earns_nothing(self):
        """A weight learned from a handful of resolutions is noise dressed
        as evidence."""
        few = _History({("grok", "MEDIUM"): {"samples": 5, "brier": 0.01}})
        self.assertEqual(calibration_multiplier("grok", "MEDIUM", few), 1.0)

    def test_above_the_threshold_a_calibrated_model_earns_weight(self):
        n = int(CFG.ALPHA_CALIBRATION_MIN_SAMPLES)
        good = _History({("grok", "MEDIUM"): {"samples": n, "brier": 0.10}})
        bad = _History({("grok", "MEDIUM"): {"samples": n, "brier": 0.40}})
        self.assertGreater(calibration_multiplier("grok", "MEDIUM", good), 1.0)
        self.assertLess(calibration_multiplier("grok", "MEDIUM", bad), 1.0)

    def test_the_multiplier_is_bounded_on_both_sides(self):
        n = int(CFG.ALPHA_CALIBRATION_MIN_SAMPLES)
        perfect = _History({("grok", "MEDIUM"): {"samples": n, "brier": 0.0}})
        awful = _History({("grok", "MEDIUM"): {"samples": n, "brier": 1.0}})
        self.assertLessEqual(calibration_multiplier("grok", "MEDIUM", perfect), 1.5)
        self.assertGreaterEqual(calibration_multiplier("grok", "MEDIUM", awful), 0.5)

    def test_no_provider_name_appears_in_the_weighting_code(self):
        """Section 15: specializations must be LEARNED. A hard-coded
        `if model == "grok"` would make the ledger decorative."""
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "alpha_meta.py")).read()
        for vendor in ("grok", "gemini", "openai", "Grok", "Gemini", "OpenAI"):
            self.assertNotIn(f'"{vendor}"', source)
            self.assertNotIn(f"'{vendor}'", source)

    def test_a_missing_or_broken_history_is_neutral_not_fatal(self):
        class _Broken:
            def calibration(self, model, category):
                raise RuntimeError("store unavailable")
        self.assertEqual(calibration_multiplier("grok", "FAST", None), 1.0)
        self.assertEqual(calibration_multiplier("grok", "FAST", _Broken()), 1.0)


class DisagreementLowersConfidence(MetaCase):

    def test_dispersion_is_measured(self):
        self.assertEqual(dispersion([0.5]), 0.0)
        self.assertEqual(dispersion([]), 0.0)
        self.assertGreater(dispersion([0.2, 0.8]), dispersion([0.49, 0.51]))

    def test_high_disagreement_reduces_ensemble_confidence(self):
        """Section 9's core requirement, measured against an otherwise
        identical agreeing ensemble."""
        snapshot = self.snapshot()
        now = datetime.now(timezone.utc)
        agree = ensemble(self.signals(snapshot, {
            "a": (0.58, 0.55, 0.61), "b": (0.60, 0.57, 0.63),
            "c": (0.59, 0.56, 0.62), "d": (0.61, 0.58, 0.64)}), snapshot, now)
        disagree = ensemble(self.signals(snapshot, {
            "a": (0.20, 0.17, 0.23), "b": (0.80, 0.77, 0.83),
            "c": (0.35, 0.32, 0.38), "d": (0.75, 0.72, 0.78)}), snapshot, now)
        self.assertGreater(disagree["disagreement"], agree["disagreement"])
        self.assertLess(disagree["confidence"], agree["confidence"])
        self.assertLess(disagree["disagreement_penalty"],
                        agree["disagreement_penalty"])

    def test_disagreement_is_reported_not_smoothed_away(self):
        """Four models that disagree sharply are telling us something. The
        ensemble still produces a number, and still says how contested it is."""
        snapshot = self.snapshot()
        meta = ensemble(self.signals(snapshot, {
            "a": (0.10, 0.05, 0.15), "b": (0.90, 0.85, 0.95)}),
            snapshot, datetime.now(timezone.utc))
        self.assertIsNotNone(meta["p_meta"])
        self.assertGreater(meta["disagreement"], 0.3)
        self.assertEqual(meta["per_model"]["a"]["p_yes"], 0.10)
        self.assertEqual(meta["per_model"]["b"]["p_yes"], 0.90)

    def test_an_empty_ensemble_is_none_never_a_midpoint(self):
        snapshot = self.snapshot()
        meta = ensemble([], snapshot, datetime.now(timezone.utc))
        self.assertIsNone(meta["p_meta"])
        self.assertNotEqual(meta["p_meta"], 0.5)
        self.assertEqual(meta["models"], 0)
        self.assertEqual(meta["confidence"], 0.0)


class EdgeIsNetOfEverything(MetaCase):

    def meta_at(self, snapshot, p):
        return ensemble(self.signals(snapshot, {
            "a": (p, max(0.0, p - 0.03), min(1.0, p + 0.03)),
            "b": (p, max(0.0, p - 0.03), min(1.0, p + 0.03))}),
            snapshot, datetime.now(timezone.utc))

    def test_raw_edge_is_the_naive_difference(self):
        snapshot = self.snapshot(yes_ask=0.46)
        edge = shadow_edge(self.meta_at(snapshot, 0.60), snapshot, side="yes")
        self.assertAlmostEqual(edge["raw_edge"], 0.60 - 0.46, places=4)

    def test_net_edge_is_always_below_raw_edge(self):
        snapshot = self.snapshot(yes_ask=0.46)
        edge = shadow_edge(self.meta_at(snapshot, 0.60), snapshot, side="yes")
        self.assertLess(edge["shadow_net_edge"], edge["raw_edge"])
        for component in ("spread_cost", "estimated_fees", "estimated_slippage",
                          "uncertainty_penalty", "latency_penalty",
                          "inference_cost_penalty"):
            self.assertIn(component, edge["components"])

    def test_inference_cost_reduces_the_edge(self):
        """Section 12: the objective is net economic alpha, so the cost of
        producing the opinion is part of the arithmetic."""
        snapshot = self.snapshot(yes_ask=0.46)
        meta = self.meta_at(snapshot, 0.60)
        free = shadow_edge(meta, snapshot, side="yes", inference_cost_usd=0.0)
        paid = shadow_edge(meta, snapshot, side="yes", inference_cost_usd=0.25)
        self.assertLess(paid["shadow_net_edge"], free["shadow_net_edge"])

    def test_a_large_enough_inference_cost_erases_the_edge(self):
        snapshot = self.snapshot(yes_ask=0.46)
        meta = self.meta_at(snapshot, 0.60)
        expensive = shadow_edge(meta, snapshot, side="yes",
                                inference_cost_usd=5.0)
        self.assertLess(expensive["shadow_net_edge"], 0.0)

    def test_uncertainty_reduces_the_edge(self):
        snapshot = self.snapshot(yes_ask=0.46)
        now = datetime.now(timezone.utc)
        tight = ensemble(self.signals(snapshot, {
            "a": (0.60, 0.58, 0.62), "b": (0.60, 0.58, 0.62)}), snapshot, now)
        wide = ensemble(self.signals(snapshot, {
            "a": (0.60, 0.30, 0.90), "b": (0.60, 0.30, 0.90)}), snapshot, now)
        self.assertAlmostEqual(tight["p_meta"], wide["p_meta"], places=6)
        self.assertLess(shadow_edge(wide, snapshot, side="yes")["shadow_net_edge"],
                        shadow_edge(tight, snapshot, side="yes")["shadow_net_edge"])

    def test_the_no_side_is_evaluated_too(self):
        snapshot = self.snapshot(yes_ask=0.60, yes_bid=0.58,
                                 no_ask=0.42, no_bid=0.40)
        edge = shadow_edge(self.meta_at(snapshot, 0.20), snapshot, side="no")
        self.assertAlmostEqual(edge["raw_edge"], (1 - 0.20) - 0.42, places=4)
        self.assertGreater(edge["shadow_net_edge"], 0.0)

    def test_best_side_chooses_on_net_edge_not_raw_edge(self):
        snapshot = self.snapshot(yes_ask=0.60, yes_bid=0.58,
                                 no_ask=0.42, no_bid=0.40)
        best = best_side(self.meta_at(snapshot, 0.20), snapshot)
        self.assertEqual(best["side"], "no")

    def test_no_ensemble_means_no_edge_not_zero_edge(self):
        snapshot = self.snapshot()
        edge = shadow_edge({"p_meta": None}, snapshot, side="yes")
        self.assertIsNone(edge["raw_edge"])
        self.assertIsNone(edge["shadow_net_edge"])


if __name__ == "__main__":
    import unittest
    unittest.main()
