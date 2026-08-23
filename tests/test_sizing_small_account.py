# -*- coding: utf-8 -*-
"""Exigence D : sizing et limites pour un compte de 93,26 $.
- effective_capital = min(solde, capital configure) exclusivement ;
- risque/trade <= 1 %, exposition totale <= 5 %, stop jour <= 5 % ;
- 3 positions max ; arret apres 3 pertes consecutives ;
- AUCUNE limite fondee sur 500 $ quand le solde est inferieur."""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="kalshi_risk_")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("KALSHI_DEMO_KEY_ID", "test")
os.environ.setdefault("KALSHI_DEMO_PRIVATE_KEY", "test")
import _bootstrap  # noqa: F401  (ajoute src/* au sys.path)

import kalshi_alpha_bot as bot                       # noqa: E402

BAL = 93.26


class _T:
    """TradeLogger factice minimal."""
    def __init__(self, settled=None):
        self._s = settled or []
        self.trades = list(self._s)

    def settled_trades(self):
        return self._s

    def open_trades(self):
        return []


class _P:
    def __init__(self, risk=0.0):
        self._r = risk

    def open_risk(self):
        return self._r

    def unrealized_pnl(self, *a, **k):
        return 0.0


def _rm(settled=None, open_risk=0.0, capital=BAL):
    rm = bot.RiskManager.__new__(bot.RiskManager)
    rm.tlog = _T(settled)
    rm.posmgr = _P(open_risk)
    rm.capital = capital
    # AIR-001 W1 (stale fixture): __new__ bypasses __init__, which
    # always sets the half-open breaker state — restore the invariant.
    rm.state = {}
    return rm


def _loss(n):
    # AIR-001 W1 (stale fixture): a fixed 10:00Z settled_at made the
    # consecutive-loss halt time-of-day dependent (cooldown elapsed
    # after 11:00 UTC enters the half-open branch). A current timestamp
    # keeps the cooldown active — the asserted halt is deterministic.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return {"net_pnl": -0.5 * n, "won": False, "gross_pnl": -0.5 * n,
            "fees": 0.05, "settled_at": now, "timestamp": now}


class TestEffectiveCapital(unittest.TestCase):
    def test_min_of_balance_and_configured(self):
        class C:
            env = "demo"
            def get_balance(self):
                return BAL
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.client = C()
        eng.configured_capital = 500.0
        eng.capital = 500.0
        eng.risk = _rm()
        ok, why = eng._balance_gate()
        self.assertTrue(ok)
        self.assertAlmostEqual(eng.capital, BAL)      # jamais 500
        self.assertAlmostEqual(eng.risk.capital, BAL)

    def test_500_never_wins_over_lower_balance(self):
        class C:
            env = "demo"
            def get_balance(self):
                return 10.0
        eng = bot.ExecutionEngine.__new__(bot.ExecutionEngine)
        eng.client = C()
        eng.configured_capital = 500.0
        eng.capital = 500.0
        eng.risk = _rm()
        eng._balance_gate()
        self.assertAlmostEqual(eng.capital, 10.0)


class TestDailyStop(unittest.TestCase):
    def test_stop_is_5pct_of_effective_capital(self):
        rm = _rm(capital=BAL)
        self.assertAlmostEqual(rm.effective_daily_stop(), 4.66, places=2)
        # et JAMAIS les -50$ de l'ancienne config (54 % du compte)
        self.assertLess(rm.effective_daily_stop(), 50.0)

    def test_stop_blocks_trading(self):
        rm = _rm(settled=[_loss(10)], capital=BAL)   # -5$ realises
        ok, why = rm.can_trade(0)
        self.assertFalse(ok)
        self.assertIn("STOP JOURNALIER", why)


class TestConsecutiveLosses(unittest.TestCase):
    def test_halt_after_three(self):
        rm = _rm(settled=[_loss(1), _loss(1), _loss(1)], capital=BAL)
        ok, why = rm.can_trade(0)
        self.assertFalse(ok)
        self.assertIn("pertes consecutives", why)

    def test_two_losses_still_trades(self):
        rm = _rm(settled=[_loss(1), _loss(1)], capital=BAL)
        ok, _ = rm.can_trade(0)
        self.assertTrue(ok)

    def test_win_resets_streak(self):
        from datetime import date
        today = date.today().isoformat()
        win = {"net_pnl": 0.4, "won": True, "gross_pnl": 0.5, "fees": 0.1,
               "settled_at": today + "T10:00:00+00:00",
               "timestamp": today + "T09:00:00+00:00"}
        rm = _rm(settled=[_loss(1), _loss(1), _loss(1), win], capital=BAL)
        self.assertEqual(rm.consecutive_losses(), 0)


class TestSizer(unittest.TestCase):
    def test_per_trade_max_1pct(self):
        for price in (10, 30, 50, 80):
            n = bot.PositionSizer.contracts(BAL, price, "2%", 9, 0.0, 0.0)
            self.assertLessEqual(n * price / 100.0, BAL * 0.01 + 1e-9,
                                 f"prix {price}c : {n} contrats > 1%")

    def test_zero_when_unaffordable(self):
        # 1 % de 93,26$ = 0,93$ : un contrat a 95c est inabordable ->
        # 0 contrat, honnetement (on ne force pas le trade).
        self.assertEqual(bot.PositionSizer.contracts(BAL, 95, "1%", 9,
                                                     0.0, 0.0), 0)

    def test_total_exposure_capped_5pct(self):
        budget = BAL * 0.05
        n = bot.PositionSizer.contracts(BAL, 30, "1%", 9, 0.0,
                                        open_risk=budget)   # budget epuise
        self.assertEqual(n, 0)

    def test_max_open_positions_is_3(self):
        self.assertEqual(bot.CFG.MAX_OPEN_POSITIONS, 3)


if __name__ == "__main__":
    unittest.main()
