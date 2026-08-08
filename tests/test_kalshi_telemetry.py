# -*- coding: utf-8 -*-
"""Review 005 — telemetrie des resultats d'appels Kalshi (OBSERVABILITE).

Ces tests prouvent deux choses distinctes :

1. La telemetrie DECRIT correctement chaque tentative : classe de resultat,
   numero de tentative, identifiant d'operation stable, latence, statut HTTP.
2. Elle ne CHANGE rien : meme nombre d'appels reseau, meme nombre de retries,
   memes delais, meme timeout, memes exceptions, memes valeurs de retour.

Le point 2 est le plus important. Chaque test de non-regression compare le
comportement observable a ce que faisait le code d'origine, et non a ce que
la telemetrie voudrait qu'il fasse.

Aucun test n'ouvre de socket : `session.request` est remplace par une
sequence deterministe, et `time.sleep` est capture (jamais execute), ce qui
permet d'affirmer que les DELAIS de backoff sont inchanges.
"""
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import requests  # noqa: E402

import kalshi_client as kc  # noqa: E402
from kalshi_client import (  # noqa: E402
    ENDPOINT_CATEGORIES,
    RESULT_CLASSES,
    TELEMETRY_FIELDS,
    KalshiAPIError,
    KalshiClient,
    classify_endpoint,
    kalshi_error_code,
    redact_telemetry,
    response_size_class,
)

#: Capture UNE SEULE FOIS, a l'import. Re-capturer dans setUp() sauvegardait
#: le stub deja pose des qu'un test se reinitialisait, et tearDown rendait
#: alors le stub permanent pour tout le processus — ce qui cassait deux tests
#: de timing sans rapport. Le comparatif de l'ensemble rouge l'a attrape.
_REAL_SLEEP = kc.time.sleep

SECRET_PEM = "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBg-SECRET-----END"
SECRET_SIG = "c2lnbmF0dXJlLXVsdHJhLXNlY3JldGU="
SECRET_KEY_ID = "kid-0123456789abcdef"


class Resp(object):
    """Reponse HTTP minimale, suffisante pour _req."""

    def __init__(self, status=200, text='{"ok": 1}', headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class Capture(logging.Handler):
    """Collecte les evenements de telemetrie emis sur le canal API."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.events = []
        self.records = []

    def emit(self, record):
        self.records.append(record)
        if getattr(record, "event", None) == "kalshi_api_call":
            self.events.append({
                k: v for k, v in record.__dict__.items()
                if k in TELEMETRY_FIELDS})


class TelemetryCase(unittest.TestCase):
    """Socle commun : client sans reseau, sleeps captures, logs collectes."""

    def setUp(self):
        self.reset()

    def reset(self):
        """Repart d'un client neuf sans retoucher au stub de sleep."""
        self.client = KalshiClient("demo")
        self.client._pk = object()          # franchit le garde /portfolio
        self.client.key_id = SECRET_KEY_ID
        self.client._sign_headers = lambda method, url: {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": SECRET_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": SECRET_SIG,
        }
        self.calls = []
        self.sleeps = []

        kc.time.sleep = lambda d: self.sleeps.append(d)

        self.logger = logging.getLogger("API")
        if getattr(self, "capture", None) is not None:
            self.logger.removeHandler(self.capture)
        else:
            self._old_level = self.logger.level
        self.capture = Capture()
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.capture)

    def tearDown(self):
        kc.time.sleep = _REAL_SLEEP
        self.logger.removeHandler(self.capture)
        self.logger.setLevel(self._old_level)

    def responses(self, *sequence):
        """Programme la sequence de reponses (Resp ou exception a lever)."""
        queue = list(sequence)

        def fake_request(method, url, **kw):
            self.calls.append((method, url, kw))
            item = queue.pop(0) if queue else Resp()
            if isinstance(item, BaseException):
                raise item
            return item

        self.client.session.request = fake_request

    @property
    def events(self):
        return self.capture.events


# ══════════════════════════════════════════════════════════════════════════
# 1. Classification : etiquettes bornees
# ══════════════════════════════════════════════════════════════════════════

