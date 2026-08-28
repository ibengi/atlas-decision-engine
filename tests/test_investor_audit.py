# -*- coding: utf-8 -*-
"""ATLAS-TOTAL-AUDIT-001 — reglement des positions reconstruites +
registre investisseur + instantane T0.

Defaut CRITIQUE mesure (Domain 2) : les positions brk-* reconstruites du
broker n'avaient AUCUNE ligne de registre -> settle_trade 'introuvable'
en boucle infinie, position bloquant MAX_OPEN_POSITIONS indefiniment,
et — pire — apres retrait de la ligne broker, suppression fantome SANS
trace comptable (cash broker invisible du registre).

Pinne : adoption idempotente a provenance explicite, reglement reussi
au premier passage du resultat lisible, PnL exact (accounting Decimal),
orphelin non-brk toujours bruyant ; classes de provenance A/F/G,
politique IP-1 (seule A comptee), double comptage interdit, gardes
d'echantillon (INSUFFICIENT/LOW), Sharpe omis sous n=30 ; T0 lecture
seule avec hachages de config et refus LIVE. Aucun reseau, aucun ordre.
"""
import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import contextlib
import io
import json
import tempfile

import kalshi_alpha_bot as bot
import accounting
import investor_report as ir


class SettleRig:
    """PositionManager reel + TradeLogger reel + broker factice."""

    def __init__(self, broker_rows, market):
        self.tmp = tempfile.mkdtemp(prefix="audit_settle_")
        self._old_dir = bot.CFG.DATA_DIR
        bot.CFG.DATA_DIR = self.tmp
        rows, mkt = broker_rows, market

        class Cli:
            @staticmethod
            def get_positions():
                return rows

            @staticmethod
            def get_market(tk):
                return mkt

        self.tlog = bot.TradeLogger()
        self.pm = bot.PositionManager(Cli(), self.tlog)
        self.pm.positions = {}

    def close(self):
        bot.CFG.DATA_DIR = self._old_dir


class TestSettleReconstructed(unittest.TestCase):
    ROWS = [{"ticker": "KXBTCD-26AUG2717-T82999.99", "position": 11,
             "market_exposure": 33}]

    def test_brk_position_settles_without_infinite_retry(self):
        rig = SettleRig(self.ROWS, {"ticker": self.ROWS[0]["ticker"],
                                    "status": "settled", "result": "yes"})
        try:
            rig.pm.reconcile_with_broker()
            self.assertEqual(rig.pm.open_count(), 1)
            buf = io.StringIO()
            h = __import__("logging").StreamHandler(buf)
            log = __import__("logging").getLogger("TRADE")
            logp = __import__("logging").getLogger("POSITION")
            for lg in (log, logp):
                lg.addHandler(h)
            try:
                realized = rig.pm.check_settlements()
            finally:
                for lg in (log, logp):
                    lg.removeHandler(h)
            t = buf.getvalue()
            self.assertNotIn("introuvable", t)          # PLUS de boucle
            self.assertIn("[LEDGER_ADOPTED]", t)
            self.assertEqual(len(realized), 1)
            self.assertEqual(rig.pm.open_count(), 0)    # garde liberee
            # PnL EXACT via accounting (achat YES 3c x11, resultat yes)
            acct = accounting.settle_yes_no(
                side="yes", result="yes", count=11, avg_price_cents=3,
                fees_dollars=0.0)
            row = realized[0]
            self.assertEqual(row["gross_pnl"], round(acct["gross"], 2))
            self.assertEqual(row["net_pnl"], round(acct["net"], 2))
            self.assertEqual(row["provenance"], "broker_reconstructed")
            self.assertIsNone(row["decision_id"])
            # relance : AUCUNE nouvelle ligne, AUCUN nouveau reglement
            realized2 = rig.pm.check_settlements()
            self.assertEqual(realized2, [])
            self.assertEqual(
                len([x for x in rig.tlog.trades
                     if x["trade_id"].startswith("brk-")]), 1)
        finally:
            rig.close()

    def test_adoption_idempotent(self):
        rig = SettleRig(self.ROWS, {})
        try:
            rig.pm.reconcile_with_broker()
            p = list(rig.pm.positions.values())[0]
            r1 = rig.tlog.adopt_reconstructed(p)
            r2 = rig.tlog.adopt_reconstructed(p)
            self.assertIs(r1, r2)
            self.assertEqual(len(rig.tlog.trades), 1)
        finally:
            rig.close()

    def test_settled_broker_row_absent_locally_still_accounted(self):
        """Le broker a DEJA retire la ligne (reglement traite cote
        broker) : le reglement local passe quand meme par get_market —
        aucune dependance a get_positions, aucune perte comptable."""
        rig = SettleRig([], {"ticker": self.ROWS[0]["ticker"],
                             "status": "settled", "result": "no"})
        try:
            # position reconstruite lors d'un cycle precedent
            rig.pm.positions["brk-KXBTCD-X-yes"] = {
                "trade_id": "brk-KXBTCD-X-yes", "ticker": "KXBTCD-X",
                "side": "yes", "count": 2, "avg_price": 3, "fees": 0.0,
                "count_initial": 2, "state": "open",
                "opened_at": bot.now_iso(), "order_ids": [],
                "fill_ids": [], "strategy": "reconciled",
                "cost_basis_source": "market_exposure"}
            realized = rig.pm.check_settlements()
            self.assertEqual(len(realized), 1)
            self.assertEqual(realized[0]["result"], "no")
            self.assertFalse(realized[0]["won"])
        finally:
            rig.close()

    def test_non_brk_orphan_still_loud_error(self):
        """Un orphelin NON reconstruit reste une erreur bruyante — bug
        distinct, jamais adopte silencieusement."""
        rig = SettleRig([], {"ticker": "KXBTCD-Y", "status": "settled",
                             "result": "yes"})
        try:
            rig.pm.positions["xyz123"] = {
                "trade_id": "xyz123", "ticker": "KXBTCD-Y", "side": "yes",
                "count": 1, "avg_price": 3, "fees": 0.0, "state": "open",
                "opened_at": bot.now_iso()}
            buf = io.StringIO()
            h = __import__("logging").StreamHandler(buf)
            log = __import__("logging").getLogger("TRADE")
            log.addHandler(h)
            try:
                realized = rig.pm.check_settlements()
            finally:
                log.removeHandler(h)
            self.assertEqual(realized, [])
            self.assertIn("introuvable", buf.getvalue())
            self.assertEqual(rig.pm.open_count(), 1)    # conservee
        finally:
            rig.close()


