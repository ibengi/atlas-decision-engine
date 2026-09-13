"""LI-06 synthetic invariants: no engine instance, provider I/O or credentials.

The model boundary is the actual strategy AST, with its broker-related imports
excluded. Providers are deterministic local functions; HTTP adapters use a stub.
"""
import ast
import copy
import dataclasses
import hashlib
import json
from pathlib import Path
import os
import typing
import unittest
from unittest.mock import patch

import btc_context as bc

NOW = 1_800_000_000.0
ROOT = Path(bc.__file__).resolve().parent


def rows(end=NOW - 60, step=60, count=30):
    return [{"ts": end - (count - 1 - i) * step, "open": 65000.,
             "high": 65040., "low": 64980.,
             "close": 65000. + (25. if i % 2 else -15.), "volume": 5.}
            for i in range(count)]


def serve(value):
    return lambda limit=30: (copy.deepcopy(value), {"http_status": 200})


def spots(now=NOW):
    return tuple(lambda name=name: {"source": name, "price": 65000., "ts": now}
                 for name in ("synthetic-a", "synthetic-b", "synthetic-c"))


def strategy(ctx):
    names = {"ModelOutput", "Strategy", "_strike_with_source",
             "_BtcAboveStrikeBase", "BtcModelStrategy", "BtcDailyStrategy"}
    nodes = [node for node in ast.parse((ROOT / "strategy_router.py").read_text()).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    assert len(nodes) == len(names)
    ns = dict(dataclass=dataclasses.dataclass, field=dataclasses.field,
              asdict=dataclasses.asdict, Optional=typing.Optional, Callable=typing.Callable)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "li06-strategy-boundary", "exec"), ns)
    return [ns[name](context_provider=lambda **kw: ctx).evaluate(
        {"floor_strike": 65000.}, {}, horizon)
        for name, horizon in (("BtcModelStrategy", 10.), ("BtcDailyStrategy", 1200.))]


