"""HIGH regression: malformed candle evidence must never invoke the model.

Offline, synthetic fixtures only. Real adapters are tested from raw HTTP-shaped
responses; separate strategy spies prove invalid context cannot evaluate p_yes.
"""
import os
import json
import unittest
from unittest.mock import Mock, patch

import btc_context as bc
from strategy_router import BtcModelStrategy, BtcDailyStrategy
from tests.candle_fixtures import candles

NOW = 1800748800.0
META = {"http_status": 200, "elapsed_ms": 1.0, "error": None}


def spots(now=NOW):
    return tuple((lambda name=name: {"source": name, "price": 65000., "ts": now})
                 for name in ("coinbase", "kraken", "bitstamp"))


def context(rows, now=NOW, use_cache=False, strike=65000):
    return bc.get_btc_context(strike=strike, minutes_remaining=10,
                              spot_sources=spots(now), klines_fn=lambda: rows,
                              now=now, use_cache=use_cache)


def raw_rows(provider, now=NOW, include_open=True):
    rows = candles(n=31 if include_open else 30, now=now + (60 if include_open else 0),
                   provider=provider)
    out = []
    for r in rows:
        if provider == "binance":
            out.append([int(r["ts"]*1000), str(r["open"]), str(r["high"]),
                        str(r["low"]), str(r["close"]), str(r["volume"]),
                        int(r["close_ts"]*1000)-1, "1000", 10, "1", "10", "0"])
        elif provider == "kraken":
            out.append([int(r["ts"]), str(r["open"]), str(r["high"]),
                        str(r["low"]), str(r["close"]), str(r["close"]),
                        str(r["volume"]), 10])
        else:
            out.append([int(r["ts"]), r["low"], r["high"], r["open"],
                        r["close"], r["volume"]])
    if provider == "coinbase":
        out.reverse()
    return {"error": [], "result": {"XXBTZUSD": out, "last": int(now)}} \
        if provider == "kraken" else out


