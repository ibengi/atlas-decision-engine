# -*- coding: utf-8 -*-
"""AUD-PROBE-002 (2026-08-27) — visibilite des cotes dans la sonde demo.

Contexte : 361 tentatives x ~202 marches -> best_ask_seen=n/a. Cause :
la decouverte lisait int(m["yes_ask"]) brut, invisible pour les formats
REELS du schema V2 (chaines decimales, champs *_dollars fixed-point,
cote vide encodee 0/"1.0000", cote YES derivable du carnet NO) que le
MOTEUR gere depuis le 2026-07-25 (market_scanner.read_price). La sonde
utilise desormais le MEME parseur + un entonnoir de rejets TYPES.

Pinne les 15 exigences de l'audit : detection exacte 30c/5c (INCHANGES),
rejets 31c/6c, raisons typees missing_ask/missing_bid/not_open/
invalid_price, correspondance de series, ticker marche != prefixe serie
sans casse silencieuse, formats cents entiers / decimaux / fixed-point
(supportes ou fail-closed EXPLICITE), cote YES, cote NO derivee, objet
malforme fail-closed, best_ask_seen numerique des qu'UN ask est
parsable, n/a UNIQUEMENT si aucun ask parsable.

Aucun reseau, aucun ordre (fonctions de decouverte uniquement).
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_demo_execution_check as kdc


def mkt(ticker="KXBTCD-T1", **fields):
    m = {"ticker": ticker, "status": "active"}
    m.update(fields)
    return m


class SeriesClient:
    """Sert des listes par serie et enregistre les series demandees."""

    def __init__(self, by_series):
        self.by_series = by_series
        self.requested = []

    def get_markets(self, series, status="open", limit=100):
        self.requested.append(series)
        return self.by_series.get(series, [])


def classify(m):
    return kdc.classify_market(m)[0]


class TestEligibilityExactCriteria(unittest.TestCase):
    # 1. marche V2 valide a EXACTEMENT ask=30c / spread=5c -> detecte
    def test_valid_market_at_exact_bounds_detected(self):
        m = mkt(yes_ask=30, yes_bid=25)
        self.assertIsNone(classify(m))
        self.assertEqual(kdc.market_is_eligible(m), (30, 25))

    # 2. ask 31c -> rejete (critere INCHANGE)
    def test_ask_31_rejected(self):
        self.assertEqual(classify(mkt(yes_ask=31, yes_bid=28)),
                         "ask_too_high")
        self.assertEqual(kdc.MAX_ASK_CENTS, 30)

    # 3. spread 6c -> rejete (critere INCHANGE)
    def test_spread_6_rejected(self):
        self.assertEqual(classify(mkt(yes_ask=25, yes_bid=19)),
                         "spread_too_wide")
        self.assertEqual(kdc.MAX_SPREAD_CENTS, 5)

    # 4. ask absent -> raison TYPEE
    def test_missing_ask_typed(self):
        self.assertEqual(classify(mkt(yes_bid=20)), "missing_ask")
        # cote VIDE encodee (0 cents / "1.0000" dollars) = missing aussi
        self.assertEqual(classify(mkt(yes_ask=0, yes_bid=20)),
                         "missing_ask")
        self.assertEqual(classify(mkt(yes_ask_dollars="1.0000",
                                      yes_bid=20)), "missing_ask")

    # 5. bid absent -> raison TYPEE
    def test_missing_bid_typed(self):
        self.assertEqual(classify(mkt(yes_ask=25)), "missing_bid")
        self.assertEqual(classify(mkt(yes_ask=25, yes_bid=0)),
                         "missing_bid")

    # 6. marche ferme -> rejete not_open (validation RENFORCEE)
    def test_closed_market_rejected(self):
        for st in ("closed", "settled", "finalized", "determined"):
            self.assertEqual(classify(mkt(status=st, yes_ask=25,
                                          yes_bid=22)), "not_open", st)
        # statuts ouverts acceptes ; statut absent tolere (la requete
        # filtre deja status=open) mais compte status_unknown en funnel
        self.assertIsNone(classify(mkt(status="open", yes_ask=25,
                                       yes_bid=22)))
        self.assertIsNone(classify(mkt(status="active", yes_ask=25,
                                       yes_bid=22)))
        m = mkt(yes_ask=25, yes_bid=22)
        del m["status"]
        self.assertIsNone(classify(m))


class TestSeriesDiscovery(unittest.TestCase):
    # 7. correspondance de series : chaque serie candidate interrogee
    #    dans l'ordre ; premier series avec candidat retenu
    def test_all_candidate_series_queried_in_order(self):
        cli = SeriesClient({s: [] for s in kdc.CANDIDATE_SERIES})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertIsNone(cand)
        self.assertEqual(cli.requested, list(kdc.CANDIDATE_SERIES))

    def test_first_series_with_candidate_wins(self):
        cli = SeriesClient({
            "KXBTCD": [mkt("KXBTCD-A", yes_ask=22, yes_bid=19)],
            "KXBTC15M": [mkt("KXBTC15M-B", yes_ask=10, yes_bid=8)]})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertEqual(cand, ("KXBTCD-A", 22))
        self.assertEqual(cli.requested, ["KXBTCD"])     # arret au 1er hit

    # 8. ticker marche != prefixe serie : COMPTE mais ne casse JAMAIS la
    #    decouverte en silence (le filtre series_ticker de l'API fait foi)
    def test_ticker_prefix_mismatch_counted_not_excluded(self):
        cli = SeriesClient({"KXBTCD": [
            mkt("BTCUSD-OTHER-NAME", yes_ask=24, yes_bid=21,
                event_ticker="KXBTCD-EVT")]})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertEqual(cand, ("BTCUSD-OTHER-NAME", 24))   # decouvert
        self.assertEqual(funnel["series_mismatch"], 1)      # et visible


class TestPriceRepresentations(unittest.TestCase):
    # 9. cents entiers (et chaines decimales de cents) parses
    def test_integer_and_decimal_cent_fields(self):
        self.assertEqual(kdc.parse_quote(mkt(yes_ask=25, yes_bid=22)),
                         (25, 22, None))
        self.assertEqual(kdc.parse_quote(mkt(yes_ask="25", yes_bid="22")),
                         (25, 22, None))
        # regression production : int("48.00") levait ValueError -> le
        # marche devenait invisible ; le parseur du moteur lit 48
        self.assertEqual(kdc.parse_quote(mkt(yes_ask="48.00",
                                             yes_bid="46.00")),
                         (48, 46, None))

    # 10. variantes fixed-point *_dollars supportees ; valeur dollars
    #     dans le champ cents -> fail-closed EXPLICITE (raison typee)
    def test_dollars_fixed_point_supported(self):
        self.assertEqual(kdc.parse_quote(mkt(yes_ask_dollars="0.2400",
                                             yes_bid_dollars="0.2100")),
                         (24, 21, None))
        self.assertIsNone(classify(mkt(yes_ask_dollars="0.3000",
                                       yes_bid_dollars="0.2500")))

    def test_dollars_value_in_cents_field_fails_closed(self):
        # "0.24" dans le champ cents : jamais interprete (ni 0c ni 24c) ;
        # rejet type missing_ask — pas d'heuristique silencieuse
        ask, bid, reason = kdc.parse_quote(mkt(yes_ask="0.24", yes_bid=20))
        self.assertIsNone(ask)
        self.assertEqual(reason, "missing_ask")

    # 11. cote YES directe
    def test_yes_side_quote(self):
        self.assertEqual(kdc.market_is_eligible(
            mkt(yes_ask=28, yes_bid=24)), (28, 24))

    # 12. cote NO : cote YES derivee du carnet NO (acheter YES a p ==
    #     croiser un bid NO a 100-p ; meme regle que liquidity_diag)
    def test_no_side_quote_derivation(self):
        m = mkt(no_bid=72, no_ask=76)          # -> YES ask 28, bid 24
        self.assertEqual(kdc.parse_quote(m), (28, 24, None))
        self.assertEqual(kdc.market_is_eligible(m), (28, 24))
        # NO seul avec ask derive > 30c -> rejet normal, pas invisible
        self.assertEqual(classify(mkt(no_bid=40, no_ask=45)),
                         "ask_too_high")

    # 13. objet API malforme -> fail-closed TYPE, jamais d'exception
    def test_malformed_object_fails_closed(self):
        self.assertEqual(classify({"ticker": "T"}), "missing_ask")
        self.assertEqual(classify(mkt(yes_ask="garbage", yes_bid=20)),
                         "invalid_price")
        self.assertEqual(classify(mkt(yes_ask=[25], yes_bid=20)),
                         "invalid_price")
        self.assertEqual(classify(mkt(yes_ask=-5, yes_bid=20)),
                         "missing_ask")       # negatif = pas de cote


class TestBestAskSeen(unittest.TestCase):
    # 14. best_ask_seen NUMERIQUE des qu'un ask est parsable, meme si
    #     aucun marche n'est eligible
    def test_numeric_when_any_parseable_ask(self):
        cli = SeriesClient({"KXBTCD": [
            mkt("KXBTCD-A", yes_ask=45, yes_bid=41),        # ask_too_high
            mkt("KXBTCD-B", yes_ask_dollars="0.6100",
                yes_bid_dollars="0.5800"),                  # ask_too_high
            mkt("KXBTCD-C", yes_bid=20)]})                  # missing_ask
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertIsNone(cand)
        self.assertEqual(funnel["eligible"], 0)
        self.assertEqual(funnel["best_ask_seen"], 45)       # numerique !
        self.assertEqual(funnel["ask_available"], 2)
        self.assertEqual(funnel["rejections"],
                         {"ask_too_high": 2, "missing_ask": 1})

    # 15. n/a UNIQUEMENT quand AUCUN ask parsable n'existe
    def test_na_only_when_no_parseable_ask(self):
        cli = SeriesClient({"KXBTCD": [
            mkt("KXBTCD-A", yes_ask=0, yes_bid=0),
            mkt("KXBTCD-B", yes_ask_dollars="1.0000"),
            mkt("KXBTCD-C")]})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertIsNone(cand)
        self.assertIsNone(funnel["best_ask_seen"])
        self.assertEqual(funnel["ask_available"], 0)
        self.assertEqual(funnel["rejections"], {"missing_ask": 3})


class TestFunnelCounts(unittest.TestCase):
    """Entonnoir complet sur une fixture mixte — les comptes livres dans
    le rapport d'audit sont EXACTEMENT ceux-ci."""

    def test_funnel_on_mixed_fixture(self):
        cli = SeriesClient({"KXBTCD": [
            mkt("KXBTCD-E1", yes_ask=24, yes_bid=21),        # ELIGIBLE
            mkt("KXBTCD-E2", yes_ask_dollars="0.2900",
                yes_bid_dollars="0.2600"),                   # ELIGIBLE ($)
            mkt("KXBTCD-H1", yes_ask=45, yes_bid=41),        # ask_too_high
            mkt("KXBTCD-W1", yes_ask=25, yes_bid=15),        # spread_too_wide
            mkt("KXBTCD-M1", yes_bid=20),                    # missing_ask
            mkt("KXBTCD-M2", yes_ask=25),                    # missing_bid
            mkt("KXBTCD-C1", status="closed", yes_ask=25,
                yes_bid=22),                                 # not_open
            mkt("KXBTCD-G1", yes_ask="garbage", yes_bid=20)]})  # invalid
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertEqual(cand, ("KXBTCD-E1", 24))
        self.assertEqual(funnel["markets_total"], 8)
        self.assertEqual(funnel["open"], 7)
        self.assertEqual(funnel["ask_available"], 5)   # E1,E2,H1,W1,M2
        self.assertEqual(funnel["quote_available"], 4)  # E1,E2,H1,W1
        self.assertEqual(funnel["spread_available"], 4)
        self.assertEqual(funnel["ask_pass"], 3)         # E1,E2,W1
        self.assertEqual(funnel["spread_pass"], 3)      # E1,E2,H1
        self.assertEqual(funnel["eligible"], 2)
        self.assertEqual(funnel["best_ask_seen"], 24)
        self.assertEqual(funnel["rejections"],
                         {"ask_too_high": 1, "spread_too_wide": 1,
                          "missing_ask": 1, "missing_bid": 1,
                          "not_open": 1, "invalid_price": 1})
        line = kdc._funnel_line(funnel)
        for frag in ("markets_total=8", "quote_available=4",
                     "eligible=2", "missing_ask:1", "not_open:1"):
            self.assertIn(frag, line, frag)

    def test_sample_is_sanitized_and_bounded(self):
        cli = SeriesClient({"KXBTCD": [
            mkt(f"KXBTCD-S{i}", yes_ask=40 + i, yes_bid=38 + i,
                event_ticker=f"KXBTCD-EVT{i}") for i in range(10)]})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertEqual(len(funnel["sample"]), 3)          # borne 1-3
        s = funnel["sample"][0]
        self.assertEqual(s["ticker"], "KXBTCD-S0")
        self.assertEqual(s["parsed_ask"], 40)
        self.assertEqual(s["parsed_spread"], 2)
        self.assertEqual(s["reason"], "ask_too_high")
        # uniquement les champs de cotation/identite declares
        allowed = {"ticker", "event", "series", "status", "yes_bid",
                   "yes_ask", "no_bid", "no_ask", "yes_ask_dollars",
                   "parsed_ask", "parsed_bid", "parsed_spread", "reason"}
        self.assertTrue(set(s) <= allowed, set(s) - allowed)


class TestProductionRegression(unittest.TestCase):
    """Reproduction du defaut observe : 202 marches, tous en formats que
    l'ancien int(m['yes_ask']) ne lisait pas -> n/a. Le nouveau chemin
    DOIT voir les prix (best_ask_seen numerique + entonnoir renseigne)."""

    def test_dollars_only_universe_is_visible(self):
        universe = [mkt(f"KXBTCD-27AUG-T{i}",
                        yes_ask_dollars=f"0.{40 + i}00",
                        yes_bid_dollars=f"0.{37 + i}00")
                    for i in range(10)]
        cli = SeriesClient({"KXBTCD": universe})
        cand, funnel = kdc.find_eligible_market(cli)
        self.assertIsNone(cand)                          # rien <= 30c : OK
        self.assertEqual(funnel["quote_available"], 10)  # mais TOUT est vu
        self.assertEqual(funnel["best_ask_seen"], 40)
        self.assertEqual(funnel["rejections"], {"ask_too_high": 10})


if __name__ == "__main__":
    unittest.main()
