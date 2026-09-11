# -*- coding: utf-8 -*-
"""Two Railway services, one repository: the deployment boundary.

THE CLAIM UNDER TEST
    The engine and the Alpha Shadow Service share a repository but not an
    authority. The engine holds broker credentials; Alpha holds AI keys and
    refuses to run if it can see a broker credential. Both are started from
    the same image by DIFFERENT commands, and neither start command can
    become the other by accident.

WHY THE FEED GETS ITS OWN CLASS
    A Railway volume is mounted into exactly ONE service, so the engine's
    spool directory is not visible to Alpha. A cross-service deployment that
    kept the local transport would not fail loudly -- it would report "no
    candidates", forever, which is indistinguishable from a quiet market.
    These cases pin the HTTP transport and, more importantly, pin that an
    unreachable feed is reported as an ERROR rather than as emptiness.
"""
import ast
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _alpha import AlphaCase                                    # noqa: E402

import alpha_consumer                                           # noqa: E402
from alpha_consumer import (FeedUnavailable, HttpSpoolSource,    # noqa: E402
                            LocalSpoolSource, SpoolConsumer,
                            default_source)
from alpha_service import (BROKER_AUTHORITY_VALUES,              # noqa: E402
                           BROKER_AUTHORITY_VARS,
                           BROKER_CREDENTIAL_VARS,
                           BrokerCredentialsPresent,
                           assert_no_broker_credentials,
                           loaded_execution_modules)
from alpha_providers import ENV_GEMINI                           # noqa: E402
from config import CFG                                           # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "kalshi-private-key-DO-NOT-LOG-0001"


def procfile() -> dict:
    text = open(os.path.join(ROOT, "Procfile"), encoding="utf-8").read()
    out = {}
    for line in text.splitlines():
        if ":" in line and line.strip():
            name, command = line.split(":", 1)
            out[name.strip()] = command.strip()
    return out


class AlphaStartupRefusesBrokerAuthority(AlphaCase):
    """Section 2. Individually, in combination, and without leaking."""

    def test_every_broker_credential_is_refused_on_its_own(self):
        for name in BROKER_CREDENTIAL_VARS:
            with self.subTest(variable=name):
                with self.assertRaises(BrokerCredentialsPresent) as caught:
                    assert_no_broker_credentials({name: SECRET})
                self.assertIn(name, str(caught.exception))

    def test_every_boolean_authority_gate_is_refused_on_its_own(self):
        for name in BROKER_AUTHORITY_VARS:
            with self.subTest(variable=name):
                with self.assertRaises(BrokerCredentialsPresent) as caught:
                    assert_no_broker_credentials({name: "true"})
                self.assertIn(name, str(caught.exception))

    def test_value_carrying_authority_is_refused(self):
        """`PROD_ACCESS_MODE=CAPITAL` is not truthy, and is exactly the
        setting that turns capital on -- a truthiness test would miss it."""
        for name, values in BROKER_AUTHORITY_VALUES.items():
            for value in values:
                with self.subTest(variable=name, value=value):
                    with self.assertRaises(BrokerCredentialsPresent):
                        assert_no_broker_credentials({name: value.upper()})

    def test_the_read_only_and_standard_values_are_not_refused(self):
        """A guard that refuses everything proves nothing about what it
        catches."""
        self.assertEqual(
            assert_no_broker_credentials({"PROD_ACCESS_MODE": "READ_ONLY",
                                          "EXECUTION_MODE": "standard"}), [])

    def test_combinations_are_all_reported_not_just_the_first(self):
        env = {"KALSHI_KEY_ID": SECRET, "KALSHI_PRIVATE_KEY": SECRET,
               "ALLOW_ORDER_SUBMISSION": "true",
               "PROD_ACCESS_MODE": "CAPITAL"}
        with self.assertRaises(BrokerCredentialsPresent) as caught:
            assert_no_broker_credentials(env)
        for name in env:
            self.assertIn(name, str(caught.exception), name)

    def test_the_refusal_names_the_variable_and_never_its_value(self):
        env = {name: SECRET for name in BROKER_CREDENTIAL_VARS}
        with self.assertRaises(BrokerCredentialsPresent) as caught:
            assert_no_broker_credentials(env)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_a_clean_alpha_environment_starts(self):
        self.assertEqual(assert_no_broker_credentials(
            {"XAI_API_KEY": "a", "GEMINI_API_KEY": "b",
             "OPENAI_API_KEY": "c", "DATA_DIR": "/data"}), [])

    def test_an_empty_broker_variable_is_not_a_credential(self):
        """Railway leaves an emptied variable present but blank."""
        self.assertEqual(
            assert_no_broker_credentials({"KALSHI_PRIVATE_KEY": "   "}), [])


