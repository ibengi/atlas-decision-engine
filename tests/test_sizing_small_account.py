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


def _rm(settled=None, open_risk=0.0, capital=BAL, state=None):
    rm = bot.RiskManager.__new__(bot.RiskManager)
    rm.tlog = _T(settled)
    rm.posmgr = _P(open_risk)
    rm.capital = capital
    # __new__ contourne __init__ : self.state doit etre fourni ici, comme
    # __init__ le garantit toujours en production (l'oubli faisait planter
    # can_trade() des que la branche post-cooldown etait atteinte, donc
    # cette branche n'etait jamais testee).
    rm.state = dict(state or {})
    rm.flush = lambda: None
    return rm


def _loss(n, minutes_ago=5.0):
    # Horodatage RELATIF a maintenant : l'ancien "T10:00:00" fixe rendait
    # ces tests dependants de l'heure d'execution (cooldown des pertes
    # consecutives ecoule ou non selon l'heure UTC du run).
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc)
          - timedelta(minutes=minutes_ago)).isoformat()
    today = ts[:10]
    return {"net_pnl": -0.5 * n, "won": False, "gross_pnl": -0.5 * n,
            "fees": 0.05, "settled_at": ts,
            "timestamp": today + "T09:00:00+00:00"}


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



class TestPostCooldownHalfOpen(unittest.TestCase):
    """Branche post-cooldown du circuit-breaker demi-ouvert (jamais testee
    auparavant : le double _rm plantait avant de l'atteindre)."""

    def _old_losses(self):
        # 3 pertes reglees il y a longtemps : cooldown certainement ecoule.
        hours = bot.CFG.CONSECUTIVE_LOSS_COOLDOWN_S / 3600.0 + 2.0
        return [_loss(1, minutes_ago=hours * 60 + i) for i in (2, 1, 0)]

    def test_cooldown_elapsed_grants_one_retry(self):
        rm = _rm(settled=self._old_losses(), capital=BAL, state={})
        ok, why = rm.can_trade(0)
        self.assertTrue(ok, f"un essai doit etre autorise apres cooldown: {why}")

    def test_claimed_half_open_blocks_until_settlement(self):
        settled = self._old_losses()
        anchor = settled[-1]["settled_at"]
        rm = _rm(settled=settled, capital=BAL,
                 state={"half_open_anchor": anchor,
                        "half_open_claimed": True})
        ok, why = rm.can_trade(0)
        self.assertFalse(ok)
        self.assertIn("demi-ouvert", why)

    def test_stale_claim_from_older_settlement_does_not_block(self):
        settled = self._old_losses()
        rm = _rm(settled=settled, capital=BAL,
                 state={"half_open_anchor": "2020-01-01T00:00:00+00:00",
                        "half_open_claimed": True})
        ok, why = rm.can_trade(0)
        self.assertTrue(ok, why)



if __name__ == "__main__":
    unittest.main()
