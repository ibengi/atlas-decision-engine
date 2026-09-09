# -*- coding: utf-8 -*-
"""Alpha Gateway sections 3, 12 and 19 — the provider adapters.

Every case drives the REAL adapter through a synthetic session, so request
construction, header assembly, response parsing and token accounting are
exercised without a socket. `tests/_netblock.py` blocks the rest.

THE INVARIANTS
    1. An API key is read at call time, sent in one header, and appears in
       no log line, no exception, no returned value and no persisted row.
    2. An unparseable or empty vendor response is a provider FAILURE, never
       a probability.
    3. Token counts and latency are recorded on every call.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase, http_session, valid_payload     # noqa: E402
from unittest.mock import patch                               # noqa: E402

from alpha_providers import (AtlasQuantProvider, GeminiProvider,   # noqa: E402
                             GrokProvider, OpenAIProvider,
                             _ChatCompletions, build_prompt,
                             default_providers)
from alpha_schema import SCHEMA_VERSION, validate_signal      # noqa: E402
from config import CFG                                        # noqa: E402

SECRET = "sk-test-DO-NOT-LOG-abcdef123456"


def chat_response(body_dict, tokens=(1000, 250)):
    return {"choices": [{"message": {"content": json.dumps(body_dict)}}],
            "usage": {"prompt_tokens": tokens[0],
                      "completion_tokens": tokens[1]}}


def responses_api_response(body_dict, tokens=(1000, 250), status="completed"):
    """The OpenAI Responses API shape: `output[].content[].text`, and usage
    named input_tokens/output_tokens rather than prompt/completion."""
    return {"id": "resp_1", "object": "response", "status": status,
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": json.dumps(body_dict)}]}],
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                      "total_tokens": sum(tokens)}}


def gemini_response(body_dict, tokens=(1000, 250)):
    return {"candidates": [{"content": {"parts": [
                {"text": json.dumps(body_dict)}]}}],
            "usageMetadata": {"promptTokenCount": tokens[0],
                              "candidatesTokenCount": tokens[1]}}


class _ChatGrok(_ChatCompletions):
    """The chat-completions surface, kept as a ready fallback.

    No shipped provider uses it -- both xAI and OpenAI go through their
    Responses APIs -- but a vendor-side problem there should be one
    configuration change away from being routed around, not a rewrite, so
    the parser stays covered.
    """
    name = "grok"
    env_key = "XAI_API_KEY"
    base_url_attr = "ALPHA_GROK_BASE_URL"

    def default_model(self):
        return CFG.ALPHA_GROK_MODEL


class ChatCompletionAdapters(AlphaCase):
    """The fallback chat-completions parser."""

    def cases(self):
        return ((_ChatGrok, "XAI_API_KEY", CFG.ALPHA_GROK_BASE_URL),)

    def test_a_well_formed_answer_round_trips(self):
        snapshot = self.snapshot()
        for cls, env, base in self.cases():
            with self.subTest(provider=cls.name):
                os.environ[env] = SECRET
                session = http_session(chat_response(valid_payload(snapshot)))
                raw, meta = cls(session=session).analyze(snapshot, 5.0)
                self.assertIsNone(meta["error"])
                signal = validate_signal(raw, snapshot, provider=cls.name,
                                         model=meta["model"])
                self.assertTrue(signal.valid, signal.rejected_detail)
                self.assertEqual(meta["cost"]["input_tokens"], 1000)
                self.assertEqual(meta["cost"]["output_tokens"], 250)
                self.assertGreaterEqual(meta["latency_ms"], 0)
                self.assertTrue(session.posts[0]["url"].startswith(base))

    def test_the_key_travels_in_the_authorization_header_only(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session(chat_response(valid_payload(snapshot)))
        _ChatGrok(session=session).analyze(snapshot, 5.0)
        post = session.posts[0]
        self.assertEqual(post["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, json.dumps(post["json"]))
        self.assertNotIn(SECRET, post["url"])

    def test_the_timeout_is_passed_to_the_transport(self):
        """An adapter that issues an untimed request would hang a worker
        past the dispatcher's deadline."""
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session(chat_response(valid_payload(snapshot)))
        _ChatGrok(session=session).analyze(snapshot, 3.5)
        self.assertEqual(session.posts[0]["timeout"], 3.5)

    def test_an_http_error_is_a_provider_failure(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session({"error": "rate limited"}, status=429)
        raw, meta = _ChatGrok(session=session).analyze(snapshot, 5.0)
        self.assertIsNone(raw)
        self.assertIn("HTTP 429", meta["error"])

    def test_a_transport_exception_is_caught(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session({}, raises=OSError("connection reset"))
        raw, meta = _ChatGrok(session=session).analyze(snapshot, 5.0)
        self.assertIsNone(raw)
        self.assertIn("connection reset", meta["error"])

    def test_unusable_response_shapes_are_failures_not_probabilities(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        for label, body in (("no choices", {"usage": {}}),
                            ("empty choices", {"choices": []}),
                            ("no message", {"choices": [{}]}),
                            ("empty content",
                             {"choices": [{"message": {"content": "  "}}]}),
                            ("not an object", ["choices"])):
            with self.subTest(case=label):
                session = http_session(body)
                raw, meta = _ChatGrok(session=session).analyze(snapshot, 5.0)
                self.assertIsNone(raw)
                self.assertIsNotNone(meta["error"])

    def test_missing_usage_counts_as_zero_tokens_not_a_crash(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session({"choices": [{"message": {
            "content": json.dumps(valid_payload(snapshot))}}]})
        raw, meta = _ChatGrok(session=session).analyze(snapshot, 5.0)
        self.assertIsNotNone(raw)
        self.assertEqual(meta["cost"]["input_tokens"], 0)


class OpenAIResponsesAdapter(AlphaCase):
    """Section 3: OpenAI is called through the Responses API."""

    def test_the_request_uses_the_responses_endpoint_and_input_field(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        session = http_session(responses_api_response(valid_payload(snapshot)))
        OpenAIProvider(session=session).analyze(snapshot, 4.0)
        post = session.posts[0]
        self.assertTrue(post["url"].endswith("/responses"), post["url"])
        self.assertIn("input", post["json"])
        self.assertNotIn("messages", post["json"])
        self.assertEqual(post["timeout"], 4.0)

    def test_a_well_formed_answer_round_trips(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        session = http_session(responses_api_response(valid_payload(snapshot)))
        raw, meta = OpenAIProvider(session=session).analyze(snapshot, 5.0)
        self.assertIsNone(meta["error"])
        self.assertTrue(validate_signal(raw, snapshot, provider="openai",
                                        model=meta["model"]).valid)
        self.assertEqual(meta["cost"]["input_tokens"], 1000)
        self.assertEqual(meta["cost"]["output_tokens"], 250)

    def test_the_output_text_convenience_field_is_preferred(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        body = responses_api_response(valid_payload(snapshot))
        body["output_text"] = json.dumps(valid_payload(snapshot, p_yes=0.42,
                                                       low=0.40, high=0.44))
        session = http_session(body)
        raw, meta = OpenAIProvider(session=session).analyze(snapshot, 5.0)
        signal = validate_signal(raw, snapshot, provider="openai", model="m")
        self.assertAlmostEqual(signal.p_yes, 0.42, places=6)

    def test_reasoning_and_tool_items_are_counted_not_parsed_as_text(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        body = responses_api_response(valid_payload(snapshot))
        body["output"] = ([{"type": "reasoning", "summary": []},
                           {"type": "web_search_call", "status": "completed"}]
                          + body["output"])
        session = http_session(body)
        raw, meta = OpenAIProvider(session=session).analyze(snapshot, 5.0)
        self.assertTrue(validate_signal(raw, snapshot, provider="openai",
                                        model="m").valid)
        self.assertEqual(meta["cost"]["tool_calls"], 2)

    def test_a_failed_response_status_is_a_provider_failure(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        body = responses_api_response(valid_payload(snapshot), status="failed")
        body["error"] = {"message": "model overloaded"}
        raw, meta = OpenAIProvider(session=http_session(body)).analyze(
            snapshot, 5.0)
        self.assertIsNone(raw)
        self.assertIn("model overloaded", meta["error"])

    def test_an_empty_output_is_a_failure_not_a_probability(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        for body in ({"status": "completed", "output": []},
                     {"status": "completed"},
                     {"status": "completed", "output": [{"type": "message",
                                                         "content": []}]}):
            with self.subTest(body=str(body)[:40]):
                raw, meta = OpenAIProvider(
                    session=http_session(body)).analyze(snapshot, 5.0)
                self.assertIsNone(raw)
                self.assertIsNotNone(meta["error"])

    def test_the_path_is_configurable(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        with patch.object(CFG, "ALPHA_OPENAI_RESPONSES_PATH", "/v2/answer"):
            session = http_session(responses_api_response(
                valid_payload(snapshot)))
            OpenAIProvider(session=session).analyze(snapshot, 5.0)
            self.assertTrue(session.posts[0]["url"].endswith("/v2/answer"))


class GeminiAdapter(AlphaCase):

    def test_a_well_formed_answer_round_trips(self):
        snapshot = self.snapshot()
        os.environ["GEMINI_API_KEY"] = SECRET
        session = http_session(gemini_response(valid_payload(snapshot)))
        raw, meta = GeminiProvider(session=session).analyze(snapshot, 5.0)
        self.assertIsNone(meta["error"])
        self.assertTrue(validate_signal(raw, snapshot, provider="gemini",
                                        model=meta["model"]).valid)
        self.assertEqual(meta["cost"]["input_tokens"], 1000)
        self.assertEqual(meta["cost"]["output_tokens"], 250)

    def test_the_key_travels_in_its_own_header_not_the_url(self):
        """A key in a query string ends up in every access log on the path."""
        snapshot = self.snapshot()
        os.environ["GEMINI_API_KEY"] = SECRET
        session = http_session(gemini_response(valid_payload(snapshot)))
        GeminiProvider(session=session).analyze(snapshot, 5.0)
        post = session.posts[0]
        self.assertEqual(post["headers"]["x-goog-api-key"], SECRET)
        self.assertNotIn(SECRET, post["url"])

    def test_multipart_text_is_joined(self):
        snapshot = self.snapshot()
        os.environ["GEMINI_API_KEY"] = SECRET
        body = json.dumps(valid_payload(snapshot))
        session = http_session({"candidates": [{"content": {"parts": [
            {"text": body[:40]}, {"text": body[40:]}]}}],
            "usageMetadata": {}})
        raw, meta = GeminiProvider(session=session).analyze(snapshot, 5.0)
        self.assertTrue(validate_signal(raw, snapshot, provider="gemini",
                                        model="m").valid)


class SecretsNeverLeak(AlphaCase):
    """Section 19, checked at every exit a key could take."""

    def test_a_secret_is_never_in_an_error_body_excerpt(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        # A vendor echoing the request headers back inside a 400 body.
        session = http_session({"error": {"message": "bad auth",
                                          "authorization": f"Bearer {SECRET}"}},
                               status=400)
        raw, meta = _ChatGrok(session=session).analyze(snapshot, 5.0)
        self.assertIsNone(raw)
        self.assertNotIn(SECRET, meta["error"])
        self.assertIn("redacted", meta["error"])

    def test_a_secret_is_never_in_the_returned_metadata(self):
        snapshot = self.snapshot()
        os.environ["OPENAI_API_KEY"] = SECRET
        session = http_session(chat_response(valid_payload(snapshot)))
        raw, meta = OpenAIProvider(session=session).analyze(snapshot, 5.0)
        self.assertNotIn(SECRET, json.dumps(meta))
        self.assertNotIn(SECRET, str(raw))

    def test_a_secret_is_never_in_a_persisted_ledger_row(self):
        from alpha_gateway import AlphaGateway
        from alpha_ledger import AlphaLedger
        os.environ["XAI_API_KEY"] = SECRET
        snapshot = self.snapshot()
        session = http_session(chat_response(valid_payload(snapshot)))
        AlphaGateway(providers=[GrokProvider(session=session)],
                     ledger=AlphaLedger()).analyze(snapshot)
        for name in (CFG.ALPHA_LEDGER_FILE, CFG.ALPHA_COST_FILE):
            path = os.path.join(self._tmp, name)
            if os.path.exists(path):
                self.assertNotIn(SECRET, open(path).read())

    def test_a_secret_is_never_in_a_log_line(self):
        snapshot = self.snapshot()
        os.environ["XAI_API_KEY"] = SECRET
        session = http_session({"error": f"Bearer {SECRET} rejected"},
                               status=401)
        with self.assertLogs("ALPHA", level="DEBUG") as captured:
            from alpha_dispatcher import dispatch
            dispatch(snapshot, [_ChatGrok(session=session)])
        self.assertNotIn(SECRET, "\n".join(captured.output))

    def test_no_secret_appears_in_the_prompt(self):
        os.environ["XAI_API_KEY"] = SECRET
        prompt = build_prompt(self.snapshot())
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn("XAI_API_KEY", prompt)

    def test_an_absent_key_is_reported_by_NAME_not_by_value(self):
        provider = GrokProvider()
        raw, meta = provider.analyze(self.snapshot(), 5.0)
        self.assertIsNone(raw)
        self.assertIn("XAI_API_KEY", meta["error"])
        self.assertNotIn(SECRET, meta["error"])


class ThePromptForbidsInstructions(AlphaCase):

    def test_the_prompt_tells_the_model_not_to_trade(self):
        prompt = build_prompt(self.snapshot())
        self.assertIn("Do NOT include any trading instruction", prompt)
        self.assertIn("probability, not a decision", prompt)

    def test_the_prompt_carries_the_immutable_identifiers(self):
        snapshot = self.snapshot()
        prompt = build_prompt(snapshot)
        self.assertIn(snapshot.market_snapshot_id, prompt)
        self.assertIn(snapshot.contract_id, prompt)
        self.assertIn(SCHEMA_VERSION, prompt)

    def test_every_provider_receives_the_identical_prompt(self):
        """A difference between two answers must be a difference of model,
        not of prompt -- even though the three request shapes differ."""
        snapshot = self.snapshot()
        prompts = set()
        bodies = {GrokProvider: responses_api_response,
                  OpenAIProvider: responses_api_response,
                  GeminiProvider: gemini_response}
        for cls, env in ((GrokProvider, "XAI_API_KEY"),
                         (OpenAIProvider, "OPENAI_API_KEY"),
                         (GeminiProvider, "GEMINI_API_KEY")):
            os.environ[env] = SECRET
            session = http_session(bodies[cls](valid_payload(snapshot)))
            cls(session=session).analyze(snapshot, 5.0)
            body = session.posts[0]["json"]
            if "messages" in body:
                text = body["messages"][0]["content"]
            elif "input" in body:
                text = body["input"]
            else:
                text = body["contents"][0]["parts"][0]["text"]
            prompts.add(text)
        self.assertEqual(len(prompts), 1, "providers received different prompts")


class TheQuantAdapterIsNotPrivileged(AlphaCase):
    """It goes through the same schema and the same validator as the LLMs."""

    def test_an_unwired_quant_model_reports_insufficient_evidence(self):
        """The default is deliberately NOT a model. A placeholder returning
        the market price would manufacture agreement with the market and
        make the ensemble look calibrated while measuring nothing."""
        snapshot = self.snapshot()
        raw, meta = AtlasQuantProvider().analyze(snapshot, 5.0)
        signal = validate_signal(raw, snapshot, provider="atlas_quant",
                                 model="atlas_quant")
        self.assertFalse(signal.valid)
        self.assertEqual(signal.rejected_reason,
                         "model_status_insufficient_evidence")
        self.assertIsNone(signal.p_yes)

    def test_a_wired_estimator_produces_a_valid_signal(self):
        snapshot = self.snapshot()
        provider = AtlasQuantProvider(
            estimator=lambda view: (0.62, 0.58, 0.66, 0.75),
            model_version="btc15m-1.2")
        raw, meta = provider.analyze(snapshot, 5.0)
        signal = validate_signal(raw, snapshot, provider="atlas_quant",
                                 model="atlas_quant")
        self.assertTrue(signal.valid, signal.rejected_detail)
        self.assertAlmostEqual(signal.p_yes, 0.62, places=6)
        self.assertEqual(signal.model_version, "btc15m-1.2")
        self.assertTrue(meta["cost"]["cost_priced"])
        self.assertEqual(meta["cost"]["api_cost_usd"], 0.0)

    def test_a_quant_model_returning_nonsense_is_excluded_like_any_vendor(self):
        snapshot = self.snapshot()
        provider = AtlasQuantProvider(
            estimator=lambda view: (1.4, 0.5, 1.4, 0.9))
        raw, meta = provider.analyze(snapshot, 5.0)
        signal = validate_signal(raw, snapshot, provider="atlas_quant",
                                 model="atlas_quant")
        self.assertFalse(signal.valid)
        self.assertIsNone(signal.p_yes)

    def test_a_raising_estimator_does_not_crash_the_adapter(self):
        def boom(view):
            raise ZeroDivisionError("bad model")
        raw, meta = AtlasQuantProvider(estimator=boom).analyze(
            self.snapshot(), 5.0)
        self.assertIsNotNone(meta["error"])
        self.assertIn("ZeroDivisionError", meta["error"])

    def test_the_estimator_receives_a_copy_it_cannot_use_to_mutate(self):
        seen = {}

        def estimator(view):
            view["yes_ask"] = 0.99
            seen.update(view)
            return None
        snapshot = self.snapshot()
        AtlasQuantProvider(estimator=estimator).analyze(snapshot, 5.0)
        self.assertEqual(seen["yes_ask"], 0.99)
        self.assertAlmostEqual(snapshot.yes_ask, 0.46, places=6)


class TheDefaultRoster(AlphaCase):

    def test_four_providers_in_a_fixed_order(self):
        providers = default_providers()
        self.assertEqual([p.name for p in providers],
                         ["grok", "gemini", "openai", "atlas_quant"])

    def test_endpoints_and_models_are_configuration(self):
        """A wrong endpoint must be correctable without editing code."""
        with patch.object(CFG, "ALPHA_GROK_BASE_URL", "https://example.test/v9"), \
             patch.object(CFG, "ALPHA_GROK_MODEL", "grok-next"):
            provider = GrokProvider()
            self.assertEqual(provider.model, "grok-next")
            os.environ["XAI_API_KEY"] = SECRET
            session = http_session(responses_api_response(
                valid_payload(self.snapshot())))
            provider.session = session
            provider.analyze(self.snapshot(), 5.0)
            self.assertTrue(session.posts[0]["url"].startswith(
                "https://example.test/v9"))


if __name__ == "__main__":
    import unittest
    unittest.main()
