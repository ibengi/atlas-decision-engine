# -*- coding: utf-8 -*-
"""Real-provider activation: pricing provenance, billed cost, smoke test.

THE INVARIANTS
    1. A rate card carries its full provenance and its validity window. A
       card whose window has closed is NOT the current rate: the model
       becomes unpriced, and unpriced means not called.
    2. A vendor-reported billed amount and our rate-card estimate are BOTH
       kept. Neither silently wins; a disagreement is flagged.
    3. The budget is checked against the WORST CASE before dispatch, and
       charged the vendor's billed figure afterwards when one exists.
    4. The smoke test makes exactly one call per provider and reports what
       happened -- never a fallback to another model.
"""
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import (AlphaCase, http_session, valid_payload,      # noqa: E402
                    write_pricing)

from alpha_cost import (REASON_EXPIRED,                         # noqa: E402
                        BudgetGuard, BudgetLedger, PricingTable,
                        budgeted_cost, reconcile_billed_cost)
from alpha_providers import (GeminiProvider, GrokProvider,        # noqa: E402
                             OpenAIProvider, _xai_billed)
from config import CFG                                            # noqa: E402

SECRET = "sk-real-activation-DO-NOT-LOG"

#: The REAL adapters report their CONFIGURED model id, and the budget
#: guard looks the rate card up by that id -- so a case that drives a
#: real adapter must price the real model, not the provider name.
REAL_MODELS = (("grok", CFG.ALPHA_GROK_MODEL),
               ("gemini", CFG.ALPHA_GEMINI_MODEL),
               ("openai", CFG.ALPHA_OPENAI_MODEL))


def responses_body(payload, *, tokens=(1200, 300), cached=0, ticks=None,
                   status="completed"):
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1],
             "input_tokens_details": {"cached_tokens": cached}}
    if ticks is not None:
        usage["cost_in_usd_ticks"] = ticks
    return {"id": "resp_1", "object": "response", "status": status,
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": json.dumps(payload)}]}],
            "usage": usage}


def gemini_body(payload, *, tokens=(1200, 300), cached=0, thoughts=0):
    return {"candidates": [{"content": {"parts": [
                {"text": json.dumps(payload)}]}}],
            "usageMetadata": {"promptTokenCount": tokens[0],
                              "candidatesTokenCount": tokens[1],
                              "cachedContentTokenCount": cached,
                              "thoughtsTokenCount": thoughts}}


