# -*- coding: utf-8 -*-
"""Alpha Gateway phase 2, sections 2, 3, 7, 8, 10, 11 — the shadow service.

THE INVARIANTS
    1. The service refuses to start beside a broker credential. The whole
       argument for a separate process is that the separation is real, so it
       is checked rather than assumed.
    2. A provider that fails its health check is EXCLUDED, never silently
       replaced by another model.
    3. A probability estimated before a catalyst does not stay actionable
       after it.
    4. Prices are captured at dispatch, first response, last valid response
       and at configured intervals afterwards, so latency decay is measured.
    5. The full lifecycle runs with no operator copying anything.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, FakeProvider                    # noqa: E402

from alpha_cost import BudgetGuard, BudgetLedger, PricingTable  # noqa: E402
from alpha_ledger import AlphaLedger                          # noqa: E402
from alpha_service import (BROKER_AUTHORITY_VARS,             # noqa: E402
                           BROKER_CREDENTIAL_VARS,
                           AlphaShadowService,
                           BrokerCredentialsPresent,
                           assert_no_broker_credentials,
                           observation_intervals)
from config import CFG                                        # noqa: E402
from research_feed import ResearchFeed, candidate_from_market  # noqa: E402


def market(ticker="KXBTCD-1", hours=4, catalyst_in=None):
    now = datetime.now(timezone.utc)
    return {"ticker": ticker, "event_ticker": "EV", "title": "BTC > 60000?",
            "rules_primary": "RTI", "volume": 1200, "open_interest": 3400,
            "close_time": (now + timedelta(hours=hours - 1)).isoformat(),
            "expiration_time": (now + timedelta(hours=hours)).isoformat()}


BOOK = {"yes_bid": 44, "yes_ask": 46, "no_bid": 54, "no_ask": 56}
QUOTE = {"yes_bid": 0.44, "yes_ask": 0.46, "no_bid": 0.54, "no_ask": 0.56}


class ServiceCase(AlphaCase):

    def setUp(self):
        super().setUp()
        self._patches.append(patch.object(CFG, "RESEARCH_FEED_ENABLED", True))
        self._patches[-1].start()
        # A genuinely separate service holds neither broker credentials nor
        # write authority. The suite's conftest sets both for the ENGINE
        # tests, so they are cleared here and restored on teardown -- never
        # deleted, since `tests/_gates.py` owns them for the whole run.
        for var in BROKER_CREDENTIAL_VARS + BROKER_AUTHORITY_VARS:
            self._saved_env.setdefault(var, os.environ.get(var))
            os.environ.pop(var, None)

    def pricing(self, rates=(3.0, 15.0), **kw):
        from _alpha import write_pricing
        return PricingTable(write_pricing(
            os.path.join(self._tmp, "pricing.json"), rates=rates, **kw))

    def emit(self, ticker="KXBTCD-1", **kw):
        return ResearchFeed().emit_candidate(
            candidate_from_market(market(ticker, **kw), BOOK, cycle_id="c1"))

    def service(self, providers=None, quote_fn=None, **kw):
        return AlphaShadowService(
            providers=providers if providers is not None
            else self.agreeing_providers(),
            budget=BudgetGuard(pricing=self.pricing(),
                               ledger=BudgetLedger(
                                   os.path.join(self._tmp, "budget.jsonl"))),
            quote_fn=quote_fn if quote_fn is not None else (lambda: dict(QUOTE)),
            **kw)


class TheServiceRefusesBrokerCredentials(ServiceCase):
    """Section 2: it may hold AI keys and read-only market data. Nothing
    that can move money."""

    def test_a_broker_key_prevents_startup(self):
        for var in ("KALSHI_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY"):
            with self.subTest(var=var):
                os.environ[var] = "secret-value"
                try:
                    with self.assertRaises(BrokerCredentialsPresent) as ctx:
                        assert_no_broker_credentials()
                    # the NAME is reported, never the value
                    self.assertIn(var, str(ctx.exception))
                    self.assertNotIn("secret-value", str(ctx.exception))
                finally:
                    os.environ.pop(var, None)

    def test_a_write_authority_gate_also_prevents_startup(self):
        # RESTORE, do not delete: `tests/_gates.py` owns this variable for
        # the whole suite, and removing it would silently change what every
        # later test is running against.
        previous = os.environ.get("ALLOW_ORDER_SUBMISSION")
        os.environ["ALLOW_ORDER_SUBMISSION"] = "true"
        try:
            with self.assertRaises(BrokerCredentialsPresent):
                assert_no_broker_credentials()
        finally:
            if previous is None:
                os.environ.pop("ALLOW_ORDER_SUBMISSION", None)
            else:
                os.environ["ALLOW_ORDER_SUBMISSION"] = previous

    def test_ai_keys_are_fine(self):
        os.environ["XAI_API_KEY"] = "x"
        os.environ["OPENAI_API_KEY"] = "y"
        try:
            self.assertEqual(assert_no_broker_credentials(), [])
        finally:
            os.environ.pop("XAI_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)

    def test_the_check_can_be_disabled_only_deliberately(self):
        os.environ["KALSHI_KEY_ID"] = "k"
        try:
            with patch.object(CFG, "ALPHA_REFUSE_BROKER_CREDENTIALS", False):
                self.assertEqual(assert_no_broker_credentials(),
                                 ["KALSHI_KEY_ID"])
        finally:
            os.environ.pop("KALSHI_KEY_ID", None)

    def test_the_startup_report_says_shadow_only(self):
        report = self.service().startup_report()
        self.assertEqual(report["mode"], "SHADOW_ONLY")
        self.assertEqual(report["broker_credentials"], [])


class ProviderHealthIsCheckedNotAssumed(ServiceCase):
    """Section 3: an unavailable provider/model is EXCLUDED, never silently
    substituted."""

    def test_an_unconfigured_provider_reports_unhealthy(self):
        from alpha_providers import GrokProvider
        report = GrokProvider().health_check()
        self.assertFalse(report["configured"])
        self.assertIn("XAI_API_KEY", report["detail"])

    def test_an_unpriced_provider_reports_unhealthy(self):
        from alpha_providers import GrokProvider
        os.environ["XAI_API_KEY"] = "k"
        try:
            with patch("alpha_providers.pricing_table",
                       return_value=self.pricing(rates=(None, None),
                                                 models=("grok",))), \
                 patch.object(CFG, "ALPHA_HEALTHCHECK_ENABLED", False):
                report = GrokProvider().health_check()
            self.assertTrue(report["configured"])
            self.assertFalse(report["priced"])
            self.assertFalse(GrokProvider().healthy(report))
        finally:
            os.environ.pop("XAI_API_KEY", None)

    def test_an_unreachable_provider_reports_unhealthy_without_raising(self):
        from _alpha import http_session
        from alpha_providers import GrokProvider
        os.environ["XAI_API_KEY"] = "k"
        try:
            provider = GrokProvider(session=http_session(
                {}, raises=OSError("connection refused")))
            with patch("alpha_providers.pricing_table",
                       return_value=self.pricing()):
                report = provider.health_check()
            self.assertIs(report["reachable"], False)
            self.assertIn("connection refused", report["detail"])
        finally:
            os.environ.pop("XAI_API_KEY", None)

    def test_the_unwired_quant_is_healthy_and_still_says_insufficient(self):
        """Section 9: it must stay separately measurable, and it must not be
        faked into an opinion."""
        from alpha_providers import AtlasQuantProvider
        with patch("alpha_providers.pricing_table",
                   return_value=self.pricing()):
            report = AtlasQuantProvider().health_check()
        self.assertTrue(report["configured"])
        self.assertFalse(report["wired"])
        self.assertIn("INSUFFICIENT_EVIDENCE", report["detail"])

    def test_an_unhealthy_provider_does_not_stop_the_others(self):
        service = self.service(providers=[
            FakeProvider("grok", error="unreachable"),
            FakeProvider("gemini"), FakeProvider("openai")])
        service.startup_report()
        self.emit()
        summary = service.cycle()
        self.assertEqual(len(summary["analyzed"]), 1)
        self.assertIsNotNone(summary["analyzed"][0]["p_meta"])


class TheFullLifecycleIsAutomatic(ServiceCase):
    """Section 10: no operator copies a contract anywhere."""

    def test_candidate_to_calibration_without_manual_steps(self):
        self.emit()
        service = self.service()
        service.startup_report()
        summary = service.cycle()
        self.assertEqual(len(summary["analyzed"]), 1)
        prediction_id = summary["analyzed"][0]["prediction_id"]

        # ...outcome attached later, calibration updated
        service.attach_outcome(prediction_id, 1, source="kalshi")
        metrics = service.ledger.metrics()
        self.assertEqual(metrics["predictions_resolved"], 1)
        self.assertIsNotNone(metrics["ensemble"]["brier"])
        self.assertEqual(service.telemetry.counters["resolved_predictions"], 1)

    def test_a_restart_does_not_re_analyse_the_same_snapshot(self):
        self.emit()
        first = self.service()
        first.cycle()
        second = self.service()
        summary = second.cycle()
        self.assertEqual(summary["analyzed"], [])
        self.assertEqual(second.consumer.stats["duplicates"], 1)

    def test_run_stops_after_max_cycles(self):
        self.emit()
        service = self.service()
        report = service.run(max_cycles=2, sleep_fn=lambda _s: None)
        self.assertEqual(report["cycles"], 2)
        self.assertEqual(report["mode"], "SHADOW_ONLY")

    def test_a_consumer_failure_does_not_end_the_loop(self):
        service = self.service()
        with patch.object(type(service.consumer), "pending",
                          side_effect=RuntimeError("spool unreadable")):
            summary = service.cycle()
        self.assertEqual(summary["analyzed"], [])
        self.assertEqual(service.telemetry.counters["errors"], 1)


class CatalystInvalidation(ServiceCase):
    """Section 7: an estimate made before an event it never saw does not
    stay actionable after the event."""

    def analysed_with_catalyst(self, seconds):
        now = datetime.now(timezone.utc)
        candidate = candidate_from_market(market(), BOOK)
        candidate["catalyst_name"] = "CPI release"
        candidate["catalyst_time_utc"] = (
            now + timedelta(seconds=seconds)).isoformat()
        ResearchFeed().emit_candidate(candidate)
        service = self.service()
        summary = service.cycle()
        return service, summary

    def test_a_prediction_is_invalidated_once_its_catalyst_passes(self):
        service, summary = self.analysed_with_catalyst(1800)
        self.assertEqual(len(summary["analyzed"]), 1)
        prediction_id = summary["analyzed"][0]["prediction_id"]
        self.assertIsNone(service.ledger.find_invalidation(prediction_id))

        later = self.service(now_fn=lambda: datetime.now(timezone.utc)
                             + timedelta(hours=1))
        self.assertEqual(later.cycle()["invalidated"], 1)
        row = later.ledger.find_invalidation(prediction_id)
        self.assertEqual(row["reason"], "catalyst_occurred")
        self.assertIn("CPI release", row["detail"])

    def test_invalidation_is_recorded_without_editing_the_prediction(self):
        service, summary = self.analysed_with_catalyst(1800)
        prediction_id = summary["analyzed"][0]["prediction_id"]
        before = json.dumps(service.ledger.find_prediction(prediction_id),
                            sort_keys=True)
        self.service(now_fn=lambda: datetime.now(timezone.utc)
                     + timedelta(hours=1)).cycle()
        after = json.dumps(AlphaLedger().find_prediction(prediction_id),
                           sort_keys=True)
        self.assertEqual(before, after)

    def test_an_invalidated_prediction_leaves_the_actionable_series(self):
        service, summary = self.analysed_with_catalyst(1800)
        prediction_id = summary["analyzed"][0]["prediction_id"]
        later = self.service(now_fn=lambda: datetime.now(timezone.utc)
                             + timedelta(hours=1))
        later.cycle()
        later.attach_outcome(prediction_id, 1)
        metrics = later.ledger.metrics()
        self.assertEqual(metrics["predictions_resolved"], 1)
        self.assertEqual(metrics["ensemble"]["samples"], 1)
        self.assertEqual(metrics["ensemble_actionable"]["samples"], 0)
        self.assertEqual(metrics["invalidated_predictions"], 1)

    def test_invalidation_is_idempotent(self):
        service, summary = self.analysed_with_catalyst(1800)
        prediction_id = summary["analyzed"][0]["prediction_id"]
        for _ in range(3):
            self.service(now_fn=lambda: datetime.now(timezone.utc)
                         + timedelta(hours=1)).cycle()
        rows = [r for r in AlphaLedger().rows()
                if r.get("kind") == "INVALIDATION"
                and r.get("prediction_id") == prediction_id]
        self.assertEqual(len(rows), 1)

    def test_a_prediction_with_no_catalyst_is_never_invalidated(self):
        self.emit()
        service = self.service()
        service.cycle()
        later = self.service(now_fn=lambda: datetime.now(timezone.utc)
                             + timedelta(days=7))
        self.assertEqual(later.cycle()["invalidated"], 0)


class MarketMovementIsTracked(ServiceCase):
    """Section 8: prices at dispatch, first response, last valid response,
    and at configured intervals afterwards."""

    def test_the_dispatch_record_carries_every_stage(self):
        self.emit()
        prices = iter([{"yes_bid": 0.44, "yes_ask": 0.46, "no_bid": 0.54, "no_ask": 0.56},
                       {"yes_bid": 0.48, "yes_ask": 0.50, "no_bid": 0.50, "no_ask": 0.52},
                       {"yes_bid": 0.49, "yes_ask": 0.51, "no_bid": 0.49, "no_ask": 0.51},
                       {"yes_bid": 0.52, "yes_ask": 0.54, "no_bid": 0.46, "no_ask": 0.48}])
        last = {"q": dict(QUOTE)}

        def quote_fn():
            try:
                last["q"] = next(prices)
            except StopIteration:
                pass
            return dict(last["q"])

        service = self.service(quote_fn=quote_fn)
        service.cycle()
        prediction = AlphaLedger().predictions()[0]
        movement = prediction["market_movement"]
        self.assertTrue(movement["measured"])
        for stage in ("dispatch", "first_response", "last_valid_response",
                      "completion"):
            self.assertIsNotNone(movement[f"{stage}_yes_ask"], stage)
        self.assertIsNotNone(movement["delta_to_first_response_yes_ask"])

    def test_configured_intervals_are_sampled_afterwards(self):
        with patch.object(CFG, "ALPHA_OBSERVATION_INTERVALS_S", "0.01,0.02"):
            self.assertEqual(observation_intervals(), [0.01, 0.02])
            self.emit()
            service = self.service()
            service.cycle()
            import time
            time.sleep(0.05)
            taken = service._take_due_observations()
        self.assertEqual(taken, 2)
        prediction = AlphaLedger().predictions()[0]
        observations = AlphaLedger().observations(prediction["prediction_id"])
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["quote"]["yes_ask"], 0.46)

    def test_an_observation_is_not_recorded_twice(self):
        with patch.object(CFG, "ALPHA_OBSERVATION_INTERVALS_S", "0.01"):
            self.emit()
            service = self.service()
            service.cycle()
            import time
            time.sleep(0.05)
            service._take_due_observations()
            prediction_id = AlphaLedger().predictions()[0]["prediction_id"]
            service.ledger.record_observation(prediction_id, interval_s=0.01,
                                              quote=dict(QUOTE))
        self.assertEqual(len(AlphaLedger().observations(prediction_id)), 1)

    def test_an_unavailable_book_is_not_recorded_as_a_price(self):
        def broken_quote():
            raise RuntimeError("book unavailable")
        with patch.object(CFG, "ALPHA_OBSERVATION_INTERVALS_S", "0.01"):
            self.emit()
            service = self.service(quote_fn=broken_quote)
            service.cycle()
            import time
            time.sleep(0.05)
            self.assertEqual(service._take_due_observations(), 0)
        self.assertEqual(AlphaLedger().observations(), [])

    def test_unreadable_intervals_are_ignored_not_fatal(self):
        with patch.object(CFG, "ALPHA_OBSERVATION_INTERVALS_S", "60,abc,,-5,300"):
            self.assertEqual(observation_intervals(), [60.0, 300.0])


class TelemetryCoversSection11(ServiceCase):

    def test_every_required_counter_is_exposed(self):
        self.emit()
        service = self.service()
        service.startup_report()
        service.cycle()
        payload = service.telemetry.snapshot()
        for counter in ("snapshots_received", "snapshots_deduplicated",
                        "provider_calls", "provider_success",
                        "provider_timeout", "provider_invalid",
                        "provider_stale", "p_meta_generated", "no_edge",
                        "positive_edge_low_confidence",
                        "positive_edge_high_confidence", "market_moved",
                        "model_disagreement", "resolved_predictions"):
            self.assertIn(counter, payload, counter)
        self.assertIn("provider_cost_usd", payload)
        self.assertIn("provider_cost_usd_total", payload)

    def test_counters_reflect_what_happened(self):
        self.emit()
        service = self.service(providers=[
            FakeProvider("grok"), FakeProvider("gemini"),
            FakeProvider("openai", error="down"),
            FakeProvider("atlas_quant", behaviour="{{{")])
        service.cycle()
        counters = service.telemetry.counters
        self.assertEqual(counters["snapshots_received"], 1)
        self.assertEqual(counters["provider_calls"], 4)
        self.assertEqual(counters["provider_success"], 2)
        self.assertEqual(counters["provider_invalid"], 2)
        self.assertEqual(counters["p_meta_generated"], 1)

    def test_daily_cost_is_exposed(self):
        self.emit()
        service = self.service()
        service.cycle()
        payload = service.telemetry.flush({"budget": service.budget.snapshot()})
        self.assertIn("spent_today_usd", payload["budget"])
        self.assertGreater(payload["provider_cost_usd_total"], 0.0)

    def test_the_snapshot_is_written_to_disk_for_a_monitor(self):
        self.emit()
        self.service().cycle()
        path = os.path.join(self._tmp, CFG.ALPHA_TELEMETRY_FILE)
        self.assertTrue(os.path.exists(path))
        payload = json.load(open(path))
        self.assertEqual(payload["schema"], "atlas-alpha-telemetry-v1")


if __name__ == "__main__":
    import unittest
    unittest.main()
