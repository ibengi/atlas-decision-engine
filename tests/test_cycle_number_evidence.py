# -*- coding: utf-8 -*-
"""A completed cycle keeps its cycle number in every durable record.

THE DEFECT
    `_finish_cycle` walked the funnel stages with a loop variable named `n`
    -- the same name as its cycle-number parameter. The last stage is
    `fills`, so after the loop `n` was the fill count, and every completed
    cycle was recorded as cycle=0 in cycles.jsonl, cycle_report.json,
    dashboard_state.json, pipeline_stats.json and the [CYCLE-SUMMARY] line.
    Blocked cycles return before the loop and numbered correctly, so the
    defect only surfaced once a production cycle completed.

Every test here runs the REAL `_finish_cycle` and reads the number back
from the file or sink it was written to.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import test_shadow_write_layer_isolation as shadow_iso    # noqa: E402
from config import _p                                     # noqa: E402
from execution_engine import ExecutionEngine              # noqa: E402
from persistence import JsonStore                         # noqa: E402


class _Sink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(json.loads(json.dumps(row)))


def _engine(fills_per_decision=0):
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.client = type("C", (), {"env": "prod"})()
    eng.cycles_jsonl = _Sink()
    eng.stats = type("S", (), {"log_summary": lambda self: None})()
    eng.orders = type("O", (), {"exchange_pause_until": 0.0})()
    eng.scanner = type("Sc", (), {"shadow_population": lambda self: []})()
    eng.btc_daily_shadow = type("B", (), {"run": lambda self, pop, cid: {}})()
    eng.capital = eng.configured_capital = eng.last_balance = 9.84
    eng._execute_decision = lambda dec, report: fills_per_decision
    return eng


def _res(accepted=0):
    return {"report": {"cycle_id": "cyc-num", "scanned_raw": 201,
                       "scanned": 201, "ranker_eligible": 3,
                       "accepted": accepted, "rejections": {},
                       "fills": 0},
            "accepted": [object() for _ in range(accepted)]}


class ACompletedCycleKeepsItsNumber(shadow_iso._IsolatedState,
                                    unittest.TestCase):

    def setUp(self):
        shadow_iso._IsolatedState.setUp(self)

    def tearDown(self):
        shadow_iso._IsolatedState.tearDown(self)

    def _records(self, eng):
        return {
            "evidence_row": eng.cycles_jsonl.rows[-1]["cycle"],
            "cycle_report.json": JsonStore.load(_p("cycle_report.json"), {})
            .get("cycle"),
            "dashboard_state.json": JsonStore.load(
                _p("dashboard_state.json"), {}).get("cycle"),
            "pipeline_stats.json": JsonStore.load(
                _p("pipeline_stats.json"), {}).get("cycle"),
            "reject_reasons.json": JsonStore.load(
                _p("reject_reasons.json"), {}).get("cycle"),
        }

    def test_cycle_123_is_persisted_as_123_everywhere(self):
        eng = _engine()
        with self.assertLogs("BOT", level="INFO") as logs:
            ExecutionEngine._finish_cycle(eng, 123, _res(), "sequential")
        for where, value in self._records(eng).items():
            with self.subTest(record=where):
                self.assertEqual(value, 123, f"{where} lost the cycle number")
        summary = [l for l in logs.output if "CYCLE-SUMMARY" in l][-1]
        self.assertIn('"cycle": 123', summary)

    def test_zero_fills_does_not_turn_the_cycle_into_zero(self):
        """The historical failure: fills=0 became the cycle number."""
        eng = _engine(fills_per_decision=0)
        ExecutionEngine._finish_cycle(eng, 264, _res(accepted=1), "sequential")
        self.assertEqual(eng.cycles_jsonl.rows[-1]["cycle"], 264)
        self.assertEqual(JsonStore.load(_p("cycle_report.json"), {})["fills"],
                         0, "the fill count itself must still be recorded")

    def test_non_zero_fills_do_not_replace_the_cycle_number(self):
        eng = _engine(fills_per_decision=1)
        ExecutionEngine._finish_cycle(eng, 500, _res(accepted=2), "parallel")
        row = eng.cycles_jsonl.rows[-1]
        self.assertEqual(row["cycle"], 500)
        self.assertEqual(row["execution_path"], "parallel")
        report = JsonStore.load(_p("cycle_report.json"), {})
        self.assertEqual(report["fills"], 2)
        self.assertEqual(report["cycle"], 500)

    def test_the_funnel_conversion_is_still_computed_from_stage_counts(self):
        """The renamed variable must keep feeding the funnel, not vanish."""
        eng = _engine()
        res = _res()
        res["report"].update(model_evaluated=5, positive_edge=3,
                             positive_net_ev=3, risk_passed=0)
        ExecutionEngine._finish_cycle(eng, 7, res, "sequential")
        conv = JsonStore.load(_p("cycle_report.json"), {})["funnel_conversion"]
        self.assertEqual(conv["scanned_raw"]["n"], 201)
        self.assertEqual(conv["model_evaluated"]["n"], 5)
        self.assertEqual(conv["positive_edge"]["pct_of_prev"], 60.0)
        self.assertEqual(conv["risk_passed"]["n"], 0)

    def test_consecutive_cycles_are_uniquely_numbered(self):
        eng = _engine()
        for n in (10, 11, 12):
            ExecutionEngine._finish_cycle(eng, n, _res(), "sequential")
        self.assertEqual([r["cycle"] for r in eng.cycles_jsonl.rows],
                         [10, 11, 12])


if __name__ == "__main__":
    unittest.main()
