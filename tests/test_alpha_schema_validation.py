# -*- coding: utf-8 -*-
"""Alpha Gateway sections 3, 6 and 7 — the validator.

THE INVARIANT
    A validation failure is INVALID / EXCLUDED and never becomes a
    probability. Above all it never becomes 0.5.

WHY THAT IS THE ONE THING WORTH PINNING HARDEST
    0.5 is not "no opinion". It is a confident claim that the market is a
    coin flip, and it enters the weighted mean like any other estimate --
    dragging the ensemble toward the midpoint exactly when one model has
    failed and the other three may be right. Every rejection case below
    therefore asserts two things: that the signal is excluded, and that
    `p_yes is None`, so there is no number for a caller to pick up by
    accident.

Every case runs the real `validate_signal` against a real snapshot.
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, valid_payload                   # noqa: E402

from alpha_schema import STATUS_VALID, validate_signal        # noqa: E402


class ValidatorRejections(AlphaCase):

    def check(self, snapshot, payload, expected_reason):
        signal = validate_signal(payload, snapshot, provider="p", model="m")
        self.assertFalse(signal.valid, f"{expected_reason} was ACCEPTED")
        self.assertEqual(signal.rejected_reason, expected_reason,
                         signal.rejected_detail)
        # The rule that matters: no probability survives a rejection.
        self.assertIsNone(signal.p_yes)
        self.assertIsNone(signal.probability_low)
        self.assertIsNone(signal.probability_high)
        self.assertIsNone(signal.confidence)
        return signal

    # ── probabilities ───────────────────────────────────────────────────
    def test_probability_below_zero_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=-0.01, low=-0.01, high=0.5),
                   "p_yes_out_of_range")

    def test_probability_above_one_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=1.01, low=0.5, high=1.01),
                   "p_yes_out_of_range")

    def test_a_percentage_where_a_probability_was_asked_is_rejected(self):
        """A model answering 60 instead of 0.60 must not become certainty."""
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=60.0, low=55.0, high=65.0),
                   "p_yes_out_of_range")

    def test_nan_probability_is_rejected(self):
        """`json.loads` accepts bare NaN, and every comparison against it is
        silently False -- which is how a NaN passes a range check."""
        s = self.snapshot()
        payload = json.dumps(valid_payload(s)).replace('"p_yes": 0.6',
                                                       '"p_yes": NaN')
        self.assertTrue(math.isnan(json.loads(payload)["p_yes"]))
        self.check(s, payload, "p_yes_not_finite")

    def test_infinite_probability_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=float("inf")), "p_yes_not_finite")
        self.check(s, valid_payload(s, p_yes=float("-inf")), "p_yes_not_finite")

    def test_a_string_probability_is_rejected_not_coerced(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes="0.60"), "p_yes_not_finite")

    def test_a_boolean_probability_is_rejected(self):
        """`True` is an int in Python and would read as p_yes = 1.0."""
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=True), "p_yes_not_finite")

    def test_a_missing_probability_is_rejected(self):
        s = self.snapshot()
        payload = valid_payload(s)
        del payload["p_yes"]
        self.check(s, payload, "p_yes_not_finite")

    # ── intervals (section 11) ──────────────────────────────────────────
    def test_interval_below_zero_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, low=-0.1), "interval_low_out_of_range")

    def test_interval_above_one_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, high=1.2), "interval_high_out_of_range")

    def test_an_interval_that_excludes_its_own_estimate_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, p_yes=0.60, low=0.65, high=0.70),
                   "interval_excludes_estimate")
        self.check(s, valid_payload(s, p_yes=0.60, low=0.30, high=0.50),
                   "interval_excludes_estimate")

    def test_a_nan_interval_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, low=float("nan")),
                   "interval_not_finite")

    # ── identity (section 2) ────────────────────────────────────────────
    def test_a_missing_contract_id_is_rejected(self):
        s = self.snapshot()
        payload = valid_payload(s)
        del payload["contract_id"]
        self.check(s, payload, "missing_contract_id")

    def test_a_wrong_contract_id_is_rejected(self):
        """The model answered about a different market. Its probability is
        about that market, not this one."""
        s = self.snapshot()
        self.check(s, valid_payload(s, contract_id="KXETH-SOMETHING-ELSE"),
                   "wrong_contract_id")

    def test_a_wrong_snapshot_id_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, market_snapshot_id="snap-deadbeef"),
                   "wrong_snapshot_id")

    def test_a_missing_snapshot_id_is_rejected(self):
        s = self.snapshot()
        payload = valid_payload(s)
        del payload["market_snapshot_id"]
        self.check(s, payload, "wrong_snapshot_id")

    # ── schema and shape ────────────────────────────────────────────────
    def test_malformed_json_is_rejected(self):
        s = self.snapshot()
        for bad in ('{"p_yes": 0.6', "not json at all", "[1, 2, 3]", "null"):
            with self.subTest(payload=bad[:20]):
                self.check(s, bad, "malformed_json")

    def test_a_fenced_json_block_is_still_read(self):
        """Models wrap JSON in ``` often enough that refusing outright would
        discard usable answers. Exactly one fence is peeled; the tolerance
        stops there."""
        s = self.snapshot()
        body = json.dumps(valid_payload(s))
        signal = validate_signal(f"```json\n{body}\n```", s,
                                 provider="p", model="m")
        self.assertTrue(signal.valid, signal.rejected_detail)
        self.assertAlmostEqual(signal.p_yes, 0.60, places=6)

    def test_prose_around_json_is_rejected_not_scavenged(self):
        s = self.snapshot()
        body = json.dumps(valid_payload(s))
        self.check(s, f"Here is my answer: {body} Hope that helps!",
                   "malformed_json")

    def test_an_unknown_schema_version_is_rejected(self):
        s = self.snapshot()
        self.check(s, valid_payload(s, schema_version="atlas-alpha-v1"),
                   "invalid_schema_version")
        payload = valid_payload(s)
        del payload["schema_version"]
        self.check(s, payload, "invalid_schema_version")

    def test_an_unknown_status_is_rejected(self):
        s = self.snapshot()
        for status in ("OK", "", None, "valid", 1):
            with self.subTest(status=status):
                self.check(s, valid_payload(s, status=status),
                           "unknown_status")

    def test_a_model_declared_non_valid_status_is_excluded_with_its_reason(self):
        """The model saying INSUFFICIENT_EVIDENCE is information, not a
        failure -- and it is still not a probability."""
        s = self.snapshot()
        signal = self.check(s, valid_payload(s, status="INSUFFICIENT_EVIDENCE"),
                            "model_status_insufficient_evidence")
        self.assertIsNone(signal.p_yes)

    # ── quality scalars ─────────────────────────────────────────────────
    def test_out_of_range_quality_scalars_are_rejected(self):
        s = self.snapshot()
        for field in ("confidence", "evidence_quality", "data_completeness"):
            with self.subTest(field=field):
                self.check(s, valid_payload(s, **{field: 1.4}),
                           f"{field}_out_of_range")
                self.check(s, valid_payload(s, **{field: float("nan")}),
                           f"{field}_not_finite")

    # ── section 3: models may not issue instructions ────────────────────
    def test_a_model_returning_an_execution_instruction_is_discarded(self):
        """Not stripped -- discarded. A model that thinks it can place an
        order has misunderstood the task, and its probability is not more
        trustworthy than its instructions."""
        s = self.snapshot()
        for field in ("action", "side", "size", "limit_price", "capital",
                      "order", "quantity", "stake"):
            with self.subTest(field=field):
                self.check(s, valid_payload(s, **{field: "BUY"}),
                           "execution_instruction")


class TimeAndCatalystExpiry(AlphaCase):
    """Sections 6 and 7: nothing outlives the information it was based on."""

    def test_generated_after_valid_until_is_rejected(self):
        s = self.snapshot()
        now = datetime.now(timezone.utc)
        signal = validate_signal(
            valid_payload(s, generated_at_utc=now.isoformat(timespec="seconds"),
                          valid_until_utc=(now - timedelta(minutes=5))
                          .isoformat(timespec="seconds")),
            s, provider="p", model="m")
        self.assertEqual(signal.rejected_reason, "generated_after_valid_until")
        self.assertIsNone(signal.p_yes)

    def test_a_response_after_the_analysis_deadline_is_stale(self):
        s = self.snapshot()
        late = s.analysis_deadline + timedelta(seconds=1)
        signal = validate_signal(valid_payload(s), s, provider="p", model="m",
                                 received_at=late.isoformat())
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason, "late_response")
        self.assertIsNone(signal.p_yes)

    def test_a_response_exactly_on_the_deadline_is_accepted(self):
        """The boundary, not just the comfortable case."""
        s = self.snapshot()
        signal = validate_signal(valid_payload(s), s, provider="p", model="m",
                                 received_at=s.analysis_deadline.isoformat())
        self.assertTrue(signal.valid, signal.rejected_detail)

    def test_a_catalyst_shortens_the_analysis_deadline(self):
        """A catalyst inside the window shortens it: there is no point asking
        for an answer guaranteed to be stale on arrival."""
        s = self.snapshot(minutes_to_resolution=240, catalyst_in=90)
        budget = (s.analysis_deadline - s.snapshot_time).total_seconds()
        self.assertLess(budget, 90)
        self.assertAlmostEqual(
            budget, 90 - float(__import__("config").CFG.ALPHA_CATALYST_BUFFER_S),
            places=3)

    def test_a_signal_cannot_survive_a_known_catalyst(self):
        """Section 6: effective validity is bounded by the catalyst minus the
        safety buffer, whatever the model claims."""
        s = self.snapshot(minutes_to_resolution=240, catalyst_in=600)
        far_future = (s.snapshot_time + timedelta(hours=3)) \
            .isoformat(timespec="seconds")
        effective = s.effective_valid_until(far_future)
        self.assertLess(effective, s.catalyst_time)
        after_catalyst = (s.catalyst_time + timedelta(seconds=1))
        signal = validate_signal(
            valid_payload(s, valid_until_utc=far_future), s,
            provider="p", model="m", received_at=after_catalyst.isoformat())
        self.assertFalse(signal.valid)
        self.assertIn(signal.rejected_reason, ("stale", "late_response"))
        self.assertIsNone(signal.p_yes)

    def test_effective_validity_is_the_minimum_of_all_three_bounds(self):
        s = self.snapshot(minutes_to_resolution=240, catalyst_in=3600)
        model_bound = (s.snapshot_time + timedelta(seconds=5)) \
            .isoformat(timespec="seconds")
        self.assertEqual(s.effective_valid_until(model_bound),
                         s.snapshot_time + timedelta(seconds=5))
        self.assertEqual(s.effective_valid_until(), s.analysis_deadline)

    def test_a_naive_timestamp_is_refused_rather_than_assumed_utc(self):
        s = self.snapshot()
        signal = validate_signal(
            valid_payload(s, generated_at_utc="2026-09-09T12:00:00"),
            s, provider="p", model="m")
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason, "invalid_schema")


class PositiveControls(AlphaCase):
    """A suite where nothing can ever pass proves nothing."""

    def test_a_well_formed_answer_is_accepted_intact(self):
        s = self.snapshot()
        signal = validate_signal(valid_payload(s, p_yes=0.62, low=0.55,
                                               high=0.70),
                                 s, provider="grok", model="grok-4",
                                 latency_ms=340)
        self.assertTrue(signal.valid, signal.rejected_detail)
        self.assertEqual(signal.status, STATUS_VALID)
        self.assertAlmostEqual(signal.p_yes, 0.62, places=6)
        self.assertAlmostEqual(signal.interval_width, 0.15, places=6)
        self.assertEqual(signal.contract_id, s.contract_id)
        self.assertEqual(signal.market_snapshot_id, s.market_snapshot_id)
        self.assertEqual(signal.provider, "grok")

    def test_the_measured_latency_wins_over_the_models_self_report(self):
        """One is observed, the other is claimed."""
        s = self.snapshot()
        signal = validate_signal(valid_payload(s, analysis_latency_ms=5),
                                 s, provider="p", model="m", latency_ms=2100)
        self.assertEqual(signal.analysis_latency_ms, 2100)

    def test_a_validated_signal_is_frozen(self):
        s = self.snapshot()
        signal = validate_signal(valid_payload(s), s, provider="p", model="m")
        with self.assertRaises(Exception):
            signal.p_yes = 0.99

    def test_edge_probabilities_zero_and_one_are_accepted(self):
        s = self.snapshot()
        for p, low, high in ((0.0, 0.0, 0.1), (1.0, 0.9, 1.0)):
            with self.subTest(p=p):
                signal = validate_signal(
                    valid_payload(s, p_yes=p, low=low, high=high), s,
                    provider="p", model="m")
                self.assertTrue(signal.valid, signal.rejected_detail)


if __name__ == "__main__":
    import unittest
    unittest.main()