class TestModelCandleBoundary(unittest.TestCase):
    def setUp(self):
        bc.clear_cache()
        self.env = patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        bc.clear_cache()

    def assertBlocked(self, rows, now=NOW):
        ctx = context(rows, now)
        self.assertFalse(ctx.valid, ctx.to_dict())
        self.assertIsNone(ctx.realized_vol_1m)
        self.assertEqual(ctx.returns, {})
        for strategy in (BtcModelStrategy, BtcDailyStrategy):
            with patch("btc_probability_model.probability_yes") as model:
                result = strategy(lambda **kw: ctx).evaluate(
                    {"floor_strike": 65000}, {}, 10)
            model.assert_not_called()
            self.assertFalse(result.valid)
        return ctx

    def test_valid_control_invokes_model(self):
        ctx = context(candles(now=NOW))
        self.assertTrue(ctx.valid, ctx.reason)
        for strategy in (BtcModelStrategy, BtcDailyStrategy):
            with patch("btc_probability_model.probability_yes", return_value=.5) as model:
                out = strategy(lambda **kw: ctx).evaluate({"floor_strike": 65000}, {}, 10)
            model.assert_called_once()
            self.assertTrue(out.valid)

    def test_future_sequence_is_blocked(self):
        self.assertBlocked(candles(now=NOW+86400))

    def test_hourly_sequence_is_blocked(self):
        rows = candles(now=NOW)
        for i, row in enumerate(rows):
            row["ts"] = NOW - 60 - (29-i)*3600
            row["close_ts"] = row["ts"] + 60
        self.assertBlocked(rows)

    def test_seventeen_second_cadence_is_blocked(self):
        rows = candles(now=NOW)
        for i, row in enumerate(rows):
            row["ts"] = NOW - 60 - (29-i)*17
            row["close_ts"] = row["ts"] + 60
        self.assertBlocked(rows)

    def test_shifted_minute_grid_is_blocked(self):
        rows = candles(now=NOW)
        for row in rows:
            row["ts"] -= .5
            row["close_ts"] -= .5
        self.assertBlocked(rows)

    def test_stale_sequence_is_blocked(self):
        self.assertBlocked(candles(now=NOW-180))

    def test_current_open_tail_is_blocked_at_context_boundary(self):
        rows = candles(now=NOW+60)
        for row in rows:
            row["provenance"]["observed_ts"] = NOW
        self.assertBlocked(rows)

    def test_explicit_nonclosed_candle_is_blocked(self):
        rows = candles(now=NOW)
        rows[-1]["closed"] = False
        self.assertBlocked(rows)

    def test_missing_interval_duplicate_and_reversed_are_blocked(self):
        rows = candles(now=NOW)
        for bad in (rows[:10]+rows[11:], rows[:10]+[rows[9]]+rows[11:], list(reversed(rows))):
            with self.subTest(shape=[r["ts"] for r in bad]):
                self.assertBlocked(bad)

    def test_malformed_row_is_not_silently_filtered(self):
        for bad in (None, [], "row", {}, {"ts": NOW-60, "close": 65000}):
            rows = candles(now=NOW)
            rows[10] = bad
            with self.subTest(bad=bad):
                self.assertBlocked(rows)

    def test_missing_fields_and_nonfinite_numbers_are_blocked(self):
        for field in ("ts", "close_ts", "open", "high", "low", "close", "volume"):
            rows = candles(now=NOW)
            del rows[10][field]
            self.assertBlocked(rows)
            for bad in (None, True, False, "65000", float("nan"), float("inf"), -float("inf"), 10**500):
                rows = candles(now=NOW)
                rows[10][field] = bad
                with self.subTest(field=field, bad=bad):
                    self.assertBlocked(rows)

    def test_invalid_ohlcv_and_close_time_are_blocked(self):
        for field, bad in (("low", 70000), ("high", 60000), ("open", 0),
                           ("close", -1), ("volume", -1), ("close_ts", NOW)):
            rows = candles(now=NOW)
            rows[10][field] = bad
            with self.subTest(field=field):
                self.assertBlocked(rows)

    def test_missing_incomplete_and_unknown_payload_are_blocked(self):
        for rows in (None, [], {}, {"candles": candles(now=NOW)}, candles(10, NOW)):
            self.assertBlocked(rows)

    def test_provenance_missing_mixed_or_wrong_binding_is_blocked(self):
        for field, bad in (("provider", "other"), ("provider", []), ("product", "ETHUSD"),
                           ("endpoint", "http://example.test"), ("interval_s", 3600),
                           ("interval_s", 60.0), ("timestamp_semantics", "close_ms"),
                           ("observed_ts", NOW+1), ("observed_ts", NOW-2000),
                           ("observed_ts", float("nan"))):
            for all_rows in (False, True):
                rows = candles(now=NOW)
                for row in rows if all_rows else rows[10:11]:
                    row["provenance"][field] = bad
                with self.subTest(field=field, bad=bad, all_rows=all_rows):
                    self.assertBlocked(rows)
        rows = candles(now=NOW)
        del rows[10]["provenance"]
        self.assertBlocked(rows)
        rows = candles(now=NOW)
        rows[10] = candles(now=NOW, provider="coinbase")[10]
        self.assertBlocked(rows)

    def test_errors_in_injected_transport_are_blocked(self):
        for meta in ({}, None, {"http_status": 206}, {"http_status": 200, "error": "late_error"},
                     {**META, "partial": True}, {**META, "errors": ["late shard failure"]}):
            self.assertBlocked((candles(now=NOW), meta))

    def test_legacy_stale_source_label_cannot_grant_evaluation(self):
        with patch.object(bc, "fetch_klines_with_fallback", return_value=(candles(now=NOW), "stale_cache:kraken(1s)")):
            ctx = bc.get_btc_context(spot_sources=spots(), now=NOW, use_cache=False)
        self.assertFalse(ctx.valid)
        self.assertIn("stale_cache_forbidden", ctx.reason)

    def test_slow_fetch_recomputes_spot_consensus_after_expiry(self):
        initial = NOW + 20
        after = NOW + 22
        raw = [{"source": "old", "price": 65200., "ts": initial-89},
               {"source": "new1", "price": 65000., "ts": initial},
               {"source": "new2", "price": 65010., "ts": initial}]
        source_functions = tuple((lambda r=r: r) for r in raw)
        def slow_candles():
            clock.return_value = after
            return candles(now=initial)
        with patch.object(bc.time, "time", return_value=initial) as clock:
            ctx = bc.get_btc_context(spot_sources=source_functions,
                                     klines_fn=slow_candles, use_cache=False)
        self.assertTrue(ctx.valid, ctx.reason)
        self.assertEqual(ctx.n_valid_sources, 2)
        self.assertEqual(ctx.spot, 65005.)
        self.assertEqual([s["source"] for s in ctx.sources], ["new1", "new2"])

    def test_cycle_context_cannot_outlive_candles(self):
        # Fresh spot timestamps alone do not refresh cached candle history.
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            old = candles(now=NOW-60)
            c1 = context(old, use_cache=True)
            self.assertTrue(c1.valid, c1.reason)
            # Seed refreshed spot cache while retaining old context and candles.
            bc._cache["spot_sources"] = ([f() for f in spots(NOW+61)], NOW+61)
            c2 = context(old, now=NOW+61, use_cache=True)
        self.assertFalse(c2.valid)
        self.assertIsNot(c1, c2)

    def test_cycle_context_cannot_outlive_spots_or_survive_clock_reversal(self):
        for delta in (91, -1):
            bc.clear_cache()
            with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
                c1 = context(candles(now=NOW), use_cache=True)
                c2 = context(candles(now=NOW), now=NOW+delta, use_cache=True)
            self.assertTrue(c1.valid)
            self.assertFalse(c2.valid)
            self.assertIsNot(c1, c2)

    def test_long_raw_cache_revalidates_for_different_strike(self):
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            c1 = context(candles(now=NOW), use_cache=True)
            bc._cache["spot_sources"] = ([f() for f in spots(NOW+181)], NOW+181)
            c2 = context(candles(now=NOW), now=NOW+181, use_cache=True, strike=66000)
        self.assertTrue(c1.valid)
        self.assertFalse(c2.valid)


