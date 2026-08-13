import unittest

from app.modules.simulation.holding_snapshot_service import (
    SnapshotReconstructionError,
    reconstruct_holding_snapshots,
    reconstruct_holding_snapshots_forward,
)


class HoldingSnapshotReconstructionTests(unittest.TestCase):
    def test_reconstructs_prior_quantity_by_reversing_later_trades(self):
        snapshots = reconstruct_holding_snapshots(
            anchor_date="2026-01-05",
            anchor_holdings=[{"securityId": 1, "quantity": 12, "averageCost": 100}],
            trades=[
                {"securityId": 1, "tradeSide": "BUY", "tradedAt": "2026-01-02", "quantity": 5, "unitPrice": 100},
                {"securityId": 1, "tradeSide": "SELL", "tradedAt": "2026-01-05", "quantity": 3, "unitPrice": 120},
            ],
            daily_prices=[
                {"securityId": 1, "priceDate": "2026-01-01", "closePrice": 90},
                {"securityId": 1, "priceDate": "2026-01-02", "closePrice": 100},
            ],
        )

        by_date = {item["snapshotDate"]: item for item in snapshots}
        self.assertEqual(by_date["2026-01-01"]["quantity"], 10)
        self.assertEqual(by_date["2026-01-02"]["quantity"], 15)
        self.assertEqual(by_date["2026-01-02"]["marketValue"], 1500)

    def test_rejects_inconsistent_history_that_reconstructs_negative_quantity(self):
        with self.assertRaises(SnapshotReconstructionError):
            reconstruct_holding_snapshots(
                anchor_date="2026-01-05",
                anchor_holdings=[{"securityId": 1, "quantity": 1, "averageCost": 100}],
                trades=[
                    {"securityId": 1, "tradeSide": "BUY", "tradedAt": "2026-01-05", "quantity": 2, "unitPrice": 100}
                ],
                daily_prices=[{"securityId": 1, "priceDate": "2026-01-02", "closePrice": 100}],
            )

    def test_forward_reconstruction_maps_weekend_trade_to_next_trading_day(self):
        result = reconstruct_holding_snapshots_forward(
            trades=[
                {"tradeId": 1, "securityId": 1, "tradeSide": "BUY", "tradedAt": "2026-01-03", "quantity": 2, "unitPrice": 100}
            ],
            daily_prices=[
                {"securityId": 1, "priceDate": "2026-01-02", "closePrice": 90},
                {"securityId": 1, "priceDate": "2026-01-05", "closePrice": 110},
            ],
        )

        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(result["snapshots"][0]["snapshotDate"], "2026-01-05")
        self.assertEqual(result["snapshots"][0]["marketValue"], 220)

    def test_forward_reconstruction_starts_on_trade_date_before_price_history(self):
        result = reconstruct_holding_snapshots_forward(
            trades=[
                {"tradeId": 1, "securityId": 1, "tradeSide": "BUY", "tradedAt": "2026-01-12", "quantity": 3, "unitPrice": 100},
                {"tradeId": 2, "securityId": 2, "tradeSide": "BUY", "tradedAt": "2026-02-05", "quantity": 2, "unitPrice": 200},
            ],
            daily_prices=[
                {"securityId": 1, "priceDate": "2026-03-13", "closePrice": 120},
                {"securityId": 2, "priceDate": "2026-03-13", "closePrice": 210},
            ],
        )

        by_date_and_security = {
            (item["snapshotDate"], item["securityId"]): item for item in result["snapshots"]
        }
        self.assertEqual(by_date_and_security[("2026-01-12", 1)]["marketValue"], 300)
        self.assertEqual(by_date_and_security[("2026-02-05", 1)]["marketValue"], 300)
        self.assertEqual(by_date_and_security[("2026-02-05", 2)]["marketValue"], 400)
        self.assertEqual(by_date_and_security[("2026-03-13", 1)]["marketValue"], 360)

    def test_forward_reconstruction_records_sell_shortfall(self):
        result = reconstruct_holding_snapshots_forward(
            trades=[
                {"tradeId": 2, "securityId": 1, "tradeSide": "SELL", "tradedAt": "2026-01-02", "quantity": 3, "unitPrice": 100}
            ],
            daily_prices=[{"securityId": 1, "priceDate": "2026-01-02", "closePrice": 100}],
        )

        self.assertEqual(result["snapshots"], [])
        self.assertEqual(result["adjustments"][0]["shortfallQuantity"], 3)


if __name__ == "__main__":
    unittest.main()
