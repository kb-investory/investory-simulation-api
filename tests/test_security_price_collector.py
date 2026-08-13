import unittest
from decimal import Decimal

import pandas as pd

from app.modules.simulation.security_price_collector import SecurityPriceCollector


class SecurityPriceCollectorTests(unittest.TestCase):
    def test_rows_from_frame_maps_fdr_units_to_database_units(self):
        frame = pd.DataFrame(
            {
                "Open": [100],
                "High": [120],
                "Low": [90],
                "Close": [110],
                "Volume": [1_000],
                "Change": [0.1],
            },
            index=pd.to_datetime(["2026-01-02"]),
        )

        rows = SecurityPriceCollector._rows_from_frame(101, frame)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 101)
        self.assertEqual(str(rows[0][1]), "2026-01-02")
        self.assertEqual(rows[0][6], Decimal("10.0"))
        self.assertEqual(rows[0][7], 1_000)
        self.assertEqual(rows[0][8], Decimal("110000"))

    def test_validate_period_rejects_reverse_range(self):
        with self.assertRaises(ValueError):
            SecurityPriceCollector._validate_period("2026-03-13", "2026-01-01")

    def test_rows_from_frame_requires_ohlcv_and_change_columns(self):
        frame = pd.DataFrame({"Close": [100]}, index=pd.to_datetime(["2026-01-02"]))
        with self.assertRaises(ValueError):
            SecurityPriceCollector._rows_from_frame(101, frame)


if __name__ == "__main__":
    unittest.main()
