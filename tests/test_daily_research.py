"""The daily loader must count events not cycles, refuse unconfirmed
labels, and stop when the label cannot discriminate."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import daily_research as dr


def pred(rid, ticker, minutes, observed_at, **kw):
    base = {"record_id": rid, "ticker": ticker, "observed_at": observed_at,
            "minutes_remaining": minutes, "predicted_probability": 0.6,
            "market_yes_ask": 50, "market_yes_bid": 47,
            "underlying_price": 77000.0, "strike": 76000.0,
            "sigma_1m": 0.0004, "ret_5m": 0.0001,
            "decision_accepted": False}
    base.update(kw)
    return base


def sett(rid, result, confirmed="status", settled_at="2026-09-01T23:00:00+00:00"):
    d = {"record_id": rid, "ticker": "x", "result": result,
         "settled_at": settled_at, "kind": "settlement"}
    if confirmed:
        d["confirmed_by"] = confirmed
    return d


class EventLevelTest(unittest.TestCase):
    def test_one_ticker_observed_every_cycle_is_one_event_per_checkpoint(self):
        preds = [pred(f"r{i}", "KXBTCD-A", 400 - i, f"2026-09-01T{10 + i // 60:02d}:{i % 60:02d}:00+00:00")
                 for i in range(50)]
        setts = [sett(f"r{i}", "yes") for i in range(50)]
        rows, _ = dr.load_events(preds, setts)
        # minutes 351..400 all map to the 360 or 720 checkpoint
        self.assertLessEqual(len(rows), 2)
        self.assertEqual({r["ticker"] for r in rows}, {"KXBTCD-A"})

    def test_the_last_observation_before_the_checkpoint_is_kept(self):
        preds = [pred("early", "KXBTCD-A", 700, "2026-09-01T10:00:00+00:00",
                      predicted_probability=0.1),
                 pred("late", "KXBTCD-A", 400, "2026-09-01T15:00:00+00:00",
                      predicted_probability=0.9)]
        setts = [sett("early", "yes"), sett("late", "yes")]
        rows, _ = dr.load_events(preds, setts)
        self.assertEqual([r["probability_yes"] for r in rows], [0.9])

    def test_unconfirmed_labels_are_excluded_by_default(self):
        preds = [pred("r1", "KXBTCD-A", 400, "2026-09-01T10:00:00+00:00")]
        rows, excl = dr.load_events(preds, [sett("r1", "no", confirmed=None)])
        self.assertEqual(rows, [])
        self.assertEqual(excl, {"unconfirmed_label": 1})
        rows, _ = dr.load_events(preds, [sett("r1", "no", confirmed=None)],
                                 allow_unconfirmed=True)
        self.assertEqual(len(rows), 1)

    def test_spot_proxy_strikes_are_excluded_by_default(self):
        preds = [pred("r1", "KXBTCD-A", 400, "2026-09-01T10:00:00+00:00",
                      strike_source="spot_proxy")]
        rows, excl = dr.load_events(preds, [sett("r1", "no")])
        self.assertEqual(rows, [])
        self.assertEqual(excl, {"spot_proxy_strike": 1})

    def test_observation_rows_do_not_settle_anything(self):
        preds = [pred("r1", "KXBTCD-A", 400, "2026-09-01T10:00:00+00:00")]
        s = sett("r1", "no")
        s["kind"] = "observation"
        rows, excl = dr.load_events(preds, [s])
        self.assertEqual(rows, [])
        self.assertEqual(excl, {"unsettled": 1})


class DegenerateLabelTest(unittest.TestCase):
    def _all_no(self, n=60):
        preds, setts = [], []
        for i in range(n):
            preds.append(pred(f"r{i}", f"KXBTCD-{i}", 400,
                              f"2026-09-01T{i % 24:02d}:{(i * 7) % 60:02d}:00+00:00"))
            setts.append(sett(f"r{i}", "no"))
        return preds, setts

    def test_an_all_no_journal_stops_the_study(self):
        # A model that always says "no" scores a perfect Brier here. That
        # is the journal talking, not the model — no ranking may be produced.
        preds, setts = self._all_no()
        rep = dr.study(preds, setts, n_folds=3)
        self.assertTrue(rep["label_health"]["degenerate"])
        self.assertEqual(rep["verdict"], "INDETERMINATE")
        self.assertNotIn("results", rep)

    def test_a_single_event_is_degenerate(self):
        preds = [pred("r1", "KXBTCD-A", 400, "2026-09-01T10:00:00+00:00")]
        rep = dr.study(preds, [sett("r1", "yes")])
        self.assertEqual(rep["verdict"], "INDETERMINATE")

    def test_a_mixed_label_lets_the_study_run(self):
        preds, setts = self._all_no(80)
        for s in setts[::3]:
            s["result"] = "yes"
        rep = dr.study(preds, setts, n_folds=3)
        self.assertFalse(rep["label_health"]["degenerate"])
        self.assertIn("results", rep)
        self.assertIn(rep["verdict"], ("CANDIDATE_BEATS_MARKET",
                                       "NO_MODEL_BEATS_MARKET"))


class SlicesTest(unittest.TestCase):
    def test_tradeable_slices_are_reported_separately(self):
        preds = [pred("r1", "KXBTCD-A", 400, "2026-09-01T10:00:00+00:00",
                      market_yes_ask=50, market_yes_bid=48),      # spread 2
                 pred("r2", "KXBTCD-B", 400, "2026-09-01T11:00:00+00:00",
                      market_yes_ask=60, market_yes_bid=40),      # spread 20
                 pred("r3", "KXBTCD-C", 400, "2026-09-01T12:00:00+00:00",
                      market_yes_ask=55, market_yes_bid=50,
                      decision_accepted=True)]                    # spread 5
        setts = [sett("r1", "yes"), sett("r2", "no"), sett("r3", "yes")]
        rows, _ = dr.load_events(preds, setts)
        s = dr.slices(rows)
        self.assertEqual(len(s["all"]), 3)
        self.assertEqual(len(s["spread_le_4"]), 1)
        self.assertEqual(len(s["spread_le_6"]), 2)
        self.assertEqual(len(s["spread_le_10"]), 2)
        self.assertEqual(len(s["pipeline_accepted"]), 1)


if __name__ == "__main__":
    unittest.main()
