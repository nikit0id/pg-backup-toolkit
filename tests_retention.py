import unittest
from datetime import datetime, timedelta

from toolkit import retention


class RetentionTest(unittest.TestCase):
    def test_newest_in_each_period_wins(self):
        end = datetime(2025, 3, 2)
        keys = [
            f"lab/{end - timedelta(days=days):%Y%m%d}T{hour:02}0000000000Z_test.json"
            for days in range(90) for hour in (1, 15)
        ]
        selected = retention(keys)
        self.assertEqual(len(selected), 7 + 3 + 1)
        self.assertTrue(all("T15" in key for key in selected))
        self.assertTrue(any("/20250131" in key for key in selected))