class RateCardsCarryProvenance(AlphaCase):

    def card(self, **over):
        entry = {"provider": "grok", "model": "grok-4.6",
                 "input_per_mtok": 2.0, "cached_input_per_mtok": 0.5,
                 "output_per_mtok": 6.0, "tool_call_usd": None,
                 "search_query_usd": None, "currency": "USD",
                 "effective_from": "2026-01-01T00:00:00+00:00",
                 "effective_until": None,
                 "source": "operator-supplied", "notes": ""}
        entry.update(over)
        path = os.path.join(self._tmp, "pricing.json")
        with open(path, "w") as fh:
            json.dump({"schema": "atlas-alpha-pricing-v2", "version": "v9",
                       "entries": [entry]}, fh)
        return PricingTable(path)

    def test_every_provenance_field_reaches_the_cost_row(self):
        row = self.card().price("grok", "grok-4.6", 1000, 1000)
        for field in ("pricing_version", "pricing_source", "currency",
                      "input_per_mtok", "cached_input_per_mtok",
                      "output_per_mtok", "tool_call_usd", "search_query_usd",
                      "effective_from", "effective_until", "priced_at"):
            self.assertIn(field, row, field)
        self.assertEqual(row["pricing_version"], "v9")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["pricing_source"], "operator-supplied")

    def test_cached_input_is_billed_at_the_cached_rate(self):
        row = self.card().price("grok", "grok-4.6", 1_000_000, 0,
                                cached_input_tokens=200_000)
        # 800k fresh at $2/M + 200k cached at $0.50/M
        self.assertAlmostEqual(row["api_cost_usd"], 1.6 + 0.1, places=8)

    def test_tool_and_search_rates_are_applied_when_configured(self):
        table = self.card(tool_call_usd=0.01, search_query_usd=0.02)
        row = table.price("grok", "grok-4.6", 0, 0, tool_calls=3,
                          search_queries=2)
        self.assertAlmostEqual(row["api_cost_usd"], 0.03 + 0.04, places=8)

    def test_an_expired_card_is_not_the_current_rate(self):
        """The Gemini window closes on 2026-12-31; using it in 2027 would
        price today at last year's numbers."""
        table = self.card(effective_until="2026-12-31T23:59:59+00:00")
        in_window = datetime(2026, 9, 9, tzinfo=timezone.utc)
        after = datetime(2027, 1, 2, tzinfo=timezone.utc)
        self.assertTrue(table.price("grok", "grok-4.6", 10, 10,
                                    at=in_window)["cost_priced"])
        expired = table.price("grok", "grok-4.6", 10, 10, at=after)
        self.assertFalse(expired["cost_priced"])
        self.assertIn("expired", expired["pricing_missing_reason"])

    def test_an_expired_card_refuses_the_call(self):
        table = self.card(effective_until="2026-01-02T00:00:00+00:00")
        guard = BudgetGuard(pricing=table,
                            ledger=BudgetLedger(os.path.join(self._tmp, "b.jsonl")))
        verdict = guard.check("grok", "grok-4.6")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], REASON_EXPIRED)

    def test_a_card_not_yet_effective_does_not_apply(self):
        table = self.card(effective_from="2099-01-01T00:00:00+00:00")
        row = table.price("grok", "grok-4.6", 10, 10)
        self.assertFalse(row["cost_priced"])
        self.assertIn("not effective until", row["pricing_missing_reason"])

    def test_an_unreadable_window_is_refused_not_ignored(self):
        """A malformed date makes its bound unenforceable, which is not the
        same as unbounded."""
        table = self.card(effective_until="not-a-date")
        row = table.price("grok", "grok-4.6", 10, 10)
        self.assertFalse(row["cost_priced"])
        self.assertIn("unreadable", row["pricing_missing_reason"])

    def test_the_shipped_card_file_prices_every_shipped_model(self):
        table = PricingTable(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "alpha_pricing.json"))
        self.assertTrue(table.loaded, table.error)
        active = table.configured_models(datetime(2026, 9, 9,
                                                  tzinfo=timezone.utc))
        for key in (f"grok/{CFG.ALPHA_GROK_MODEL}",
                    f"gemini/{CFG.ALPHA_GEMINI_MODEL}",
                    f"openai/{CFG.ALPHA_OPENAI_MODEL}"):
            self.assertIn(key, active, key)

    def test_the_gemini_card_stops_applying_after_its_window(self):
        table = PricingTable(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "alpha_pricing.json"))
        after = datetime(2027, 1, 2, tzinfo=timezone.utc)
        self.assertNotIn(f"gemini/{CFG.ALPHA_GEMINI_MODEL}",
                         table.configured_models(after))
        self.assertIn(f"gemini/{CFG.ALPHA_GEMINI_MODEL}",
                      table.expired_models(after))