class TestLi06CandleQualification(unittest.TestCase):
    def setUp(self):
        bc.clear_cache()
        self.env = patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "0"})
        self.env.start()
        self.no_http = patch.object(bc, "_http_get_json_meta",
                                    side_effect=AssertionError("real HTTP forbidden"))
        self.no_http.start()

    def tearDown(self):
        self.no_http.stop()
        self.env.stop()
        bc.clear_cache()

    def context(self, value, now=NOW, use_cache=False):
        return bc.get_btc_context(strike=65000., minutes_remaining=10.,
                                  spot_sources=spots(now), klines_fn=lambda: value,
                                  now=now, use_cache=use_cache)

    def refused(self, value):
        ctx = self.context(value)
        self.assertFalse(ctx.valid)
        self.assertIsNone(ctx.realized_vol_1m)
        self.assertEqual(ctx.returns, {})
        self.assertTrue(all(not out.valid and out.probability_yes is None
                            for out in strategy(ctx)))
        return ctx

    def test_complete_closed_window_positive(self):
        ctx = self.context(rows())
        self.assertTrue(ctx.valid)
        self.assertTrue(all(out.valid for out in strategy(ctx)))

    def test_original_future_365_day_witness(self):
        self.refused(rows(NOW + 365 * 86400))

    def test_original_hourly_as_minute_witness(self):
        self.refused(rows(step=3600))

    def test_wrong_cadence_30_seconds(self):
        self.refused(rows(step=30))

    def test_future_interior_row_not_silently_dropped(self):
        value = rows(); value[7]["ts"] = NOW + 60
        self.refused(value)

    def test_missing_interval(self):
        value = rows(); del value[7]
        self.refused(value)

    def test_duplicate_interval(self):
        value = rows(); value[7] = dict(value[6])
        self.refused(value)

    def test_reversed_window(self):
        self.refused(list(reversed(rows())))

    def test_off_grid_timestamp(self):
        self.refused(rows(NOW - 59))

    def test_boolean_timestamp(self):
        value = rows(); value[7]["ts"] = True
        self.refused(value)

    def test_timestamp_numeric_string_requires_wire_adapter(self):
        value = rows(); value[7]["ts"] = str(value[7]["ts"])
        self.refused(value)

    def test_huge_nan_infinite_timestamp(self):
        for bad in (10**500, float("inf"), float("nan"), None, [], {}):
            with self.subTest(kind=type(bad).__name__, value=str(bad)[:20]):
                value = rows(); value[7]["ts"] = bad
                self.refused(value)

    def test_negative_and_epoch_timestamp(self):
        for bad in (-60, 0):
            value = rows(); value[7]["ts"] = bad
            self.refused(value)

    def test_malformed_entire_container_or_member(self):
        for value in ({"candles": rows()}, tuple(rows()), rows()[:7]+[None]+rows()[8:]):
            self.refused(value)

    def test_missing_required_fields(self):
        for field in ("ts", "open", "high", "low", "close", "volume"):
            with self.subTest(field=field):
                value = rows(); del value[7][field]
                self.refused(value)

    def test_boolean_nonfinite_ohlcv(self):
        for field in ("open", "high", "low", "close", "volume"):
            for bad in (True, float("nan"), float("inf"), 10**500):
                with self.subTest(field=field, kind=str(bad)[:20]):
                    value = rows(); value[7][field] = bad
                    self.refused(value)

    def test_impossible_ohlc_ranges(self):
        for field, bad in (("high", 64000), ("low", 66000), ("close", 0),
                           ("open", -1), ("volume", -1)):
            value = rows(); value[7][field] = bad
            self.refused(value)

    def test_zero_volume_is_valid(self):
        value = rows()
        for row in value: row["volume"] = 0
        self.assertTrue(self.context(value).valid)

    def test_current_open_bar_is_explicitly_omitted(self):
        ctx = self.context(rows(NOW))
        self.assertTrue(ctx.valid)
        self.assertEqual(ctx.klines_count, 29)
        self.assertEqual(ctx.klines_provenance["last_close_ts"], NOW)

    def test_partial_window_after_open_bar_omission(self):
        self.refused(rows(NOW, count=11))

    def test_freshness_exact_boundary(self):
        self.assertTrue(self.context(rows(NOW - 180)).valid)
        self.assertFalse(self.context(rows(NOW - 180), now=NOW + .01).valid)

    def test_original_stale_first_falls_through(self):
        called = []
        def old(limit): called.append("binance"); return rows(NOW-3600), {}
        def fresh(limit): called.append("kraken"); return rows(), {}
        value, origin = bc.fetch_klines_with_fallback(
            providers=[("binance", old), ("kraken", fresh)], now=NOW)
        self.assertEqual(called, ["binance", "kraken"])
        self.assertEqual(origin, "fresh:kraken")
        self.assertTrue(self.context(value).valid)

    def test_malformed_first_falls_through(self):
        bad = rows(); bad[7]["close"] = float("nan")
        value, origin = bc.fetch_klines_with_fallback(
            providers=[("binance", serve(bad)), ("kraken", serve(rows()))], now=NOW)
        self.assertEqual(origin, "fresh:kraken")
        self.assertIsNotNone(value)

    def test_original_outage_cache_never_executable(self):
        bc.fetch_klines_with_fallback(providers=[("kraken", serve(rows()))], now=NOW)
        for age in (0, 1, 180, 601):
            value, origin = bc.fetch_klines_with_fallback(
                providers=[("binance", serve(None)), ("kraken", serve(None))], now=NOW+age)
            self.assertIsNone(value)
            self.assertEqual(origin, "none")

    def test_forged_stale_cache_injection_is_refused(self):
        with patch.object(bc, "fetch_klines_with_fallback", return_value=(rows(), "stale_cache:kraken(0s)")):
            ctx = bc.get_btc_context(spot_sources=spots(), now=NOW, use_cache=False)
        self.assertFalse(ctx.valid)

    def test_cycle_context_cannot_outlive_spot_freshness(self):
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            first = self.context(rows(), use_cache=True)
            self.assertTrue(first.valid)
            second = self.context(rows(), now=NOW+91, use_cache=True)
            self.assertFalse(second.valid)
            self.assertIsNot(first, second)

    def test_raw_cycle_cache_cannot_outlive_candle_freshness(self):
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            first = self.context(rows(), use_cache=True)
            self.assertTrue(first.valid)
            bc._cache["spot_sources"] = (list(f() for f in spots(NOW+121)), NOW)
            second = self.context(rows(), now=NOW+121, use_cache=True)
            self.assertFalse(second.valid)
            self.assertIn("klines:stale_closed_bar", second.quality_flags)

    def test_clock_rollback_cannot_reuse_context(self):
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            first = self.context(rows(), use_cache=True)
            self.assertTrue(first.valid)
            second = self.context(rows(), now=NOW-3600, use_cache=True)
            self.assertFalse(second.valid)

    def test_invalid_clock_is_structured_refusal(self):
        for now in (True, 10**500, float("nan"), float("inf"), "today", -1):
            self.assertFalse(self.context(rows(), now=now).valid)

    def test_provenance_bound_to_normalized_rows_and_retained_in_model(self):
        ctx = self.context(rows())
        canonical = json.dumps(rows(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        # Qualification normalizes integer timestamp/volume into floats.
        normalized, _ = bc.qualify_klines(rows(), NOW)
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(ctx.klines_provenance["normalized_sha256"], hashlib.sha256(canonical).hexdigest())
        for out in strategy(ctx):
            self.assertEqual(out.features["candle_provenance"], ctx.klines_provenance)
        altered = rows(); altered[-1]["close"] += 1
        self.assertNotEqual(ctx.klines_provenance["normalized_sha256"], self.context(altered).klines_provenance["normalized_sha256"])

    def test_wire_boolean_timestamp_rejected_before_coercion(self):
        with patch.object(bc, "_http_get_json_meta", return_value=([[True, "1", "2", "1", "2", "3"]], {})):
            value, meta = bc.fetch_klines_binance()
        self.assertIsNone(value)
        self.assertEqual(meta["error"], "parse_error")

    def test_wire_numeric_strings_permitted_but_boolean_price_refused(self):
        wire = [[int(r["ts"])*1000, str(r["open"]), str(r["high"]), str(r["low"]), str(r["close"]), str(r["volume"]), int(r["ts"])*1000+59999, "0", 1, "0", "0", "0"] for r in rows()]
        with patch.object(bc, "_http_get_json_meta", return_value=(wire, {})):
            value, _ = bc.fetch_klines_binance()
        self.assertTrue(self.context(value).valid)
        wire[7][4] = True
        with patch.object(bc, "_http_get_json_meta", return_value=(wire, {})):
            value, _ = bc.fetch_klines_binance()
        self.assertIsNone(value)

    def test_coinbase_order_is_verified_not_repaired(self):
        wire = [[r["ts"], r["low"], r["high"], r["open"], r["close"], r["volume"]] for r in reversed(rows())]
        with patch.object(bc, "_http_get_json_meta", return_value=(wire, {})):
            value, _ = bc.fetch_klines_coinbase()
        self.assertTrue(self.context(value).valid)
        wire[2], wire[3] = wire[3], wire[2]
        with patch.object(bc, "_http_get_json_meta", return_value=(wire, {})):
            value, _ = bc.fetch_klines_coinbase()
        self.assertIsNone(value)


    def test_provider_delay_cannot_extend_freshness(self):
        clock = [NOW]
        def delayed():
            clock[0] += 20
            return rows(NOW-180)
        with patch.object(bc.time, "time", side_effect=lambda: clock[0]):
            ctx = bc.get_btc_context(spot_sources=spots(), klines_fn=delayed, use_cache=False)
        self.assertFalse(ctx.valid)

    def test_partial_at_request_start_never_promoted_during_parse(self):
        clock = [NOW+59]
        def delayed(limit=30):
            clock[0] = NOW+61
            return rows(NOW), {}
        with patch.object(bc.time, "time", side_effect=lambda: clock[0]):
            value, origin = bc.fetch_klines_with_fallback(providers=[("binance", delayed)])
        self.assertEqual(origin, "fresh:binance")
        self.assertEqual(len(value), 29)
        self.assertEqual(value[-1]["ts"], NOW-60)

    def test_injected_cache_never_promotes_previous_partial_bar(self):
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW+59):
            first = self.context(rows(NOW), now=NOW+59, use_cache=True)
            self.assertTrue(first.valid)
            second = bc.get_btc_context(strike=65001., minutes_remaining=10.,
                spot_sources=spots(NOW+61), klines_fn=lambda: rows(NOW), now=NOW+61, use_cache=True)
            self.assertTrue(second.valid)
            self.assertEqual(second.klines_provenance["last_close_ts"], NOW)

    def test_binance_contradictory_close_timestamp(self):
        wire = [[r["ts"]*1000, str(r["open"]), str(r["high"]), str(r["low"]),
                 str(r["close"]), str(r["volume"]), r["ts"]*1000+3599999,
                 "0", 1, "0", "0", "0"] for r in rows()]
        with patch.object(bc, "_http_get_json_meta", return_value=(wire, {})):
            value, _ = bc.fetch_klines_binance()
        self.assertIsNone(value)

    def test_kraken_error_envelope_never_becomes_success(self):
        wire = [[r["ts"], str(r["open"]), str(r["high"]), str(r["low"]),
                 str(r["close"]), "65000", str(r["volume"]), 1] for r in rows()]
        for error in (["synthetic_error"], False, None, "", {}):
            payload = {"error": error, "result": {"XXBTZUSD": wire, "last": NOW}}
            with patch.object(bc, "_http_get_json_meta", return_value=(payload, {})):
                value, _ = bc.fetch_klines_kraken()
            self.assertIsNone(value)

    def test_kraken_malformed_prefix_not_hidden_by_limit(self):
        wire = [[r["ts"], str(r["open"]), str(r["high"]), str(r["low"]),
                 str(r["close"]), "65000", str(r["volume"]), 1] for r in rows()]
        wire.insert(0, [NOW+3600, "65000", "65040", "64980", "65025", "65000", "1", 1])
        payload = {"error": [], "result": {"XXBTZUSD": wire, "last": NOW}}
        with patch.object(bc, "_http_get_json_meta", return_value=(payload, {})):
            value, origin = bc.fetch_klines_with_fallback(providers=[("kraken", bc.fetch_klines_kraken)], now=NOW)
        self.assertIsNone(value)

    def test_all_provider_rows_validated_then_last30_used(self):
        many = rows(count=60)
        value, _ = bc.fetch_klines_with_fallback(providers=[("kraken", serve(many))], now=NOW)
        self.assertEqual(len(value), 30)
        self.assertEqual(value, many[-30:])

    def known_model(self):
        with patch.object(bc, "fetch_klines_with_fallback", return_value=(rows(), "fresh:kraken")):
            ctx = bc.get_btc_context(spot_sources=spots(), now=NOW, use_cache=False)
        return strategy(ctx)[0].to_dict()

    def test_execution_proof_expiry_and_scalar_identity(self):
        mo = self.known_model()
        self.assertTrue(bc.decision_candles_current(mo, now=NOW))
        self.assertTrue(bc.decision_candles_current(mo, now=NOW+90))
        self.assertFalse(bc.decision_candles_current(mo, now=NOW+90.01))
        for field in ("sigma_1m", "ret_5m"):
            corrupt = copy.deepcopy(mo); corrupt["features"][field] = True
            self.assertFalse(bc.decision_candles_current(corrupt, now=NOW))

    def test_execution_proof_missing_or_tampered_refused(self):
        mo = self.known_model()
        for field, bad in (("source", "fresh:unknown"), ("normalized_sha256", "0"*64),
                           ("row_count", True), ("endpoint", "https://other.invalid"),
                           ("schema", "old"), ("degraded_cache_allowed", 0),
                           ("validated_at", NOW+60), ("valid_until", NOW+3600)):
            corrupt = copy.deepcopy(mo); corrupt["features"]["candle_provenance"][field] = bad
            self.assertFalse(bc.decision_candles_current(corrupt, now=NOW), field)
        self.assertFalse(bc.decision_candles_current({"valid": True}, now=NOW))

    def test_exact_engine_gate_keys_off_ticker_even_if_label_renamed(self):
        import types
        import logging
        tree = ast.parse((ROOT / "execution_engine.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name=="ExecutionEngine")
        gate = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name=="_candle_input_gate")
        ns = {"log_trd": logging.getLogger("synthetic")}
        exec(compile(ast.Module(body=[gate], type_ignores=[]), "actual-engine-candle-gate", "exec"), ns)
        dec = types.SimpleNamespace(ticker="KXBTC15M-SYNTHETIC", market_type="sports_moneyline", model_output=self.known_model())
        report = {"rejections": {}}
        with patch.object(bc.time, "time", return_value=NOW):
            self.assertTrue(ns[gate.name](object(), dec, report))
        with patch.object(bc.time, "time", return_value=NOW+91):
            self.assertFalse(ns[gate.name](object(), dec, report))
        dec.model_output = None
        with patch.object(bc.time, "time", return_value=NOW):
            self.assertFalse(ns[gate.name](object(), dec, report))
        dec.ticker = "KXNFL-SYNTHETIC"
        self.assertTrue(ns[gate.name](object(), dec, report))


    def release_branch(self, status):
        import types
        import logging
        tree = ast.parse((ROOT / "execution_engine.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name=="ExecutionEngine")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name=="_execute_decision")
        block = next(node for node in method.body if isinstance(node, ast.If) and ast.unparse(node.test)=="exec_res.filled <= 0")
        fn = ast.parse("def exact_release(self, exec_res, dec):\n ticker='synthetic'\n count=1\n entry=20\n").body[0]
        fn.body.append(block)
        ns = {"log_trd": logging.getLogger("synthetic")}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "actual-risk-release-block", "exec"), ns)
        releases = []
        owner = types.SimpleNamespace(risk=types.SimpleNamespace(release_half_open_attempt=lambda *args: releases.append(args)))
        result = types.SimpleNamespace(filled=0, order_id=None, status=status, state="rejected")
        ns[fn.name](owner, result, types.SimpleNamespace(side="yes"))
        return releases

    def test_first_attempt_timeout_then_expiry_keeps_half_open_reservation(self):
        self.assertEqual(self.release_branch("ambiguous:candle_expired_after_send:unavailable"), [])

    def test_proven_pre_send_expiry_can_release_half_open_reservation(self):
        self.assertEqual(len(self.release_branch("blocked:candle_expired_not_sent")), 1)


    def test_cycle_cache_recomputes_quality_when_one_spot_source_expires(self):
        mixed = (lambda: {"source": "old", "price": 65000., "ts": NOW-89},
                 lambda: {"source": "fresh-a", "price": 65000., "ts": NOW},
                 lambda: {"source": "fresh-b", "price": 65000., "ts": NOW})
        with patch.dict(os.environ, {"BTC_CONTEXT_CYCLE_CACHE": "1"}), patch.object(bc.time, "time", return_value=NOW):
            first = bc.get_btc_context(strike=65000., minutes_remaining=10.,
                spot_sources=mixed, klines_fn=lambda: rows(), now=NOW, use_cache=True)
            second = bc.get_btc_context(strike=65000., minutes_remaining=10.,
                spot_sources=mixed, klines_fn=lambda: rows(), now=NOW+2, use_cache=True)
        self.assertTrue(first.valid and second.valid)
        self.assertEqual(first.n_valid_sources, 3)
        self.assertEqual(second.n_valid_sources, 2)
        self.assertLess(second.data_quality_score, first.data_quality_score)


if __name__ == "__main__":
    unittest.main()
