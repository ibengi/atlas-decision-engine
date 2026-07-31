"""Regression tests for explicit boolean environment defaults."""
import os
import unittest

import kalshi_alpha_bot
import market_scanner


class EnvBooleanDefaultsTest(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("ENV_B_REGRESSION", None)

    def tearDown(self):
        os.environ.pop("ENV_B_REGRESSION", None)
        if self._old is not None:
            os.environ["ENV_B_REGRESSION"] = self._old

    def test_kalshi_defaults_and_environment_overrides(self):
        env_b = kalshi_alpha_bot._env_b
        self.assertTrue(env_b("ENV_B_REGRESSION", default=True))
        self.assertFalse(env_b("ENV_B_REGRESSION", default=False))

        for value, expected in (("1", True), ("true", True),
                                ("0", False), ("false", False),
                                ("no", False), ("non", False)):
            with self.subTest(value=value):
                os.environ["ENV_B_REGRESSION"] = value
                self.assertEqual(env_b("ENV_B_REGRESSION", default=True), expected)
                self.assertEqual(env_b("ENV_B_REGRESSION", default=False), expected)

    def test_market_scanner_defaults_and_environment_overrides(self):
        env_b = market_scanner._env_b
        self.assertTrue(env_b("ENV_B_REGRESSION", default=True))
        self.assertFalse(env_b("ENV_B_REGRESSION", default=False))

        for value, expected in (("1", True), ("true", True),
                                ("0", False), ("false", False),
                                ("no", False)):
            with self.subTest(value=value):
                os.environ["ENV_B_REGRESSION"] = value
                self.assertEqual(env_b("ENV_B_REGRESSION", default=True), expected)
                self.assertEqual(env_b("ENV_B_REGRESSION", default=False), expected)

    def test_default_is_required_keyword_only_for_both_modules(self):
        with self.assertRaises(TypeError):
            kalshi_alpha_bot._env_b("ENV_B_REGRESSION")
        with self.assertRaises(TypeError):
            market_scanner._env_b("ENV_B_REGRESSION")
        with self.assertRaises(TypeError):
            kalshi_alpha_bot._env_b("ENV_B_REGRESSION", True)
        with self.assertRaises(TypeError):
            market_scanner._env_b("ENV_B_REGRESSION", False)


if __name__ == "__main__":
    unittest.main()