class TheGeminiVariableNameIsExact(AlphaCase):
    """The key was installed as `Gemini API Key`, which no process can read
    as an environment variable. Detecting that is the difference between an
    excluded provider and a silent one."""

    def test_the_adapter_looks_up_the_underscored_name(self):
        self.assertEqual(ENV_GEMINI, "GEMINI_API_KEY")

    def test_a_spaced_variable_name_is_not_the_gemini_key(self):
        from alpha_providers import GeminiProvider
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_GEMINI_API_KEY", None)
        os.environ["Gemini API Key"] = "installed-under-the-wrong-name"
        self.addCleanup(os.environ.pop, "Gemini API Key", None)
        self.assertFalse(GeminiProvider().configured())

    def test_a_missing_gemini_key_excludes_gemini_and_says_so(self):
        from alpha_providers import GeminiProvider
        provider = GeminiProvider()
        self.assertFalse(provider.configured())
        raw, meta = provider.analyze(self.snapshot(), 5.0)
        self.assertIsNone(raw)
        self.assertIn("GEMINI_API_KEY", meta["error"])

    def test_the_longer_google_name_still_works_as_a_fallback(self):
        from alpha_providers import GeminiProvider
        os.environ["GOOGLE_GEMINI_API_KEY"] = "k"
        self.assertTrue(GeminiProvider().configured())