class BilledCostIsReconciledNotReplaced(AlphaCase):

    def test_xai_ticks_are_recorded_raw(self):
        billed = _xai_billed({"cost_in_usd_ticks": 12345})
        self.assertEqual(billed["billed_cost_raw"],
                         {"unit": "cost_in_usd_ticks", "value": 12345})
        self.assertEqual(billed["billed_cost_source"],
                         "xai.usage.cost_in_usd_ticks")

    def test_no_usd_figure_is_derived_until_the_scale_is_configured(self):
        """Guessing the tick denomination wrong by a factor of ten would
        misprice every call."""
        with patch.object(CFG, "ALPHA_XAI_COST_TICKS_PER_USD", 0.0):
            billed = _xai_billed({"cost_in_usd_ticks": 12345})
        self.assertNotIn("billed_cost_usd", billed)
        self.assertIn("unset", billed["billed_cost_note"])

    def test_a_configured_scale_produces_a_usd_figure(self):
        with patch.object(CFG, "ALPHA_XAI_COST_TICKS_PER_USD", 1_000_000.0):
            billed = _xai_billed({"cost_in_usd_ticks": 12345})
        self.assertAlmostEqual(billed["billed_cost_usd"], 0.012345, places=9)

    def test_a_missing_tick_field_is_not_a_zero_cost(self):
        self.assertEqual(_xai_billed({}), {})
        self.assertEqual(_xai_billed({"cost_in_usd_ticks": None}), {})

    def test_both_figures_survive_reconciliation(self):
        row = reconcile_billed_cost(
            {"api_cost_usd": 0.010, "provider": "grok", "model": "m"},
            {"billed_cost_usd": 0.0105})
        self.assertEqual(row["api_cost_usd"], 0.010)
        self.assertEqual(row["billed_cost_usd"], 0.0105)
        self.assertTrue(row["cost_reconciled"])
        self.assertIsNone(row["cost_reconciliation"])

    def test_a_large_disagreement_is_flagged_not_resolved(self):
        row = reconcile_billed_cost(
            {"api_cost_usd": 0.010, "provider": "grok", "model": "m"},
            {"billed_cost_usd": 1.0})
        self.assertIsNotNone(row["cost_reconciliation"])
        self.assertIn("disagree", row["cost_reconciliation"])
        # neither figure was overwritten
        self.assertEqual(row["api_cost_usd"], 0.010)
        self.assertEqual(row["billed_cost_usd"], 1.0)

    def test_the_budget_is_charged_the_vendors_figure_when_it_exists(self):
        self.assertAlmostEqual(
            budgeted_cost({"api_cost_usd": 0.01, "billed_cost_usd": 0.03}),
            0.03, places=9)
        self.assertAlmostEqual(budgeted_cost({"api_cost_usd": 0.01}),
                               0.01, places=9)

    def test_a_grok_call_carries_ticks_through_to_the_cost_row(self):
        os.environ["XAI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), rates=(2.0, 6.0),
            models=("grok",)))
        with patch("alpha_providers.pricing_table", return_value=table), \
             patch.object(CFG, "ALPHA_GROK_MODEL", "grok"):
            provider = GrokProvider(session=http_session(
                responses_body(valid_payload(snapshot), ticks=98765)))
            raw, meta = provider.analyze(snapshot, 5.0)
        self.assertIsNone(meta["error"])
        self.assertEqual(meta["cost"]["billed_cost_raw"]["value"], 98765)
        self.assertTrue(meta["cost"]["cost_priced"])


