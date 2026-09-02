"""The label audit must name an impossible label and refuse to guess."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import daily_label_audit as la


def spot(points):
    return la.SpotSeries(points)


def row(ticker, result, settled_at="2026-09-01T21:06:41+00:00", **kw):
    return {"record_id": kw.get("rid", ticker), "ticker": ticker,
            "result": result, "settled_at": settled_at, **kw}


# KXBTCD-26SEP0117 closes 17:00 ET = 21:00 UTC = 1788382800
CLOSE_17 = 1788296400.0


class TickerParsingTest(unittest.TestCase):
    def test_a_daily_ticker_yields_utc_close_and_strike(self):
        ts, strike = la.parse_ticker("KXBTCD-26SEP0117-T76499.99")
        self.assertEqual(ts, CLOSE_17)
        self.assertEqual(strike, 76499.99)

    def test_a_non_daily_ticker_is_not_parsed(self):
        self.assertEqual(la.parse_ticker("KXBTC15M-26SEP011945-45"), (None, None))
        self.assertEqual(la.parse_ticker(None), (None, None))


class ClassifyTest(unittest.TestCase):
    def test_no_while_the_underlying_is_far_above_the_strike_is_impossible(self):
        self.assertEqual(la.classify("no", 68000.0, 77360.0), "IMPOSSIBLE")

    def test_yes_while_the_underlying_is_far_below_the_strike_is_impossible(self):
        self.assertEqual(la.classify("yes", 90000.0, 77360.0), "IMPOSSIBLE")

    def test_a_label_that_agrees_with_the_price_is_consistent(self):
        self.assertEqual(la.classify("no", 90000.0, 77360.0), "CONSISTENT")
        self.assertEqual(la.classify("yes", 68000.0, 77360.0), "CONSISTENT")

    def test_inside_the_margin_nothing_is_decided(self):
        # 0.2% away: the settlement index and the engine's consensus can
        # legitimately disagree by that much. Neither verdict is earned.
        self.assertEqual(la.classify("no", 77200.0, 77360.0), "AMBIGUOUS")
        self.assertEqual(la.classify("yes", 77200.0, 77360.0), "AMBIGUOUS")

    def test_without_a_price_the_label_is_unverifiable_not_fine(self):
        self.assertEqual(la.classify("no", 68000.0, None), "UNVERIFIABLE")


class AuditTest(unittest.TestCase):
    def test_one_verdict_per_event_not_per_journal_row(self):
        # Five rows of one ticker are one outcome; the audit must not count
        # five impossible labels.
        rows = [row("KXBTCD-26SEP0117-T67999.99", "no", rid=f"r{i}")
                for i in range(5)]
        rep = la.audit(rows, spot([(CLOSE_17, 77360.0)]))
        self.assertEqual(rep["events"], 1)
        self.assertEqual(rep["journal_rows"], 5)
        self.assertEqual(rep["verdicts"], {"IMPOSSIBLE": 1})
        self.assertFalse(rep["label_trustworthy"])

    def test_an_all_consistent_journal_is_trustworthy(self):
        rows = [row("KXBTCD-26SEP0117-T87999.99", "no"),
                row("KXBTCD-26SEP0117-T67999.99", "yes")]
        rep = la.audit(rows, spot([(CLOSE_17, 77360.0)]))
        self.assertEqual(rep["verdicts"], {"CONSISTENT": 2})
        self.assertTrue(rep["label_trustworthy"])

    def test_a_close_outside_the_spot_series_is_unverifiable_not_trusted(self):
        rows = [row("KXBTCD-26SEP0217-T67999.99", "no")]     # a day later
        rep = la.audit(rows, spot([(CLOSE_17, 77360.0)]))
        self.assertEqual(rep["verdicts"], {"UNVERIFIABLE": 1})
        self.assertFalse(rep["label_trustworthy"])   # nothing verified

    def test_observation_rows_are_not_labels(self):
        rows = [row("KXBTCD-26SEP0117-T67999.99", "no", kind="observation")]
        rep = la.audit(rows, spot([(CLOSE_17, 77360.0)]))
        self.assertEqual(rep["events"], 0)

    def test_a_spot_sample_too_far_from_close_is_not_used(self):
        rows = [row("KXBTCD-26SEP0117-T67999.99", "no")]
        far = spot([(CLOSE_17 + 3600.0, 77360.0)])      # an hour off
        rep = la.audit(rows, far, tolerance_s=900.0)
        self.assertEqual(rep["verdicts"], {"UNVERIFIABLE": 1})

    def test_the_journal_close_time_overrides_the_ticker_convention(self):
        # If the row carries the API's close_time, that wins over the
        # Eastern-time inference from the ticker.
        rows = [row("KXBTCD-26SEP0117-T67999.99", "no",
                    market_close_time="2026-09-01T22:00:00Z")]
        rep = la.audit(rows, spot([(CLOSE_17 + 3600.0, 77360.0)]))
        self.assertEqual(rep["verdicts"], {"IMPOSSIBLE": 1})

    def test_impossible_rows_are_named_and_ordered_by_margin(self):
        rows = [row("KXBTCD-26SEP0117-T67999.99", "no"),
                row("KXBTCD-26SEP0117-T75999.99", "no")]
        rep = la.audit(rows, spot([(CLOSE_17, 77360.0)]))
        self.assertEqual([x["ticker"] for x in rep["impossible"]],
                         ["KXBTCD-26SEP0117-T67999.99",
                          "KXBTCD-26SEP0117-T75999.99"])
        self.assertIsNone(rep["impossible"][0]["confirmed_by"])


if __name__ == "__main__":
    unittest.main()