class TestClassification(unittest.TestCase):

    def test_every_endpoint_maps_to_a_known_category(self):
        cases = [
            ("GET", "/markets", "get_markets", "markets_list", None),
            ("GET", "/markets/KXBTCD-1", "get_market", "market_detail",
             "KXBTCD-1"),
            ("GET", "/portfolio/balance", "get_balance", "balance", None),
            ("GET", "/portfolio/positions", "get_positions", "positions",
             None),
            ("GET", "/portfolio/fills", "get_fills", "fills", None),
            ("GET", "/portfolio/orders/ord_1", "get_order", "order_detail",
             "ord_1"),
            ("POST", "/portfolio/events/orders", "create_order",
             "orders_create", None),
            ("DELETE", "/portfolio/events/orders/ord_1", "cancel_order",
             "orders_cancel", "ord_1"),
        ]
        for method, path, operation, category, resource in cases:
            got = classify_endpoint(method, path)
            self.assertEqual(got, (operation, category, resource), path)
            self.assertIn(category, ENDPOINT_CATEGORIES)

    def test_query_string_does_not_change_the_category(self):
        self.assertEqual(
            classify_endpoint("GET", "/markets?series_ticker=KXBTCD&limit=50"),
            ("get_markets", "markets_list", None))

    def test_an_unknown_path_falls_back_to_a_bounded_label(self):
        operation, category, resource = classify_endpoint("GET", "/whatever/x")
        self.assertEqual((operation, category, resource),
                         ("unknown", "other", None))
        self.assertIn(category, ENDPOINT_CATEGORIES)

    def test_ticker_never_becomes_a_category(self):
        """La cardinalite des categories est FIXE, quels que soient les
        tickers rencontres : c'est la condition d'une metrique bornee."""
        seen = set()
        for i in range(500):
            _, category, resource = classify_endpoint(
                "GET", "/markets/KXBTCD-26AUG%04d-T%d" % (i, i))
            seen.add(category)
            self.assertIsNotNone(resource)      # l'id reste, en champ de log
        self.assertEqual(seen, {"market_detail"})

    def test_size_classes_are_bounded(self):
        self.assertEqual(response_size_class(0), "empty")
        self.assertEqual(response_size_class(500), "small")
        self.assertEqual(response_size_class(5000), "medium")
        self.assertEqual(response_size_class(500000), "large")
        self.assertEqual(response_size_class(None), "unknown")
        self.assertEqual(response_size_class(-1), "unknown")

    def test_error_code_extraction_is_bounded_and_safe(self):
        self.assertEqual(
            kalshi_error_code('{"error":{"code":"rate_limit_exceeded"}}'),
            "rate_limit_exceeded")
        self.assertIsNone(kalshi_error_code(""))
        self.assertIsNone(kalshi_error_code("not json at all"))
        # Un corps hostile ne peut pas injecter 4 Ko dans une etiquette.
        self.assertIsNone(kalshi_error_code('{"code":"%s"}' % ("x" * 200)))


# ══════════════════════════════════════════════════════════════════════════
# 2. Un evenement par tentative, par classe de resultat
# ══════════════════════════════════════════════════════════════════════════