class TestProviderBoundary(unittest.TestCase):
    def setUp(self):
        bc.clear_cache()
        bc._last_good_klines.update(kl=None, ts=0, provider=None)

    def fetch(self, provider, payload=None, meta=None, clock=NOW):
        fn = getattr(bc, "fetch_klines_"+provider)
        payload = raw_rows(provider) if payload is None else payload
        with patch.object(bc, "_http_get_json_meta", return_value=(payload, META if meta is None else meta)), patch.object(bc.time, "time", return_value=clock):
            return fn()

    def test_transport_rejects_redirect_partial_and_mismatched_origin(self):
        url = bc.KLINE_ORIGINS["kraken"][1]
        for status, response_url in ((302, url), (206, url),
                                     (200, "https://example.test/public/OHLC")):
            response = Mock(status_code=status, url=response_url, headers={})
            response.json.return_value = raw_rows("kraken")
            with patch("requests.get", return_value=response) as http:
                data, meta = bc._http_get_json_meta(url)
            self.assertIsNone(data)
            self.assertIsNotNone(meta["error"])
            self.assertFalse(http.call_args.kwargs["allow_redirects"])
        response = Mock(status_code=200, url=url+"?pair=XBTUSD&interval=1", headers={})
        response.json.return_value = raw_rows("kraken")
        with patch("requests.get", return_value=response):
            data, meta = bc._http_get_json_meta(url)
        self.assertIsNotNone(data)
        self.assertIsNone(meta["error"])

    def test_http_200_content_range_never_proves_complete_response(self):
        from requests import Response
        response = Response()
        response.status_code = 200
        response.url = bc.KLINE_ORIGINS["kraken"][1]
        response._content = json.dumps(raw_rows("kraken")).encode("utf-8")
        response.encoding = "utf-8"
        response.headers["Content-Range"] = "bytes 0-100/200"
        with patch("requests.get", return_value=response), patch.object(bc.time, "time", return_value=NOW):
            rows, meta = bc.fetch_klines_kraken()
        self.assertIsNone(rows)
        self.assertIn("partial_http_response", meta["error"])

    def test_duplicate_json_members_cannot_erase_provider_failure(self):
        from requests import Response
        result = json.dumps(raw_rows("kraken")["result"])
        good_rows = json.dumps(raw_rows("kraken")["result"]["XXBTZUSD"])
        payloads = [
            '{"error":["EGeneral:failure"],"error":[],"result":'+result+'}',
            '{"error":[],"result":{"partial":true},"result":'+result+'}',
            '{"error":[],"result":{"XXBTZUSD":[],"XXBTZUSD":'+good_rows+
            ',"last":'+str(int(NOW))+'}}',
        ]
        for payload in payloads:
            response = Response()
            response.status_code = 200
            response.url = bc.KLINE_ORIGINS["kraken"][1]
            response._content = payload.encode("utf-8")
            response.encoding = "utf-8"
            with patch("requests.get", return_value=response), patch.object(bc.time, "time", return_value=NOW):
                rows, meta = bc.fetch_klines_kraken()
            self.assertIsNone(rows)
            self.assertIn("duplicate_json_member", meta["error"])
            ctx = context((rows, meta))
            self.assertFalse(ctx.valid)
            for strategy in (BtcModelStrategy, BtcDailyStrategy):
                with patch("btc_probability_model.probability_yes") as model:
                    strategy(lambda **kw: ctx).evaluate({"floor_strike": 65000}, {}, 10)
                model.assert_not_called()
        # Positive raw-response control proves strict decoding remains usable.
        response._content = json.dumps(raw_rows("kraken")).encode("utf-8")
        with patch("requests.get", return_value=response), patch.object(bc.time, "time", return_value=NOW):
            rows, meta = bc.fetch_klines_kraken()
        self.assertEqual(len(rows), 30, meta)
        self.assertTrue(context(rows).valid)

    def test_transport_rejects_nonstandard_json_constants_recursively(self):
        from requests import Response
        for constant in ("NaN", "Infinity", "-Infinity"):
            response = Response()
            response.status_code = 200
            response.url = bc.KLINE_ORIGINS["kraken"][1]
            response._content = ('{"nested":{"unused":'+constant+'}}').encode("utf-8")
            response.encoding = "utf-8"
            with patch("requests.get", return_value=response):
                payload, meta = bc._http_get_json_meta(response.url)
            self.assertIsNone(payload)
            self.assertIn("nonstandard_json_constant", meta["error"])

    def test_wire_timestamp_coercion_cannot_erase_malformed_timing(self):
        for provider in bc.KLINE_ORIGINS:
            for transform in (lambda x: str(x)+".00000001", str, float,
                              lambda x: x+.5, lambda x: True):
                payload = raw_rows(provider)
                rows = payload["result"]["XXBTZUSD"] if provider == "kraken" else payload
                rows[5][0] = transform(rows[5][0])
                with self.subTest(provider=provider, value=rows[5][0]):
                    normalized, meta = self.fetch(provider, payload)
                    self.assertIsNone(normalized)
                    self.assertIn("timestamp_wire_type_invalid", meta["error"])
                    ctx = context((normalized, meta))
                    self.assertFalse(ctx.valid)
                    for strategy in (BtcModelStrategy, BtcDailyStrategy):
                        with patch("btc_probability_model.probability_yes") as model:
                            strategy(lambda **kw: ctx).evaluate({"floor_strike": 65000}, {}, 10)
                        model.assert_not_called()
        for transform in (lambda x: str(x)+".00000001", str, float):
            payload = raw_rows("binance")
            payload[5][6] = transform(payload[5][6])
            normalized, meta = self.fetch("binance", payload)
            self.assertIsNone(normalized)
            self.assertIn("close_timestamp_wire_type_invalid", meta["error"])

    def test_raw_decimal_sign_and_ohlc_bounds_survive_conversion(self):
        for provider in bc.KLINE_ORIGINS:
            columns = {"binance": (1, 2, 3, 4, 5),
                       "kraken": (1, 2, 3, 4, 6),
                       "coinbase": (3, 2, 1, 4, 5)}[provider]
            for bad_kind in ("negative_underflow", "positive_underflow", "inverted_bounds"):
                payload = raw_rows(provider)
                rows = payload["result"]["XXBTZUSD"] if provider == "kraken" else payload
                o, h, l, c, v = columns
                if bad_kind == "inverted_bounds":
                    for index in (o, l, c):
                        rows[5][index] = "65000.000000000000000000000001"
                    rows[5][h] = "65000"
                else:
                    rows[5][v] = "-1e-400" if bad_kind == "negative_underflow" else "1e-400"
                with self.subTest(provider=provider, bad_kind=bad_kind):
                    normalized, meta = self.fetch(provider, payload)
                    self.assertIsNone(normalized)
                    self.assertIsNotNone(meta["error"])

    def test_json_number_precision_preserved_before_ohlcv_validation(self):
        from requests import Response
        payload = raw_rows("coinbase")
        for index in (1, 3, 4):
            payload[5][index] = "EXACT_DECIMAL_MARKER"
        payload[5][2] = 65000
        raw = json.dumps(payload).replace('"EXACT_DECIMAL_MARKER"',
                                         '65000.000000000000000000000001')
        response = Response()
        response.status_code = 200
        response.url = bc.KLINE_ORIGINS["coinbase"][1]
        response.encoding = "utf-8"
        response._content = raw.encode("utf-8")
        with patch("requests.get", return_value=response), patch.object(bc.time, "time", return_value=NOW):
            normalized, meta = bc.fetch_klines_coinbase()
        self.assertIsNone(normalized)
        self.assertIn("exact_ohlcv_invalid", meta["error"])

    def test_provider_open_tail_removed_before_model_inputs(self):
        for provider in bc.KLINE_ORIGINS:
            with self.subTest(provider=provider):
                rows, meta = self.fetch(provider)
                self.assertEqual(len(rows), 30, meta)
                self.assertEqual(rows[-1]["close_ts"], NOW)
                self.assertIsNone(bc._validate_klines(rows, NOW, provider))
                self.assertTrue(context(rows).valid)

    def test_request_start_closure_survives_network_delay(self):
        # Kraken final row stays excluded even after it becomes old by clock.
        payload = raw_rows("kraken")
        rows, meta = self.fetch("kraken", payload, clock=NOW+61)
        self.assertEqual(rows[-1]["close_ts"], NOW)
        self.assertIsNone(meta["error"])

    def test_provider_errors_incomplete_schema_and_future_rows_rejected(self):
        for provider in bc.KLINE_ORIGINS:
            for value in (None, {}, "malformed", [[NOW]]):
                # None handled explicitly since fetch(None) means fixture.
                fn = getattr(bc, "fetch_klines_"+provider)
                with patch.object(bc, "_http_get_json_meta", return_value=(value, META)), patch.object(bc.time, "time", return_value=NOW):
                    rows, meta = fn()
                self.assertIsNone(rows)
                self.assertIsNotNone(meta["error"])
            rows, meta = self.fetch(provider, raw_rows(provider, now=NOW+60))
            self.assertIsNone(rows)
        bad = raw_rows("kraken")
        bad["error"] = ["EGeneral:Internal error"]
        self.assertIsNone(self.fetch("kraken", bad)[0])
        del bad["error"]
        self.assertIsNone(self.fetch("kraken", bad)[0])

    def test_kraken_unknown_incomplete_or_late_error_envelope_rejected(self):
        for field, value in (("partial", True), ("complete", False),
                             ("errors", ["late shard failure"]), ("next", "unread")):
            payload = raw_rows("kraken")
            payload[field] = value
            with self.subTest(field=field):
                rows, meta = self.fetch("kraken", payload)
                self.assertIsNone(rows)
                self.assertIsNotNone(meta["error"])
        for field in ("partial", "errors", "next"):
            payload = raw_rows("kraken")
            payload["result"][field] = True
            self.assertIsNone(self.fetch("kraken", payload)[0])

    def test_bad_binance_close_and_malformed_open_tail_rejected(self):
        payload = raw_rows("binance")
        payload[-1][6] += 1
        self.assertIsNone(self.fetch("binance", payload)[0])
        for provider in bc.KLINE_ORIGINS:
            payload = raw_rows(provider)
            rows = payload["result"]["XXBTZUSD"] if provider == "kraken" else payload
            rows[0 if provider == "coinbase" else -1][1] = "nan"
            self.assertIsNone(self.fetch(provider, payload)[0])

    def test_fallback_skips_malformed_provider_and_does_not_cache_it(self):
        bad = candles(now=NOW, provider="binance")
        bad[-1]["ts"] += 3600
        good = candles(now=NOW)
        rows, source = bc.fetch_klines_with_fallback(providers=[
            ("binance", lambda limit: (bad, META)),
            ("kraken", lambda limit: (good, META))], now=NOW)
        self.assertEqual(source, "fresh:kraken")
        self.assertEqual(rows, good)
        self.assertEqual(bc._last_good_klines["provider"], "kraken")

    def test_fallback_rejects_wrong_provider_binding_and_late_errors(self):
        for name, meta in (("coinbase", META), ("kraken", {**META, "error": "late_error"}),
                           ("kraken", {})):
            rows, source = bc.fetch_klines_with_fallback(
                providers=[(name, lambda limit: (candles(now=NOW), meta))], now=NOW)
            self.assertIsNone(rows)
            self.assertEqual(source, "none")
            self.assertIsNone(bc._last_good_klines["kl"])

    def test_incomplete_provider_history_never_accepted_or_cached(self):
        rows, source = bc.fetch_klines_with_fallback(
            providers=[("kraken", lambda limit: (candles(10, NOW), META))], now=NOW)
        self.assertEqual(source, "partial:kraken(10)")
        self.assertEqual(len(rows), 10)
        self.assertIsNone(bc._last_good_klines["kl"])

    def test_outage_never_reuses_previous_provider_response(self):
        bc.fetch_klines_with_fallback(providers=[("kraken", lambda limit: (candles(now=NOW), META))], now=NOW)
        rows, source = bc.fetch_klines_with_fallback(providers=[("kraken", lambda limit: (None, {"error": "timeout"}))], now=NOW+1)
        self.assertIsNone(rows)
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
