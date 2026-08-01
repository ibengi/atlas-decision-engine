import unittest
from unittest.mock import Mock, patch

from risk_manager import RiskManager


class PortfolioLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tlog = Mock()
        self.tlog.settled_trades.return_value = []
        self.tlog.trades = []
        self.pos = Mock()
        self.pos.open_risk.return_value = 0.0
        self.pos.open_risk_by_group.return_value = {"BTC": 100.0}
        self.risk = RiskManager(self.tlog, self.pos, 1000.0)

    def test_group_limit_blocks_increment(self):
        with patch.object(self.risk, "_group_limit_pct", return_value=15.0):
            ok, reason = self.risk.portfolio_check("KXBTC15M", "Crypto", 60.0)
        self.assertFalse(ok)
        self.assertIn("BTC", reason)

    def test_disabled_limits_pass(self):
        with patch("risk_manager.CFG.MAX_PORTFOLIO_RISK_PCT", 0.0), \
             patch("risk_manager.CFG.MAX_CORRELATION_GROUP_PCT", 0.0):
            self.pos.open_risk.return_value = 99999.0
            ok, _ = self.risk.portfolio_check("X", "Other", 1.0)
        self.assertTrue(ok)

    def test_drawdown_factor(self):
        self.tlog.settled_trades.return_value = [
            {"net_pnl": 100.0, "settled_at": "2026-01-01T00:00:00+00:00"},
            {"net_pnl": -60.0, "settled_at": "2026-01-01T00:00:00+00:00"},
        ]
        with patch("risk_manager.CFG.PORTFOLIO_DRAWDOWN_THROTTLE_PCT", 5.0), \
             patch("risk_manager.CFG.DRAWDOWN_THROTTLE_FACTOR", 0.5):
            self.assertEqual(self.risk.drawdown_size_factor(), 0.5)


if __name__ == "__main__":
    unittest.main()