class TestResultClasses(TelemetryCase):

    def test_successful_call_is_reported_as_success(self):
        self.responses(Resp(200, '{"markets": []}'))
        out = self.client._req("GET", "/markets")

        self.assertEqual(out, {"markets": []})
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["result_class"], "SUCCESS")
        self.assertEqual(event["http_status"], 200)
        self.assertEqual(event["operation"], "get_markets")
        self.assertEqual(event["endpoint_category"], "markets_list")
        self.assertEqual(event["http_method"], "GET")
        self.assertEqual(event["environment"], "demo")
        self.assertEqual(event["attempt_number"], 1)
        self.assertEqual(event["max_attempts"], 4)
        self.assertTrue(event["final_attempt"])
        self.assertFalse(event["retry_scheduled"])
        self.assertIsInstance(event["latency_ms"], float)
        self.assertEqual(event["response_size_class"], "small")

    def test_client_error_is_reported_as_http_error(self):
        self.responses(Resp(404, '{"error":{"code":"not_found"}}'))
        with self.assertRaises(KalshiAPIError) as ctx:
            self.client._req("GET", "/markets/KXBTCD-1")

        self.assertEqual(ctx.exception.status, 404)
        event = self.events[-1]
        self.assertEqual(event["result_class"], "HTTP_ERROR")
        self.assertEqual(event["http_status"], 404)
        self.assertEqual(event["kalshi_error_code"], "not_found")
        self.assertEqual(event["resource_id"], "KXBTCD-1")

    def test_authentication_failure_has_its_own_class(self):
        for status in (401, 403):
            self.reset()
            self.responses(Resp(status, '{"error":{"code":"unauthorized"}}'))
            with self.assertRaises(KalshiAPIError):
                self.client._req("GET", "/portfolio/balance")
            self.assertEqual(self.events[-1]["result_class"], "AUTH_ERROR",
                             "HTTP %d" % status)

    def test_rate_limiting_has_its_own_class(self):
        # 429 est retryable : la derniere tentative est aussi un 429.
        self.responses(*[Resp(429, '{"error":{"code":"rate_limit"}}')] * 4)
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets")

        classes = [e["result_class"] for e in self.events]
        self.assertEqual(classes, ["RATE_LIMIT"] * 4)
        self.assertEqual(self.events[-1]["kalshi_error_code"], "rate_limit")

    def test_timeout_is_distinguished_from_transport_failure(self):
        self.responses(requests.Timeout("read timeout"), Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(self.events[0]["result_class"], "TIMEOUT")
        self.assertEqual(self.events[0]["exception_type"], "Timeout")

        self.reset()
        self.responses(requests.ConnectionError("refused"), Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(self.events[0]["result_class"], "TRANSPORT_ERROR")
        self.assertEqual(self.events[0]["exception_type"], "ConnectionError")

    def test_malformed_json_is_reported_as_parse_error(self):
        self.responses(Resp(200, "<html>maintenance</html>"))
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets")

        event = self.events[-1]
        self.assertEqual(event["result_class"], "PARSE_ERROR")
        self.assertEqual(event["http_status"], 200)

    def test_hidden_exceptions_are_now_visible(self):
        """requests.TooManyRedirects n'etait ni retryee ni journalisee :
        elle traversait le client sans laisser de trace."""
        self.responses(requests.TooManyRedirects("loop"))
        with self.assertRaises(requests.TooManyRedirects):
            self.client._req("GET", "/markets")

        event = self.events[-1]
        self.assertEqual(event["result_class"], "UNEXPECTED_ERROR")
        self.assertEqual(event["exception_type"], "TooManyRedirects")
        self.assertTrue(event["final_attempt"])

    def test_every_emitted_class_belongs_to_the_closed_set(self):
        self.responses(Resp(500), Resp(429), requests.Timeout("t"), Resp(200))
        self.client._req("GET", "/markets")
        self.assertTrue(self.events)
        for event in self.events:
            self.assertIn(event["result_class"], RESULT_CLASSES)


# ══════════════════════════════════════════════════════════════════════════
# 3. Semantique des retries : operation vs tentative
# ══════════════════════════════════════════════════════════════════════════

class TestRetrySemantics(TelemetryCase):

    def test_a_retry_sequence_is_fully_described(self):
        self.responses(Resp(503), Resp(503), Resp(200, '{"ok":1}'))
        self.client._req("GET", "/markets")

        self.assertEqual([e["attempt_number"] for e in self.events], [1, 2, 3])
        self.assertEqual([e["result_class"] for e in self.events],
                         ["HTTP_ERROR", "HTTP_ERROR", "SUCCESS"])
        self.assertEqual([e["retry_scheduled"] for e in self.events],
                         [True, True, False])
        self.assertEqual([e["final_attempt"] for e in self.events],
                         [False, False, True])

    def test_one_operation_id_spans_every_attempt(self):
        self.responses(Resp(503), Resp(503), Resp(200))
        self.client._req("GET", "/markets")

        ids = {e["operation_id"] for e in self.events}
        self.assertEqual(len(ids), 1, "un retry doit rester UNE operation")

    def test_two_operations_have_different_ids(self):
        self.responses(Resp(200), Resp(200))
        self.client._req("GET", "/markets")
        self.client._req("GET", "/markets")
        self.assertEqual(len({e["operation_id"] for e in self.events}), 2)

    def test_exhausted_retries_are_marked_final(self):
        self.responses(*[requests.ConnectionError("down")] * 4)
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets")

        self.assertEqual(len(self.events), 4)
        self.assertEqual([e["attempt_number"] for e in self.events],
                         [1, 2, 3, 4])
        self.assertEqual([e["final_attempt"] for e in self.events],
                         [False, False, False, True])
        self.assertEqual([e["retry_scheduled"] for e in self.events],
                         [True, True, True, False])

    def test_operation_latency_accumulates_across_attempts(self):
        self.responses(Resp(503), Resp(200))
        self.client._req("GET", "/markets")

        first, last = self.events[0], self.events[-1]
        self.assertGreaterEqual(last["operation_latency_ms"],
                                first["operation_latency_ms"])
        self.assertGreaterEqual(last["operation_latency_ms"],
                                last["latency_ms"])

    def test_a_refused_authenticated_request_emits_nothing(self):
        """Aucune tentative reseau n'a lieu : la compter fausserait le taux
        d'echec de l'API."""
        self.client._pk = None
        self.responses(Resp(200))
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/portfolio/balance")

        self.assertEqual(self.events, [])
        self.assertEqual(self.calls, [])


# ══════════════════════════════════════════════════════════════════════════
# 4. NON-REGRESSION : le comportement observable est inchange
# ══════════════════════════════════════════════════════════════════════════

class TestNoBehaviourChange(TelemetryCase):

    def test_no_additional_api_call_is_introduced(self):
        self.responses(Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(len(self.calls), 1)

        self.reset()
        self.responses(Resp(503), Resp(503), Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(len(self.calls), 3)

    def test_retry_count_is_unchanged(self):
        self.responses(*[Resp(500)] * 4)
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets")
        self.assertEqual(len(self.calls), 4)          # 1 + 3 retries

        self.reset()
        self.responses(*[Resp(500)] * 3)
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets", retries=1)
        self.assertEqual(len(self.calls), 2)          # 1 + 1 retry

    def test_backoff_delays_are_unchanged(self):
        self.responses(*[Resp(500)] * 4)
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/markets")
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_retry_after_header_still_wins_on_429(self):
        self.responses(Resp(429, "{}", {"Retry-After": "7"}), Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(self.sleeps, [7.0])

    def test_timeout_value_is_unchanged(self):
        self.responses(Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(self.calls[0][2]["timeout"], 15)

    def test_request_parameters_are_untouched(self):
        self.responses(Resp(200))
        self.client._req("GET", "/markets",
                         params={"series_ticker": "KXBTCD", "limit": 50})
        method, url, kw = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/markets"))
        self.assertEqual(kw["params"], {"series_ticker": "KXBTCD",
                                        "limit": 50})
        self.assertNotIn("operation", kw)
        self.assertNotIn("event", kw)

    def test_exception_types_and_messages_are_unchanged(self):
        self.responses(*[requests.ConnectionError("boom")] * 4)
        with self.assertRaises(KalshiAPIError) as ctx:
            self.client._req("GET", "/markets")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("reseau:", str(ctx.exception))

        self.reset()
        self.responses(Resp(410, "deprecated_v1_order_endpoint"))
        with self.assertRaises(KalshiAPIError) as ctx:
            self.client._req("POST", "/portfolio/orders")
        self.assertEqual(ctx.exception.status, 410)
        self.assertIn("V1 OBSOLETE", str(ctx.exception))

    def test_unexpected_exceptions_still_propagate_unwrapped(self):
        self.responses(requests.TooManyRedirects("loop"))
        with self.assertRaises(requests.TooManyRedirects):
            self.client._req("GET", "/markets")
        self.assertEqual(len(self.calls), 1, "aucun retry ajoute")
        self.assertEqual(self.sleeps, [])

    def test_return_value_is_unchanged(self):
        self.responses(Resp(200, '{"markets": [{"ticker": "A"}]}'))
        self.assertEqual(self.client._req("GET", "/markets"),
                         {"markets": [{"ticker": "A"}]})

        self.reset()
        self.responses(Resp(200, "   "))
        self.assertEqual(self.client._req("GET", "/markets"), {})

    def test_last_http_status_is_still_recorded(self):
        self.responses(Resp(200))
        self.client._req("GET", "/markets")
        self.assertEqual(self.client.last_http_status, 200)

    def test_a_broken_logger_cannot_break_an_api_call(self):
        """La telemetrie est subordonnee a l'appel, jamais l'inverse."""
        def explode(*a, **kw):
            raise RuntimeError("logging backend down")
        original = kc.log_api.info
        kc.log_api.info = explode
        try:
            self.responses(Resp(200, '{"ok": 1}'))
            self.assertEqual(self.client._req("GET", "/markets"), {"ok": 1})
        finally:
            kc.log_api.info = original


# ══════════════════════════════════════════════════════════════════════════
# 5. Secrets
# ══════════════════════════════════════════════════════════════════════════

class TestSecretSafety(TelemetryCase):

    def test_no_credential_reaches_the_telemetry(self):
        self.responses(Resp(401, '{"error":{"code":"unauthorized"}}'),
                       Resp(200))
        with self.assertRaises(KalshiAPIError):
            self.client._req("GET", "/portfolio/balance")

        blob = json.dumps(self.events, default=str)
        for secret in (SECRET_PEM, SECRET_SIG, SECRET_KEY_ID):
            self.assertNotIn(secret, blob)
        for forbidden in ("KALSHI-ACCESS", "Authorization", "signature",
                          "private", "headers"):
            self.assertNotIn(forbidden.lower(), blob.lower())

    def test_no_credential_reaches_any_api_log_line(self):
        """Pas seulement la telemetrie : AUCUNE ligne du canal API."""
        self.responses(Resp(503), Resp(200))
        self.client._req("GET", "/markets")
        for record in self.capture.records:
            rendered = record.getMessage() + json.dumps(
                record.__dict__, default=str)
            self.assertNotIn(SECRET_SIG, rendered)
            self.assertNotIn(SECRET_PEM, rendered)

    def test_redaction_drops_everything_not_explicitly_approved(self):
        kept = redact_telemetry({
            "operation": "get_markets",
            "result_class": "SUCCESS",
            "api_key": "sk-live-1234",
            "Authorization": "Bearer abc",
            "KALSHI-ACCESS-SIGNATURE": SECRET_SIG,
            "headers": {"KALSHI-ACCESS-KEY": SECRET_KEY_ID},
            "payload": {"ticker": "X", "count": 1},
            "a_field_invented_next_year": "possibly a secret",
        })
        self.assertEqual(kept, {"operation": "get_markets",
                                "result_class": "SUCCESS"})

    def test_the_whitelist_contains_no_credential_shaped_name(self):
        for field in TELEMETRY_FIELDS:
            lowered = field.lower()
            for banned in ("key", "secret", "token", "signature", "password",
                           "credential", "auth", "header", "payload", "body"):
                self.assertNotIn(banned, lowered, field)


# ══════════════════════════════════════════════════════════════════════════
# 6. Cardinalite
# ══════════════════════════════════════════════════════════════════════════

class TestCardinality(TelemetryCase):

    def test_metric_labels_stay_bounded_across_many_distinct_resources(self):
        categories, classes, resources = set(), set(), set()
        for i in range(200):
            self.reset()
            self.responses(Resp(200))
            self.client._req("GET", "/markets/KXBTCD-26AUG%04d-T%d" % (i, i))
            event = self.events[-1]
            categories.add(event["endpoint_category"])
            classes.add(event["result_class"])
            resources.add(event["resource_id"])

        self.assertEqual(categories, {"market_detail"})
        self.assertEqual(classes, {"SUCCESS"})
        self.assertEqual(len(resources), 200)     # log seulement, pas metrique
        self.assertLessEqual(len(ENDPOINT_CATEGORIES) * len(RESULT_CLASSES), 72)

    def test_no_raw_url_is_logged(self):
        self.responses(Resp(200))
        self.client._req("GET", "/markets",
                         params={"series_ticker": "KXBTCD", "limit": 50})
        blob = json.dumps(self.events, default=str)
        self.assertNotIn("://", blob)                    # aucun URL absolu
        self.assertNotIn(self.client.base_url, blob)
        self.assertNotIn("series_ticker", blob)          # aucun parametre
        self.assertNotIn("limit", blob)


if __name__ == "__main__":
    unittest.main()
