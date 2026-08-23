# -*- coding: utf-8 -*-
"""AIR-001 Wave 6 (DE-P0-008) — unified projected RiskProof validator.

Reproduced defects pinned here:
- aggregate risk controls default to 0 = disabled and nothing surfaced
  that fact (now: DISABLED_BY_DEFAULT refuses LIVE approval);
- single-market/category checks compared only CURRENT exposure — the
  proposed order itself was not projected;
- KELLY_MIN_BET silently RAISED a capped allocation up to the minimum,
  overriding the percentage maxima;
- unknown financial state (balance, broker schema, unresolved intents)
  did not block orders.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

from position_sizer import PositionSizer  # noqa: E402
from risk_proof import (DISABLED_BY_DEFAULT, FAIL,  # noqa: E402
                        UNKNOWN_FAIL_CLOSED, build_risk_proof,
                        persist_proof)


class StubRisk:
    def __init__(self, pnl=0.0, stop=5.0, dd_pct=0.0, losses=0,
                 since=None):
        self._pnl, self._stop = pnl, stop
        self._dd, self._losses, self._since = dd_pct, losses, since

    @staticmethod
    def _group_for(ticker, category="Other"):
        return "Crypto"

    def daily_realized_pnl(self):
        return self._pnl

    def effective_daily_stop(self):
        return self._stop

    def rolling_drawdown_pct(self):
        return self._dd

    def consecutive_losses(self):
        return self._losses

    def seconds_since_last_settlement(self):
        return self._since


class StubPos:
    exchange_schema_incompatible = False

    def __init__(self, on_ticker=0.0, by_cat=None, by_group=None,
                 total=0.0, count=0):
        self._on, self._cat = on_ticker, by_cat or {}
        self._grp, self._total, self._count = by_group or {}, total, count

    def open_risk_on(self, ticker):
        return self._on

    def open_risk_by_category(self):
        return dict(self._cat)

    def open_risk_by_group(self):
        return dict(self._grp)

    def open_risk(self):
        return self._total

    def open_count(self):
        return self._count


def proof(*, risk=None, posmgr=None, count=1, entry=40, capital=100.0,
          balance_known=True, blocked=False, now=1000.0):
    return build_risk_proof(
        ticker="KXT-1", category="Crypto", side="yes", count=count,
        entry_cents=entry, risk=risk or StubRisk(),
        posmgr=posmgr or StubPos(), capital=capital,
        balance_known=balance_known,
        orders_blocked_reconciling=blocked, now=now)


ENABLED_AGGREGATES = {
    "MAX_CORRELATION_GROUP_PCT": 10.0,
    "MAX_PORTFOLIO_RISK_PCT": 10.0,
    "PORTFOLIO_DRAWDOWN_THROTTLE_PCT": 10.0,
}


def _patch_cfg(**kw):
    import config
    patches = [patch.object(config.CFG, k, v) for k, v in kw.items()]
    return patches


class TestDefaultDisabledAggregates(unittest.TestCase):
    def test_default_config_refuses_live_approval(self):
        """The finding itself: with stock configuration the aggregate
        controls are OFF (config defaults are 0). That must now be
        visible and blocking. The stock defaults are pinned explicitly
        because other suites mutate the CFG singleton in-process."""
        ps = _patch_cfg(MAX_CORRELATION_GROUP_PCT=0.0,
                        MAX_PORTFOLIO_RISK_PCT=0.0,
                        PORTFOLIO_DRAWDOWN_THROTTLE_PCT=0.0)
        for p_ in ps:
            p_.start()
        try:
            self.assertEqual(
                (0.0, 0.0, 0.0),
                (float(os.environ.get("MAX_PORTFOLIO_RISK_PCT", 0) or 0),
                 float(os.environ.get("MAX_CORRELATION_GROUP_PCT", 0)
                       or 0),
                 float(os.environ.get("PORTFOLIO_DRAWDOWN_THROTTLE_PCT",
                                      0) or 0)),
                "stock env must leave the aggregates disabled")
            p = proof()
        finally:
            for p_ in ps:
                p_.stop()
        self.assertFalse(p.approved)
        by_name = {c.name: c for c in p.checks}
        for name in ("projected_correlation_group",
                     "projected_portfolio_total", "drawdown_throttle"):
            self.assertEqual(by_name[name].status, DISABLED_BY_DEFAULT,
                             name)
            self.assertIn(name, p.failing)
        # Nothing else fails in a healthy default state.
        self.assertEqual(set(p.failing),
                         {"projected_correlation_group",
                          "projected_portfolio_total",
                          "drawdown_throttle"})

    def test_operator_enabled_aggregates_approve(self):
        ps = _patch_cfg(**ENABLED_AGGREGATES)
        for p_ in ps:
            p_.start()
        try:
            p = proof()
        finally:
            for p_ in ps:
                p_.stop()
        self.assertTrue(p.approved, p.failing)
        self.assertEqual(p.failing, ())

    def test_content_hash_deterministic_and_input_sensitive(self):
        a, b = proof(now=1000.0), proof(now=1000.0)
        self.assertEqual(a.content_hash(), b.content_hash())
        c = proof(count=2, now=1000.0)
        self.assertNotEqual(a.content_hash(), c.content_hash())


class TestFailClosedUnknowns(unittest.TestCase):
    def _failing_check(self, p, name):
        return {c.name: c for c in p.checks}[name]

    def test_schema_incompatible_blocks(self):
        pos = StubPos()
        pos.exchange_schema_incompatible = True
        p = proof(posmgr=pos)
        self.assertFalse(p.approved)
        self.assertEqual(
            self._failing_check(p, "exchange_schema_compatible").status,
            UNKNOWN_FAIL_CLOSED)

    def test_unknown_balance_blocks(self):
        p = proof(balance_known=False)
        self.assertEqual(self._failing_check(p, "balance_known").status,
                         UNKNOWN_FAIL_CLOSED)
        self.assertFalse(p.approved)

    def test_unresolved_intents_block(self):
        p = proof(blocked=True)
        self.assertEqual(
            self._failing_check(p, "order_intents_resolved").status,
            UNKNOWN_FAIL_CLOSED)
        self.assertFalse(p.approved)


class TestProjectedLimits(unittest.TestCase):
    def test_projection_catches_what_current_only_check_missed(self):
        """capital=100, single-market cap 1% = $1. Current exposure
        $0.90 passed the OLD check (0.90 < 1.00); the proposed $0.80
        pushes the PROJECTED exposure to $1.70 — refused now."""
        p = proof(posmgr=StubPos(on_ticker=0.90), count=2, entry=40)
        by_name = {c.name: c for c in p.checks}
        self.assertEqual(by_name["projected_single_market"].status, FAIL)

    def test_category_projection(self):
        p = proof(posmgr=StubPos(by_cat={"Crypto": 2.8}), count=2,
                  entry=40)  # cap 3% of 100 = $3.00; 2.8+0.8=3.6
        by_name = {c.name: c for c in p.checks}
        self.assertEqual(by_name["projected_category"].status, FAIL)

    def test_per_order_cap(self):
        # MAX_POS_PCT=1% of 100 = $1; 3 x 40c = $1.20
        p = proof(count=3, entry=40)
        by_name = {c.name: c for c in p.checks}
        self.assertEqual(by_name["max_loss_per_order"].status, FAIL)

    def test_daily_stop_and_consecutive_losses(self):
        p = proof(risk=StubRisk(pnl=-6.0, stop=5.0))
        self.assertIn("daily_loss_stop", p.failing)
        p2 = proof(risk=StubRisk(losses=3, since=10.0))
        self.assertIn("consecutive_loss_circuit", p2.failing)

    def test_kill_switch(self):
        import config
        with patch.object(config.CFG, "KILL_SWITCH", True):
            p = proof()
        self.assertIn("kill_switch", p.failing)


class TestRiskConfigBinding(unittest.TestCase):
    def test_proof_binds_live_risk_config_hash(self):
        from config_identity import risk_config_hash
        p = proof()
        self.assertEqual(p.risk_config_hash, risk_config_hash())
        import config
        with patch.object(config.CFG, "MAX_POS_PCT", 2.5):
            p2 = proof()
        self.assertNotEqual(p.risk_config_hash, p2.risk_config_hash)

    def test_persist_proof_durable_with_hash(self):
        target = os.path.join(tempfile.mkdtemp(prefix="rp_"),
                              "risk_proofs.jsonl")
        p = proof()
        persist_proof(p, path=target)
        with open(target, encoding="utf-8") as fh:
            row = json.loads(fh.readline())
        self.assertEqual(row["content_hash"], p.content_hash())
        self.assertEqual(row["ticker"], "KXT-1")


class TestKellyMinBetNoOverride(unittest.TestCase):
    def test_min_bet_never_raises_a_capped_allocation(self):
        """BEFORE: capital=100, MAX_POS_PCT=0.5% caps the allocation at
        $0.50; the old floor lifted it to KELLY_MIN_BET=$1.00 — i.e.
        1% of capital, silently overriding the configured maximum.
        NOW: below minimum tradable size => NO TRADE (0 contracts)."""
        import config
        with patch.object(config.CFG, "KELLY_ENABLED", True), \
             patch.object(config.CFG, "MAX_POS_PCT", 0.5), \
             patch.object(config.CFG, "KELLY_MIN_BET", 1.0):
            n = PositionSizer.contracts(100, 50, "1%", 9, 0, 0,
                                        probability=0.9)
        self.assertEqual(n, 0)

    def test_allocation_at_or_above_minimum_unchanged(self):
        import config
        with patch.object(config.CFG, "KELLY_ENABLED", True), \
             patch.object(config.CFG, "MAX_POS_PCT", 10.0), \
             patch.object(config.CFG, "KELLY_MIN_BET", 1.0):
            n = PositionSizer.contracts(100, 50, "1%", 9, 0, 0,
                                        probability=0.51)
        self.assertEqual(n, 2)     # $1.00 = the minimum, taken as-is


if __name__ == "__main__":
    unittest.main()
