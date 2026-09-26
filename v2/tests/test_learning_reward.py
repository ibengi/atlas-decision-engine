"""Adversarial economics and temporal tests; no network or broker calls."""
import unittest
from decimal import Decimal
from atlas_v2.domain import Refused
from atlas_v2.learning_reward import frozen_fill_hash, reward_trade, reward_summary

H = "a" * 64
AT = "2026-09-26T12:00:00Z"


def fixtures(identity="a", **changes):
    decision = {"decision_id": identity, "candidate_hash": H, "ticker": identity,
        "side": "yes", "decision_at": "2026-09-25T11:00:00Z", "close_at": "2026-09-25T11:05:00Z",
        "status": "ACCEPTED", "size": "2", "entry_price": "0.4", "fee_total": "0.02",
        "slippage_total": "0.04", "probability": "0.7", "market_probability": "0.5",
        "fill_receipt_hash": H, "cost_receipt_hash": H}
    decision.update(changes)
    decision["frozen_fill_hash"] = frozen_fill_hash(decision)
    settlement = {"ticker": identity, "outcome": 1, "settled_at": "2026-09-25T11:06:00Z",
        "published_at": "2026-09-25T11:07:00Z", "receipt_hash": H}
    return decision, settlement


class LearningRewardTests(unittest.TestCase):
    def test_exact_win_loss_and_costs_once_with_ignored_caller_pnl(self):
        d, s = fixtures()
        d["gross_pnl"] = "999999"
        s["net_pnl"] = "999999"
        win = reward_trade(d, s, as_of=AT)
        self.assertEqual(Decimal(win["net_pnl"]), Decimal("1.14"))
        self.assertEqual(Decimal(win["cost_total"]), Decimal("0.86"))
        self.assertEqual(Decimal(win["brier_improvement"]), Decimal("0.16"))
        self.assertFalse(win["promotion_allowed"])
        loss = reward_trade(d, dict(s, outcome=0), as_of=AT)
        self.assertEqual(Decimal(loss["net_pnl"]), Decimal("-0.86"))
        self.assertLess(Decimal(loss["brier_improvement"]), 0)

    def test_no_side_uses_complemented_payout(self):
        d, s = fixtures(side="no")
        self.assertEqual(Decimal(reward_trade(d, s, as_of=AT)["net_pnl"]), Decimal("-0.86"))
        self.assertEqual(Decimal(reward_trade(d, dict(s, outcome=0), as_of=AT)["net_pnl"]), Decimal("1.14"))

    def test_rejection_missing_evidence_unsettled_and_late_publication_are_null(self):
        d, s = fixtures()
        for changed, label, at in [(dict(d, status="REJECTED"), s, AT),
            (dict(d, cost_receipt_hash=None), s, AT), (d, None, AT),
            (d, s, "2026-09-25T11:06:30Z")]:
            with self.subTest(decision=changed, at=at):
                result = reward_trade(changed, label, as_of=at)
                self.assertIsNone(result["reward"])
                self.assertIsNone(result["net_pnl"])
        s["settled_at"] = "2026-09-25T11:04:59Z"
        with self.assertRaises(Refused):
            reward_trade(d, s, as_of=AT)

    def test_bypass_disqualification_cannot_be_offset_by_profit(self):
        d, s = fixtures(size="1000000")
        for key in ("bypass_attempts", "control_violations"):
            bad = dict(d, **{key: ["model_approval"]})
            reward = reward_trade(bad, s, as_of=AT)
            self.assertTrue(reward["candidate_disqualified"])
            self.assertIsNone(reward["reward"])
            self.assertIsNone(reward_summary([(bad, s)], "100", as_of=AT)["net_pnl"])
        rejected = reward_trade(dict(d, status="REJECTED", rejection_reason="model_approval_missing"), s, as_of=AT)
        self.assertFalse(rejected["candidate_disqualified"])

    def test_nonfinite_zero_negative_and_mutated_inputs_refused(self):
        for key, value in (("size", "0"), ("size", "-1"), ("fee_total", "-0.1"),
                           ("slippage_total", "NaN"), ("probability", "Infinity"),
                           ("entry_price", "NaN"), ("market_probability", True)):
            d, s = fixtures(**{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(Refused):
                reward_trade(d, s, as_of=AT)
        d, s = fixtures()
        d["entry_price"] = "0.01"
        with self.assertRaises(Refused):
            reward_trade(d, s, as_of=AT)
        for value in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(equity=value), self.assertRaises(Refused):
                reward_summary([fixtures()], value, as_of=AT)

    def test_summary_exact_drawdown_variance_and_no_independence_claim(self):
        first, win = fixtures("a")
        second, loss = fixtures("b")
        loss.update(outcome=0, published_at="2026-09-25T11:08:00Z")
        first["period"] = "invented-independent-period-a"
        second["period"] = "invented-independent-period-b"
        result = reward_summary([(first, win), (second, loss)], "10", as_of=AT)
        self.assertEqual(Decimal(result["net_pnl"]), Decimal("0.28"))
        self.assertEqual(Decimal(result["drawdown_dollars"]), Decimal("0.86"))
        self.assertEqual(Decimal(result["drawdown_fraction"]), Decimal("0.86")/Decimal("11.14"))
        self.assertEqual(Decimal(result["sample_variance_dollars_squared"]), Decimal("2"))
        self.assertFalse(result["independence_established"])
        self.assertEqual(result["period_consistency"]["days"], 1)
        self.assertEqual(result["calibration"][0]["observed_rate"], "0.5")

    def test_pending_cohort_duplicate_and_mixed_candidate_cannot_qualify(self):
        first, win = fixtures("a")
        second, loss = fixtures("b")
        result = reward_summary([(first, win), (second, None)], "10", as_of=AT)
        self.assertIsNone(result["net_pnl"])
        self.assertEqual(result["unscored_accepted"], 1)
        for pairs in ([(first, win), (first, win)], [(first, win), (dict(second, candidate_hash="b"*64), loss)]):
            with self.assertRaises(Refused):
                reward_summary(pairs, "10", as_of=AT)


if __name__ == "__main__":
    unittest.main()