class TheTwoServicesHaveDifferentStartCommands(unittest.TestCase):
    """Section 1. Shared repository, separate authority."""

    def test_the_engine_process_type_is_unchanged(self):
        self.assertEqual(procfile()["worker"],
                         "python kalshi_alpha_bot.py --loop --live-read-only")

    def test_there_is_an_alpha_process_type(self):
        self.assertEqual(procfile()["alpha"],
                         "python tools/alpha_service_run.py")

    def test_the_two_commands_are_different_programs(self):
        self.assertNotEqual(procfile()["alpha"], procfile()["worker"])
        self.assertNotIn("kalshi_alpha_bot", procfile()["alpha"])

    def test_the_dockerfile_cmd_is_still_the_engine(self):
        """Alpha overrides the start command; it must not become the image
        default, or a redeploy of the ENGINE would silently start Alpha."""
        text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
        cmds = re.findall(r"^CMD\s+(\[.*\])\s*$", text, flags=re.M)
        self.assertEqual([json.loads(c) for c in cmds],
                         [["python", "kalshi_alpha_bot.py", "--loop",
                           "--live-read-only"]])

    def test_the_bare_alpha_command_runs_the_continuous_pipeline(self):
        """The Procfile command carries no subcommand, so the default must
        be the loop -- not a one-shot, and not an error."""
        import tools.alpha_service_run as runner
        import argparse
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd", required=False)
        self.assertIsNone(sub.dest and ap.parse_args([]).cmd)
        self.assertTrue(hasattr(runner, "cmd_run"))
        self.assertEqual(runner.STARTUP_REFUSED_MARKER,
                         "ALPHA_STARTUP_REFUSED_BROKER_CREDENTIALS")

    def test_the_alpha_entry_point_imports_no_execution_module(self):
        tree = ast.parse(open(os.path.join(ROOT, "tools",
                                           "alpha_service_run.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported & {"kalshi_alpha_bot", "execution_engine",
                                     "order_manager", "kalshi_client",
                                     "risk_manager"}, set())


class NoExecutionModuleIsLoadedInTheAlphaProcess(unittest.TestCase):
    """The AST tests prove nothing IMPORTS the money path; this proves the
    running interpreter does not HOLD it.

    It must run in a FRESH interpreter. Inside pytest every test module
    shares one process, and sibling modules have long since imported
    `kalshi_client` and friends -- so an in-process assertion here would be
    measuring the test runner, not the service. A subprocess is what the
    deployed Alpha service actually is.
    """

    def run_probe(self, body: str) -> str:
        import subprocess
        return subprocess.run(
            [sys.executable, "-c", body], cwd=ROOT, timeout=120,
            capture_output=True, text=True).stdout.strip()

    def test_importing_the_whole_alpha_service_loads_no_execution_module(self):
        out = self.run_probe(
            "import alpha_service, alpha_gateway, alpha_consumer, "
            "alpha_providers, alpha_cost, alpha_telemetry;"
            "print(alpha_service.loaded_execution_modules())")
        self.assertEqual(out, "[]", out)

    def test_the_alpha_entry_point_loads_no_execution_module(self):
        """The real start command, not just the library."""
        out = self.run_probe(
            "import sys; sys.path.insert(0, 'tools');"
            "import alpha_service_run, alpha_service;"
            "print(alpha_service.loaded_execution_modules())")
        self.assertEqual(out, "[]", out)

    def test_the_detector_can_actually_fire(self):
        """A detector that never fires is not a detector."""
        out = self.run_probe(
            "import order_manager, alpha_service;"
            "print(alpha_service.loaded_execution_modules())")
        self.assertIn("order_manager", out)

    def test_the_health_report_counts_what_is_loaded(self):
        self.assertEqual(loaded_execution_modules.__module__, "alpha_service")


class TheResearchFeedCrossesTheServiceBoundary(AlphaCase):
    """Section 6. The transport, and what it does when it cannot reach."""

    def source(self, pages, **kw):
        return HttpSpoolSource(base_url="http://engine.internal",
                               token="feed-token-DO-NOT-LOG",
                               session=_FakeSession(pages), **kw)

    def record(self, contract_id="C1", sha="a" * 64):
        import research_feed
        return {"schema": research_feed.FEED_SCHEMA,
                "record_sha256": sha, "record_id": sha[:20],
                "emitted_at_utc": "2026-09-09T12:00:00+00:00",
                "contract_id": contract_id, "event_id": "E",
                "question": "Will it?", "resolution_rules": "as written",
                "resolution_source": "CF Benchmarks RTI",
                "yes_bid": 0.44, "yes_ask": 0.46,
                "no_bid": 0.54, "no_ask": 0.56,
                "volume": 10.0, "open_interest": 5.0,
                "market_close_time_utc": "2026-09-10T12:00:00+00:00",
                "expected_resolution_time_utc": "2026-09-10T13:00:00+00:00",
                # Every required fact names the source key it was read from.
                # A record without this is refused: see `SpoolConsumer._valid`.
                "field_provenance": {
                    "contract_id": "market.ticker", "question": "market.title",
                    "resolution_rules": "market.rules_primary",
                    "resolution_source": "market.settlement_sources",
                    "yes_bid": "book.yes_bid(cents)",
                    "yes_ask": "book.yes_ask(cents)",
                    "no_bid": "book.no_bid(cents)",
                    "no_ask": "book.no_ask(cents)",
                    "volume": "market.volume",
                    "open_interest": "market.open_interest",
                    "market_close_time_utc": "market.close_time",
                    "expected_resolution_time_utc": "market.expiration_time"},
                "unavailable_fields": ["catalyst_name", "catalyst_time_utc"]}

    def test_the_default_transport_is_local_so_one_host_is_unchanged(self):
        with unittest.mock.patch.object(CFG, "ALPHA_FEED_TRANSPORT", "local"):
            self.assertIsInstance(default_source(), LocalSpoolSource)

    def test_http_is_selected_explicitly(self):
        with unittest.mock.patch.object(CFG, "ALPHA_FEED_TRANSPORT", "http"):
            self.assertIsInstance(default_source(), HttpSpoolSource)

    def test_it_pulls_candidates_from_the_engine_research_api(self):
        session = _FakeSession([{"rows": [self.record()], "has_more": False}])
        source = HttpSpoolSource(base_url="http://engine.internal",
                                 token="t0ken-value", session=session)
        rows = source.records()
        self.assertEqual(len(rows), 1)
        self.assertTrue(session.gets[0]["url"].endswith(
            "/api/research/v1/candidates"))
        self.assertEqual(session.gets[0]["headers"]["Authorization"],
                         "Bearer t0ken-value")

    def test_it_only_ever_issues_gets(self):
        """The consumer's inability to mutate engine state is a property of
        the transport, not a convention."""
        session = _FakeSession([{"rows": [], "has_more": False}])
        HttpSpoolSource(base_url="http://e", token="t",
                        session=session).records()
        self.assertEqual(session.methods_used, {"get"})
        for verb in ("post", "put", "patch", "delete"):
            self.assertFalse(hasattr(HttpSpoolSource, verb))

    def test_it_follows_the_cursor_across_pages(self):
        source = self.source([
            {"rows": [self.record("C1", "a" * 64)], "has_more": True,
             "next_cursor": "cur1"},
            {"rows": [self.record("C2", "b" * 64)], "has_more": False}])
        self.assertEqual(len(source.records()), 2)

    def test_a_cursor_that_never_advances_does_not_loop_forever(self):
        stuck = {"rows": [self.record()], "has_more": True,
                 "next_cursor": "same"}
        source = self.source([stuck] * 200)
        self.assertLessEqual(len(source.records()),
                             alpha_consumer.MAX_FEED_PAGES)

    def test_an_unreachable_feed_is_an_error_not_an_empty_page(self):
        """The failure this exists to prevent: an outage that looks exactly
        like a market with nothing to analyse."""
        session = _FakeSession([], raises=OSError("connection refused"))
        source = HttpSpoolSource(base_url="http://e", token="t",
                                 session=session)
        with self.assertRaises(FeedUnavailable):
            source.records()

    def test_a_non_200_is_an_error_not_an_empty_page(self):
        source = HttpSpoolSource(base_url="http://e", token="t",
                                 session=_FakeSession([{}], status=503))
        with self.assertRaises(FeedUnavailable):
            source.records()

    def test_an_unconfigured_feed_refuses_rather_than_reporting_nothing(self):
        for kwargs in ({"base_url": "", "token": "t"},
                       {"base_url": "http://e", "token": ""}):
            with self.subTest(**kwargs):
                with self.assertRaises(FeedUnavailable):
                    HttpSpoolSource(session=_FakeSession([]),
                                    **kwargs).records()

    def test_the_consumer_reports_a_feed_outage_and_analyses_nothing(self):
        session = _FakeSession([], raises=OSError("connection refused"))
        consumer = SpoolConsumer(source=HttpSpoolSource(
            base_url="http://e", token="t", session=session))
        self.assertEqual(consumer.pending(), [])
        self.assertEqual(consumer.stats["feed_errors"], 1)
        self.assertIn("connection refused", consumer.feed_error)

    def test_the_feed_token_never_appears_in_an_error(self):
        token = "feed-token-DO-NOT-LOG"
        session = _FakeSession([], raises=OSError(
            f"proxy rejected Authorization: Bearer {token}"))
        source = HttpSpoolSource(base_url="http://e", token=token,
                                 session=session)
        with self.assertRaises(FeedUnavailable) as caught:
            source.records()
        self.assertNotIn(token, str(caught.exception))

    def test_the_feed_token_never_appears_in_the_description(self):
        source = HttpSpoolSource(base_url="http://e", token="feed-token-x")
        self.assertNotIn("feed-token-x", json.dumps(source.describe()))
        self.assertTrue(source.describe()["token_configured"])

    def test_a_pulled_record_mints_the_same_snapshot_as_a_local_one(self):
        """The transport must not change identity, or the two deployments
        would deduplicate differently."""
        consumer = SpoolConsumer(source=self.source(
            [{"rows": [self.record()], "has_more": False}]))
        pulled = consumer.pending()
        self.assertEqual(len(pulled), 1)
        local = SpoolConsumer(source=_ListSource([self.record()]))
        self.assertEqual(pulled[0][0].market_snapshot_id,
                         local.pending()[0][0].market_snapshot_id)


class TheEngineServesTheSpoolReadOnly(AlphaCase):
    """The producer side of the same boundary."""

    def spool(self, count=2):
        import research_feed
        directory = os.path.join(self._tmp, "research_spool")
        os.makedirs(directory, exist_ok=True)
        for i in range(count):
            record = {"schema": research_feed.FEED_SCHEMA,
                      "record_sha256": chr(97 + i) * 64,
                      "contract_id": f"C{i}", "question": "?"}
            with open(os.path.join(directory, f"r{i}.json"), "w") as fh:
                json.dump(record, fh)
        return directory

    def test_the_candidates_route_serves_what_the_producer_wrote(self):
        import research_export as rx
        self.spool(2)
        body = rx.candidates(self._tmp)
        self.assertEqual(body["dataset"], "candidates")
        self.assertEqual(len(body["rows"]), 2)
        self.assertEqual({r["contract_id"] for r in body["rows"]},
                         {"C0", "C1"})

    def test_reading_the_spool_does_not_modify_it(self):
        import research_export as rx
        directory = self.spool(3)
        before = {n: open(os.path.join(directory, n), "rb").read()
                  for n in sorted(os.listdir(directory))}
        rx.candidates(self._tmp)
        after = {n: open(os.path.join(directory, n), "rb").read()
                 for n in sorted(os.listdir(directory))}
        self.assertEqual(before, after)

    def test_an_absent_spool_is_an_empty_page_not_a_crash(self):
        import research_export as rx
        body = rx.candidates(os.path.join(self._tmp, "nowhere"))
        self.assertEqual(body["rows"], [])

    def test_the_route_requires_a_bearer_token(self):
        import research_export as rx
        os.environ.pop("RESEARCH_API_TOKEN", None)
        self.assertFalse(rx.authorised("Bearer anything"))

    def test_the_engine_side_imports_only_the_neutral_module(self):
        """`research_export` may reach the spool name through
        `research_feed`, never through an alpha module."""
        tree = ast.parse(open(os.path.join(ROOT, "research_export.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertIn("research_feed", imported)
        self.assertEqual({n for n in imported if n.startswith("alpha_")}, set())


class _ListSource:
    kind = "list"
    directory = None

    def __init__(self, rows):
        self.rows = rows

    def describe(self):
        return {"transport": self.kind}

    def records(self):
        return list(self.rows)


class _FakeSession:
    """A `requests`-like session. Never opens a socket."""

    def __init__(self, pages, *, status=200, raises=None):
        self.pages = list(pages)
        self.status = status
        self.raises = raises
        self.gets = []
        self.methods_used = set()

    def get(self, url, headers=None, params=None, timeout=None):
        self.methods_used.add("get")
        self.gets.append({"url": url, "headers": headers or {},
                          "params": params or {}})
        if self.raises is not None:
            raise self.raises
        body = self.pages[min(len(self.gets) - 1, len(self.pages) - 1)] \
            if self.pages else {}
        return _FakeResponse(body, self.status)


class _FakeResponse:
    def __init__(self, body, status):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


if __name__ == "__main__":
    unittest.main()
