import unittest
from unittest.mock import patch
from atlas_v2.domain import Refused
from atlas_v2.service import authorized_mode


class LearningStartupTests(unittest.TestCase):
    def test_learning_requires_readonly_zero_writes_and_qualification(self):
        good = {"ATLAS_V2_MODE": "LIVE_MARKET_LEARNING", "ATLAS_V2_QUALIFICATION_ON_START": "1",
                "CAPITAL": "OFF", "PROD_ACCESS_MODE": "READ_ONLY", "BROKER_WRITES": "0",
                "REAL_ORDERS_SUBMITTED": "0"}
        with patch.dict("os.environ", good, clear=True):
            self.assertEqual(authorized_mode(), "LIVE_MARKET_LEARNING")
            for key, value in (("CAPITAL", "ON"), ("PROD_ACCESS_MODE", "LIVE"),
                               ("BROKER_WRITES", "1"), ("REAL_ORDERS_SUBMITTED", "1"),
                               ("ATLAS_V2_QUALIFICATION_ON_START", "0"), ("ATLAS_V2_MODE", "AUTO_LIVE"),
                               ("KALSHI_PRIVATE_KEY", "synthetic"), ("OPENAI_API_KEY", "synthetic")):
                with self.subTest(key=key), patch.dict("os.environ", {key: value}), self.assertRaises(Refused):
                    authorized_mode()

    def test_default_remains_public_data_only(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(authorized_mode(), "PUBLIC_DATA_ONLY")