class TheResponsesAdaptersHandleRealShapes(AlphaCase):

    def setUp(self):
        super().setUp()
        self.table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), rates=(2.0, 6.0),
            models=REAL_MODELS))

    def test_grok_uses_the_responses_endpoint(self):
        os.environ["XAI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        session = http_session(responses_body(valid_payload(snapshot)))
        with patch("alpha_providers.pricing_table", return_value=self.table):
            GrokProvider(session=session).analyze(snapshot, 5.0)
        post = session.posts[0]
        self.assertTrue(post["url"].endswith("/responses"), post["url"])
        self.assertTrue(post["url"].startswith("https://api.x.ai/v1"))
        self.assertIn("input", post["json"])
        self.assertEqual(post["json"]["model"], CFG.ALPHA_GROK_MODEL)

    def test_both_responses_adapters_bound_the_output(self):
        """The budget was checked against this ceiling, so the provider is
        held to it rather than trusted to be typical."""
        for cls, env in ((GrokProvider, "XAI_API_KEY"),
                         (OpenAIProvider, "OPENAI_API_KEY")):
            with self.subTest(provider=cls.name):
                os.environ[env] = SECRET
                snapshot = self.snapshot()
                session = http_session(responses_body(valid_payload(snapshot)))
                with patch("alpha_providers.pricing_table",
                           return_value=self.table):
                    cls(session=session).analyze(snapshot, 5.0)
                self.assertEqual(session.posts[0]["json"]["max_output_tokens"],
                                 int(CFG.ALPHA_MAX_OUTPUT_TOKENS))

    def test_an_incomplete_response_is_a_failure_not_half_an_opinion(self):
        os.environ["OPENAI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        body = responses_body(valid_payload(snapshot), status="incomplete")
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        with patch("alpha_providers.pricing_table", return_value=self.table):
            raw, meta = OpenAIProvider(
                session=http_session(body)).analyze(snapshot, 5.0)
        self.assertIsNone(raw)
        self.assertIn("max_output_tokens", meta["error"])

    def test_cached_tokens_are_captured_from_both_shapes(self):
        os.environ["OPENAI_API_KEY"] = SECRET
        os.environ["GEMINI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        with patch("alpha_providers.pricing_table", return_value=self.table):
            _, openai_meta = OpenAIProvider(session=http_session(
                responses_body(valid_payload(snapshot), cached=400))).analyze(
                    snapshot, 5.0)
            _, gemini_meta = GeminiProvider(session=http_session(
                gemini_body(valid_payload(snapshot), cached=250))).analyze(
                    snapshot, 5.0)
        self.assertEqual(openai_meta["cost"]["cached_input_tokens"], 400)
        self.assertEqual(gemini_meta["cost"]["cached_input_tokens"], 250)

    def test_gemini_thinking_tokens_are_billed_as_output(self):
        os.environ["GEMINI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        with patch("alpha_providers.pricing_table", return_value=self.table):
            _, meta = GeminiProvider(session=http_session(
                gemini_body(valid_payload(snapshot), tokens=(1000, 200),
                            thoughts=800))).analyze(snapshot, 5.0)
        self.assertEqual(meta["cost"]["output_tokens"], 1000)

    def test_gemini_bounds_its_output_too(self):
        os.environ["GEMINI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        session = http_session(gemini_body(valid_payload(snapshot)))
        with patch("alpha_providers.pricing_table", return_value=self.table):
            GeminiProvider(session=session).analyze(snapshot, 5.0)
        config = session.posts[0]["json"]["generationConfig"]
        self.assertEqual(config["maxOutputTokens"],
                         int(CFG.ALPHA_MAX_OUTPUT_TOKENS))

    def test_the_gemini_key_is_read_from_either_variable(self):
        provider = GeminiProvider()
        self.assertFalse(provider.configured())
        os.environ["GOOGLE_GEMINI_API_KEY"] = SECRET
        self.assertTrue(provider.configured())
        os.environ.pop("GOOGLE_GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = SECRET
        self.assertTrue(provider.configured())


class TheSmokeTestReportsWhatHappened(AlphaCase):
    """Section 4, driven through the real `probe()` with a synthetic
    transport -- so the harness itself is proven to work before it is
    pointed at a real vendor."""

    def setUp(self):
        super().setUp()
        self.table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), rates=(2.0, 6.0),
            models=REAL_MODELS))
        self.guard = BudgetGuard(pricing=self.table,
                                 ledger=BudgetLedger(
                                     os.path.join(self._tmp, "b.jsonl")))

    def probe(self, provider):
        from tools.alpha_smoke_test import fixture_snapshot, probe
        with patch("alpha_providers.pricing_table", return_value=self.table):
            return probe(provider, fixture_snapshot(), self.guard)

    def test_a_working_provider_passes_with_every_field_captured(self):
        os.environ["XAI_API_KEY"] = SECRET
        from tools.alpha_smoke_test import fixture_snapshot
        snapshot = fixture_snapshot()
        provider = GrokProvider(session=http_session(
            responses_body(valid_payload(snapshot), ticks=54321)))
        result = self.probe(provider)
        self.assertEqual(result["verdict"], "PASS", result["detail"])
        self.assertTrue(result["credential_present"])
        self.assertTrue(result["pricing_configured"])
        self.assertTrue(result["called"])
        self.assertTrue(result["reachable"])
        self.assertTrue(result["response_parsable"])
        self.assertTrue(result["schema_valid"])
        self.assertTrue(result["usage_captured"])
        self.assertTrue(result["latency_captured"])
        self.assertTrue(result["cost_captured"])
        self.assertIsNotNone(result["p_yes"])
        # the one number only a real call can supply
        self.assertIsNotNone(result["xai_ticks_per_usd_observed"])

    def test_a_missing_credential_stops_before_any_call(self):
        provider = GrokProvider(session=http_session({}))
        result = self.probe(provider)
        self.assertEqual(result["verdict"], "NOT_EXECUTED")
        self.assertFalse(result["called"])
        self.assertIn("XAI_API_KEY", result["detail"])

    def test_an_unreachable_provider_fails_without_substitution(self):
        os.environ["OPENAI_API_KEY"] = SECRET
        provider = OpenAIProvider(session=http_session(
            {}, raises=OSError("Tunnel connection failed: 403 Forbidden")))
        result = self.probe(provider)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("403", result["detail"])
        # the model it was asked about is the model it reports
        self.assertEqual(result["model"], CFG.ALPHA_OPENAI_MODEL)

    def test_a_reachable_provider_returning_junk_is_not_a_pass(self):
        os.environ["GEMINI_API_KEY"] = SECRET
        provider = GeminiProvider(session=http_session(
            gemini_body({"not": "a signal"})))
        result = self.probe(provider)
        self.assertEqual(result["verdict"], "REACHABLE_BUT_INVALID")
        self.assertTrue(result["reachable"])
        self.assertFalse(result["schema_valid"])
        self.assertIsNone(result["p_yes"])

    def test_the_smoke_test_is_charged_against_the_budget(self):
        """It cannot be used to sidestep the daily cap."""
        os.environ["XAI_API_KEY"] = SECRET
        from tools.alpha_smoke_test import fixture_snapshot
        provider = GrokProvider(session=http_session(
            responses_body(valid_payload(fixture_snapshot()))))
        self.probe(provider)
        self.assertGreater(self.guard.ledger.spent_today(), 0.0)

    def test_an_exhausted_budget_refuses_the_smoke_call(self):
        os.environ["XAI_API_KEY"] = SECRET
        self.guard.ledger.record({"provider": "grok", "api_cost_usd": 1000.0})
        provider = GrokProvider(session=http_session({}))
        with patch.object(CFG, "ALPHA_MAX_COST_PER_DAY_USD", 1.0):
            result = self.probe(provider)
        self.assertTrue(result["verdict"].startswith("REFUSED_"))
        self.assertFalse(result["called"])

    def test_no_smoke_test_field_carries_the_key(self):
        """The report is printed and may be pasted into a ticket."""
        os.environ["XAI_API_KEY"] = SECRET
        from tools.alpha_smoke_test import fixture_snapshot
        for session in (
                http_session(responses_body(valid_payload(fixture_snapshot()),
                                            ticks=54321)),
                http_session({}, raises=OSError(f"auth failed for {SECRET}")),
                http_session({"unexpected": "shape"})):
            result = self.probe(GrokProvider(session=session))
            self.assertNotIn(SECRET, json.dumps(result, default=str),
                             result["verdict"])

    def test_the_fixture_describes_no_real_market(self):
        from tools.alpha_smoke_test import FIXTURE_CONTRACT, fixture_snapshot
        snapshot = fixture_snapshot()
        self.assertEqual(snapshot.contract_id, FIXTURE_CONTRACT)
        self.assertIn("SMOKE-TEST", snapshot.contract_id)
        self.assertEqual(snapshot.resolution_source, "synthetic")
        self.assertIn("not a real market", snapshot.resolution_rules)


class TheCredentialValueNeverLeavesTheAdapter(AlphaCase):
    """Section 5. `_safe_body` covers a vendor echoing our header back in a
    RESPONSE; this covers the other route -- a transport or proxy raising an
    EXCEPTION that quotes the request."""

    def test_a_transport_exception_quoting_the_key_is_redacted(self):
        os.environ["XAI_API_KEY"] = SECRET
        table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), models=REAL_MODELS))
        snapshot = self.snapshot()
        session = http_session({}, raises=OSError(
            f"ProxyError: CONNECT failed, sent Authorization: Bearer {SECRET}"))
        with patch("alpha_providers.pricing_table", return_value=table):
            _, meta = GrokProvider(session=session).analyze(snapshot, 5.0)
        self.assertNotIn(SECRET, meta["error"])
        # the failure itself is still reported -- redaction is not silence
        self.assertIn("CONNECT failed", meta["error"])
        self.assertIn("<redacted:XAI_API_KEY>", meta["error"])

    def test_another_providers_key_is_stripped_too(self):
        """A shared session or a proxy can quote a different vendor."""
        os.environ["OPENAI_API_KEY"] = SECRET
        table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), models=REAL_MODELS))
        os.environ["GEMINI_API_KEY"] = "gm-" + SECRET
        session = http_session({}, raises=OSError(f"proxy saw gm-{SECRET}"))
        with patch("alpha_providers.pricing_table", return_value=table):
            _, meta = OpenAIProvider(session=session).analyze(
                self.snapshot(), 5.0)
        self.assertNotIn(SECRET, meta["error"])

    def test_redaction_does_not_fire_on_a_short_or_absent_value(self):
        """An empty or trivially short key must not turn every message into
        a redaction marker -- that would destroy the diagnostic."""
        from alpha_providers import redact
        os.environ.pop("XAI_API_KEY", None)
        self.assertEqual(redact("HTTP 500: upstream error"),
                         "HTTP 500: upstream error")
        os.environ["XAI_API_KEY"] = "abc"
        self.assertEqual(redact("abc happens to appear"),
                         "abc happens to appear")


