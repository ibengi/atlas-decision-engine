import unittest
from unittest.mock import patch

from position_sizer import PositionSizer


class TestKellyMath(unittest.TestCase):
    def test_full_kelly_yes(self):
        # p=.60, price=.50 => f=.20
        self.assertAlmostEqual(PositionSizer.full_kelly(.60, 50), .20)

    def test_no_uses_complement_probability(self):
        # Buy NO at 40c: p(NO)=.70, f=(.7*.? - .3)/b = .5
        self.assertAlmostEqual(PositionSizer.full_kelly(.30, 40, "no"), .50)

    def test_degenerate_prices_and_zero_edge(self):
        self.assertEqual(PositionSizer.full_kelly(.5, 0), 0)
        self.assertEqual(PositionSizer.full_kelly(.5, 100), 0)
        self.assertEqual(PositionSizer.full_kelly(.5, 50), 0)

    def test_half_kelly_and_cap(self):
        with patch.object(PositionSizer, "_legacy", return_value=-1):
            with patch("position_sizer.CFG.KELLY_ENABLED", True), \
                 patch("position_sizer.CFG.MAX_POS_PCT", 10.0), \
                 patch("position_sizer.CFG.KELLY_MAX_POS_PCT", 10.0), \
                 patch("position_sizer.CFG.KELLY_FRACTION", .5):
                n = PositionSizer.contracts(1000, 50, "1%", 9, 0, 0,
                                            probability=.9)
        self.assertEqual(n * .5, 50)  # 100 contracts at 50c = $50? half Kelly

    def test_floor_enforcement(self):
        with patch("position_sizer.CFG.KELLY_ENABLED", True), \
             patch("position_sizer.CFG.MAX_POS_PCT", 10.0), \
             patch("position_sizer.CFG.KELLY_MIN_BET", 1.0):
            self.assertEqual(PositionSizer.contracts(
                100, 50, "1%", 9, 0, 0, probability=.51), 2)


if __name__ == "__main__":
    unittest.main()
