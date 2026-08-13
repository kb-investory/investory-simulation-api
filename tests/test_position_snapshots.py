import unittest

from app.modules.simulation.backtest import BacktestEngine
from app.modules.simulation.models import Position
from app.modules.simulation.strategies import BaseStrategy


class HoldStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(1, "HOLD", "Hold")

    def generate_signals(
        self,
        current_date,
        portfolio,
        daily_prices_today,
        securities_map,
        context=None,
    ):
        return []


class PositionSnapshotTests(unittest.TestCase):
    def test_position_snapshot_uses_daily_close_for_return(self):
        prices = [
            {
                "securityId": 117,
                "priceDate": "2026-01-05",
                "openPrice": 100.0,
                "closePrice": 110.0,
            },
            {
                "securityId": 117,
                "priceDate": "2026-01-06",
                "openPrice": 110.0,
                "closePrice": 120.0,
            },
        ]
        position = Position(117, "068270", "셀트리온보통주", 2, 100.0, 100.0)
        engine = BacktestEngine(
            1,
            "2026-01-05",
            "2026-01-06",
            200.0,
            {117: {"securityCode": "068270", "securityName": "셀트리온보통주"}},
            prices,
        )
        engine.register_variant(1, HoldStrategy(), initial_positions={117: position}, initial_cash=0.0)

        engine.run()

        self.assertEqual(len(engine.position_snapshots), 2)
        self.assertEqual(engine.position_snapshots[0]["currentPrice"], 110.0)
        self.assertEqual(engine.position_snapshots[0]["returnPercent"], 10.0)
        self.assertEqual(engine.position_snapshots[1]["marketValue"], 240.0)
        self.assertEqual(engine.position_snapshots[1]["returnPercent"], 20.0)


if __name__ == "__main__":
    unittest.main()
