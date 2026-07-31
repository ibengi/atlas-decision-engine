# -*- coding: utf-8 -*-
"""Tests de non-regression de la classification, avec les TICKERS REELS
observes dans les logs Railway du 2026-07-20 (exigence B)."""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from market_classifier import classify, series_root


def C(ticker, title="", category=""):
    return classify({"ticker": ticker, "title": title, "category": category})


class TestSeriesRoot(unittest.TestCase):
    def test_root(self):
        self.assertEqual(series_root("KXWNBASPREAD-26JUL20LVTOR-LV7"),
                         "KXWNBASPREAD")
        self.assertEqual(series_root(""), "")


class TestSportsRegressions(unittest.TestCase):
    """Bugs reels des logs : la sous-chaine 'gas' de 'Las VeGAS' et 'gold'
    de 'GOLDen State' classaient des matchs WNBA en Commodities."""

    def test_wnba_spread_lvtor_was_commodities(self):
        c = C("KXWNBASPREAD-26JUL20LVTOR-LV7",
              title="Las Vegas Aces vs Toronto: Las Vegas -7.5")
        self.assertEqual(c.market_type, "sports_spread")
        self.assertEqual(c.category, "Sports")          # etait Commodities
        self.assertEqual(c.family, "WNBA")

    def test_wnba_total_wshgs_was_commodities(self):
        c = C("KXWNBATOTAL-26JUL20WSHGS-152",
              title="Washington vs Golden State Valkyries: total 152+")
        self.assertEqual(c.market_type, "sports_total")
        self.assertEqual(c.category, "Sports")          # etait Commodities
        self.assertEqual(c.family, "WNBA")

    def test_wnba_spread_gs_side(self):
        c = C("KXWNBASPREAD-26JUL20WSHGS-GS10", title="Golden State -10.5")
        self.assertEqual((c.market_type, c.category),
                         ("sports_spread", "Sports"))

    def test_wnba_moneyline(self):
        c = C("KXWNBAGAME-26JUL20NYDAL-NY")
        self.assertEqual((c.market_type, c.category),
                         ("sports_moneyline", "Sports"))

    def test_mlb(self):
        self.assertEqual(C("KXMLBGAME-26JUL20NYMMIL-MIL").market_type,
                         "sports_moneyline")
        self.assertEqual(C("KXMLBSPREAD-26JUL20LADNYY-NYY2").market_type,
                         "sports_spread")
        self.assertEqual(C("KXMLBTOTAL-26JUL20LADNYY-9").market_type,
                         "sports_total")
        for t in ("KXMLBGAME-26JUL20NYMMIL-MIL",
                  "KXMLBSPREAD-26JUL20LADNYY-NYY2"):
            self.assertEqual(C(t).category, "Sports")

    def test_tennis_families_were_unknown(self):
        """ATP/WTA/ITF etaient market_type=unknown dans les logs."""
        self.assertEqual(C("KXATPMATCH-26JUL19NEUHAN-HAN").market_type,
                         "sports_moneyline")
        self.assertEqual(C("KXATPCHALLENGERMATCH-26JUL19ZAMPOL-POL").category,
                         "Sports")                       # etait Politics !
        self.assertEqual(C("KXATPGSPREAD-26JUL19FERVAN-VAN5").market_type,
                         "sports_spread")
        self.assertEqual(C("KXWTAMATCH-26JUL20PARHAV-HAV").market_type,
                         "sports_moneyline")
        self.assertEqual(C("KXITFWMATCH-26JUL19BASBOU-BAS").market_type,
                         "sports_moneyline")
        for t in ("KXWTAMATCH-26JUL20PARHAV-HAV",
                  "KXITFWMATCH-26JUL19BASBOU-BAS"):
            self.assertEqual(C(t).category, "Sports")

    def test_cfl_nascar_ufc(self):
        self.assertEqual(C("KXCFLGAME-26JUL19WPGOTT-OTT").market_type,
                         "sports_moneyline")
        self.assertEqual(C("KXNASCARRACE-26JUL20-CHASE").category, "Sports")
        self.assertEqual(C("KXUFCFIGHT-26JUL26-DOE").category, "Sports")

    def test_wnba_before_nba(self):
        self.assertEqual(C("KXWNBAGAME-26JUL20NYDAL-NY").family, "WNBA")
        self.assertEqual(C("KXNBAGAME-26OCT20LALBOS-LAL").family, "NBA")


class TestCryptoRegressions(unittest.TestCase):
    def test_btc_daily(self):
        c = C("KXBTCD-26JUL2017-T64999.99")
        self.assertEqual((c.market_type, c.category),
                         ("btc_above_strike_daily", "Crypto"))

    def test_btc_15m(self):
        c = C("KXBTC15M-26JUL201430-T65000")
        self.assertEqual((c.market_type, c.category),
                         ("btc_15m_above_strike", "Crypto"))

    def test_eth_xrp_were_other(self):
        self.assertEqual(C("KXETHD-26JUL2017-T1889.99").category, "Crypto")
        self.assertEqual(C("KXETH15M-26JUL192315-15").category, "Crypto")
        self.assertEqual(C("KXXRPD-26JUL2017-T1.0999").category, "Crypto")


class TestNonSports(unittest.TestCase):
    def test_aaagas_is_commodities_not_sports(self):
        """'KXAAAGASW' contient 'GAS' mais n'est PAS un marche sportif ;
        c'est un marche de prix de l'essence (Commodities)."""
        c = C("KXAAAGASW-26JUL20-4.000")
        self.assertEqual(c.category, "Commodities")
        self.assertNotIn(c.market_type, ("sports_moneyline", "sports_spread",
                                         "sports_total"))

    def test_cpi(self):
        c = C("KXECONSTATCPIYOY-26AUG-T3.6")
        self.assertEqual((c.market_type, c.category),
                         ("cpi_above_threshold", "Economics"))

    def test_mednom_is_not_election_winner(self):
        """Regression : KXMEDNOMJUL etait classe election_winner."""
        c = C("KXMEDNOMJUL-26AUG01-DKLE")
        self.assertNotEqual(c.market_type, "election_winner")

    def test_electionbill_is_not_winner(self):
        c = C("KXELECTIONBILL-26OCT01")
        self.assertEqual(c.category, "Politics")
        self.assertNotEqual(c.market_type, "election_winner")

    def test_pres_is_election_winner(self):
        c = C("KXPRES-28-DEM")
        self.assertEqual((c.market_type, c.category),
                         ("election_winner", "Politics"))

    def test_unknown_has_zero_confidence(self):
        c = C("KXRT-ODY-95")
        self.assertEqual(c.market_type, "unknown")
        self.assertEqual(c.confidence, 0)

    def test_unknown_never_scores_high(self):
        from market_ranker import tradability_score
        s = tradability_score({"ticker": "KXRT-ODY-95",
                               "yes_bid": 40, "yes_ask": 41,
                               "volume_24h": 100000,
                               "_minutes_remaining": 120})
        self.assertLessEqual(s, 10.0)


if __name__ == "__main__":
    unittest.main()
