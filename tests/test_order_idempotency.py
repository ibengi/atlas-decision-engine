"""Tests for deterministic Kalshi client order IDs (P2.6)."""
import hashlib
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402

import kalshi_alpha_bot as bot  # noqa: E402


class TestOrderIdempotency(unittest.TestCase):
    def test_same_params_produce_same_client_order_id(self):
        """A retry with identical order parameters reuses its client ID."""
        client = MagicMock()
        manager = bot.OrderManager(client)

        first = manager._client_order_id("KXTEST-TICKER", "yes", 3, 42)
        second = manager._client_order_id("KXTEST-TICKER", "yes", 3, 42)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            "alpha_" + hashlib.sha256(
                b"KXTEST-TICKER|yes|3|42"
            ).hexdigest()[:16],
        )

    def test_different_params_produce_different_client_order_id(self):
        """Changing ticker or price creates a distinct client ID."""
        client = MagicMock()
        manager = bot.OrderManager(client)
        base = manager._client_order_id("KXTEST-TICKER", "yes", 3, 42)

        self.assertNotEqual(
            base, manager._client_order_id("KXOTHER-TICKER", "yes", 3, 42)
        )
        self.assertNotEqual(
            base, manager._client_order_id("KXTEST-TICKER", "yes", 3, 43)
        )


if __name__ == "__main__":
    unittest.main()