def _trade(i, net, decision_id="d", state="settled", prov=None, **kw):
    t = {"trade_id": f"t{i}", "decision_id": decision_id,
         "state": state, "net_pnl": net,
         "gross_pnl": net if net is None else net + 0.02, "fees": 0.02,
         "ticker": "KXBTCD-X", "side": "yes", "timestamp": "2026-08-28",
         "requested_count": 1, "filled_count": 1, "avg_fill_price": 20,
         "holding_seconds": 600, "order_id": f"o{i}", "won": None,
         "result": "yes", "edge": 0.1, "ev": 0.05, "confidence": 8,
         "analysis": {"strategy": "btc_daily_above_strike"}}
    if prov:
        t["provenance"] = prov
    t.update(kw)
    return t


class TestInvestorLedger(unittest.TestCase):
    def test_provenance_classes_and_policy(self):
        rows = ir.build_ledger([
            _trade(1, 1.0),                                  # A
            _trade(2, 5.0, decision_id=None,
                   prov="broker_reconstructed"),             # F
            _trade(3, 2.0, decision_id=None)])               # G
        classes = [r["provenance_class"] for r in rows]
        self.assertEqual(classes, ["A_STRATEGY", "F_RECONSTRUCTED",
                                   "G_UNKNOWN_PROVENANCE"])
        self.assertEqual([r["investor_included"] for r in rows],
                         [True, False, False])
        m = ir.compute_metrics(rows)
        self.assertEqual(m["settled_trades"], 1)     # F/G jamais comptes
        self.assertEqual(m["excluded_rows"], 2)

    def test_open_trades_never_counted(self):
        rows = ir.build_ledger([_trade(1, None, state="open")])
        self.assertFalse(rows[0]["investor_included"])

    def test_double_counting_refused(self):
        with self.assertRaises(ValueError):
            ir.build_ledger([_trade(1, 1.0), _trade(1, 1.0)])

    def test_small_sample_gates(self):
        rows = ir.build_ledger([_trade(i, 1.0) for i in range(3)])
        m = ir.compute_metrics(rows)
        self.assertEqual(m["sample_grade"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(m["sharpe_like_per_trade"],
                         "OMIS(INSUFFICIENT_SAMPLE)")
        rows = ir.build_ledger([_trade(i, 1.0) for i in range(12)])
        self.assertEqual(ir.compute_metrics(rows)["sample_grade"],
                         "LOW_SAMPLE")

    def test_full_metrics_on_adequate_sample(self):
        nets = [1.0, -0.5] * 20                       # n=40
        rows = ir.build_ledger([_trade(i, x)
                                for i, x in enumerate(nets)])
        m = ir.compute_metrics(rows, starting_equity=100.0)
        self.assertEqual(m["sample_grade"], "OK")
        self.assertEqual(m["settled_trades"], 40)
        self.assertEqual(m["net_pnl"], 10.0)
        self.assertEqual(m["wins"], 20)
        self.assertEqual(m["losses"], 20)
        self.assertEqual(m["win_rate"], 0.5)
        self.assertEqual(m["profit_factor"], 2.0)
        self.assertEqual(m["expectancy_per_trade"], 0.25)
        self.assertEqual(m["largest_win"], 1.0)
        self.assertEqual(m["largest_loss"], -0.5)
        self.assertEqual(m["starting_equity"], 100.0)
        self.assertEqual(m["ending_equity"], 110.0)
        self.assertEqual(m["max_drawdown"], 0.5)      # sequence connue
        self.assertEqual(m["return_pct"], 10.0)
        self.assertIsInstance(m["sharpe_like_per_trade"], float)
        self.assertIn("btc_daily_above_strike", m["by_strategy"])
        self.assertIn("BTC_DAILY", m["by_market_family"])

    def test_report_reproducible_and_labeled(self):
        trades = [_trade(i, 1.0) for i in range(5)]
        r1 = ir.render_report(trades, starting_equity=100.0, sha="abc123")
        r2 = ir.render_report(trades, starting_equity=100.0, sha="abc123")
        for k in ("inclusion_policy", "provenance_counts", "performance",
                  "ledger"):
            self.assertEqual(r1[k], r2[k])            # reproductible
        self.assertEqual(r1["build_sha"], "abc123")
        self.assertIn("MEASURED", r1["evidence_labels"]["ledger_rows"])
        self.assertIn("known_limitations", r1)


class TestT0Snapshot(unittest.TestCase):
    class Cli:
        base_url = "https://demo-api.kalshi.co/trade-api/v2"

        @staticmethod
        def get_balance():
            return 131.91

        @staticmethod
        def get_subaccounts_balances():
            return [{"subaccount": 0, "exchange_index": 0,
                     "balance_dollars": "130.91"},
                    {"subaccount": 0, "exchange_index": 2,
                     "balance_dollars": "1.00"}]

        @staticmethod
        def get_positions():
            return []

        @staticmethod
        def get_orders(ticker=None):
            return []

    def test_snapshot_contents_and_hash(self):
        import investor_t0_snapshot as t0
        snap = t0.build_snapshot(self.Cli())
        for k in ("t0_utc", "build_sha", "strategy_config_hash",
                  "risk_config_hash", "risk_limits", "inclusion_policy",
                  "starting_balance", "balances_by_exchange_index",
                  "open_positions", "open_orders", "package_sha256"):
            self.assertIn(k, snap)
        self.assertFalse(snap["clock_started"])
        self.assertEqual(snap["risk_limits"]["max_open_positions"], 3)
        self.assertEqual(len(snap["package_sha256"]), 64)

    def test_t0_refuses_live_and_is_read_only(self):
        import investor_t0_snapshot as t0
        os.environ["LIVE_TRADING"] = "1"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    t0.main()
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.environ.pop("LIVE_TRADING", None)
        src = open(t0.__file__, encoding="utf-8").read()
        for verb in ('"POST"', '"PUT"', '"DELETE"', "create_order",
                     "cancel_order", "intra_exchange_instance_transfer"):
            self.assertNotIn(verb, src, verb)


class TestRiskLimitsFrozen(unittest.TestCase):
    """Domain 8 : les limites de l'enveloppe ne derivent pas."""

    def test_envelope_defaults(self):
        self.assertEqual(bot.CFG.MAX_OPEN_POSITIONS, 3)
        self.assertEqual(bot.CFG.MAX_DAILY_LOSS_PCT, 5.0)
        self.assertEqual(bot.CFG.MAX_POS_PCT, 1.0)
        self.assertEqual(bot.CFG.RISK_BUDGET_PCT, 5.0)
        self.assertEqual(bot.CFG.MAX_CONSECUTIVE_LOSSES, 3)
        from config_identity import risk_config_hash
        self.assertEqual(len(risk_config_hash()), 64)   # liaison contenu


if __name__ == "__main__":
    unittest.main()