class PerProviderTelemetry(AlphaCase):
    """Section 8: each provider independently. An aggregate hides one vendor
    timing out while another answers."""

    def test_every_required_field_is_exposed_per_provider(self):
        from _alpha import FakeProvider
        from alpha_gateway import AlphaGateway
        from alpha_ledger import AlphaLedger
        from alpha_telemetry import Telemetry
        table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), rates=(2.0, 6.0),
            models=("grok", "gemini", "openai")))
        telemetry = Telemetry(os.path.join(self._tmp, "t.json"))
        with patch("alpha_providers.pricing_table", return_value=table):
            AlphaGateway(providers=[FakeProvider("grok", latency_ms=300),
                                    FakeProvider("gemini", error="timeout"),
                                    FakeProvider("openai", behaviour="{{")],
                         ledger=AlphaLedger()).analyze(
                self.snapshot(), record=False,
                on_signal=telemetry.record_signal)
        payload = telemetry.snapshot()["by_provider"]
        for provider in ("grok", "gemini", "openai"):
            self.assertIn(provider, payload)
            for field in ("calls", "success", "timeouts", "invalid",
                          "input_tokens", "output_tokens",
                          "estimated_cost_usd", "billed_cost_usd",
                          "cost_usd", "average_latency_ms", "success_rate"):
                self.assertIn(field, payload[provider], f"{provider}.{field}")
        self.assertEqual(payload["grok"]["success"], 1)
        self.assertEqual(payload["gemini"]["timeouts"], 1)
        self.assertEqual(payload["openai"]["invalid"], 1)
        self.assertEqual(payload["grok"]["average_latency_ms"], 300.0)
        self.assertGreater(payload["grok"]["estimated_cost_usd"], 0.0)

    def test_no_telemetry_counter_can_carry_a_key(self):
        """Section 5: secrets must not reach telemetry. Telemetry is
        numeric per provider, so the check is that the whole persisted
        snapshot is free of the credential even after a failing call that
        the vendor answered by echoing the header back."""
        from _alpha import FakeProvider
        from alpha_gateway import AlphaGateway
        from alpha_ledger import AlphaLedger
        from alpha_telemetry import Telemetry
        os.environ["XAI_API_KEY"] = SECRET
        path = os.path.join(self._tmp, "t.json")
        telemetry = Telemetry(path)
        table = PricingTable(write_pricing(
            os.path.join(self._tmp, "p.json"), rates=(2.0, 6.0),
            models=("grok", "gemini", "openai")))
        with patch("alpha_providers.pricing_table", return_value=table):
            AlphaGateway(providers=[
                FakeProvider("grok", error=f"401 for {SECRET}"),
                FakeProvider("gemini", latency_ms=200),
                FakeProvider("openai", behaviour="{{")],
                ledger=AlphaLedger()).analyze(
                    self.snapshot(), record=False,
                    on_signal=telemetry.record_signal)
        self.assertNotIn(SECRET, json.dumps(telemetry.snapshot(), default=str))
        telemetry.flush()
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn(SECRET, fh.read())

    def test_billed_and_estimated_are_tracked_separately(self):
        from alpha_schema import AlphaSignal
        from alpha_telemetry import Telemetry
        telemetry = Telemetry(os.path.join(self._tmp, "t.json"))
        signal = AlphaSignal(
            schema_version="atlas-alpha-v2", market_snapshot_id="s",
            contract_id="c", model="grok-4.6", model_version="1",
            generated_at_utc="2026-09-09T00:00:00+00:00", p_yes=0.5,
            probability_low=0.4, probability_high=0.6, confidence=0.5,
            evidence_quality=0.5, data_completeness=0.5,
            analysis_latency_ms=250, valid_until_utc="2026-09-09T01:00:00+00:00",
            status="VALID", provider="grok",
            cost={"provider": "grok", "api_cost_usd": 0.01,
                  "billed_cost_usd": 0.013, "input_tokens": 100,
                  "output_tokens": 50})
        telemetry.record_signal(signal)
        slot = telemetry.snapshot()["by_provider"]["grok"]
        self.assertAlmostEqual(slot["estimated_cost_usd"], 0.01, places=9)
        self.assertAlmostEqual(slot["billed_cost_usd"], 0.013, places=9)
        # the budget is charged the vendor's number
        self.assertAlmostEqual(slot["cost_usd"], 0.013, places=9)


if __name__ == "__main__":
    import unittest
    unittest.main()
