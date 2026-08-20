import unittest

from app.api.endpoints.simulation_helpers import normalize_daily_snapshot
from app.modules.simulation.backtest import BacktestEngine
from app.modules.simulation.models import VirtualOrder
from app.modules.simulation.strategies import BaseStrategy


class BuyOnceStrategy(BaseStrategy):
    def __init__(self):
        super().__init__(1, "TEST", "Buy once")

    def generate_signals(self, current_date, portfolio, daily_prices_today, securities_map, context=None):
        if current_date == "2026-01-01":
            return [VirtualOrder("buy", 1, 1, "BUY", 1, 100.0, current_date, "test")]
        return []


class SimulationRateUnitTests(unittest.TestCase):
    def test_backtest_stores_rates_as_decimal_and_api_displays_percent(self):
        engine = BacktestEngine(
            simulation_run_id=1,
            period_start="2026-01-01",
            period_end="2026-01-02",
            initial_capital=10_000.0,
            securities_map={1: {}},
            daily_prices=[
                {"priceDate": "2026-01-01", "securityId": 1, "openPrice": 100.0, "closePrice": 100.0},
                {"priceDate": "2026-01-02", "securityId": 1, "openPrice": 100.0, "closePrice": 200.0},
            ],
        )
        engine.register_variant(1, BuyOnceStrategy())
        _, snapshots = engine.run()

        snapshot = snapshots[-1]
        self.assertAlmostEqual(snapshot.cumulative_return, 0.009988, places=6)
        self.assertAlmostEqual(snapshot.drawdown_rate, 0.0, places=6)

        response = normalize_daily_snapshot(snapshot)
        self.assertEqual(response["cumulativeReturnPercent"], round(snapshot.cumulative_return * 100, 2))
        self.assertEqual(response["mddPercent"], round(snapshot.drawdown_rate * 100, 2))

    def test_normalizer_converts_fractional_cumulative_return_and_mdd_once(self):
        response = normalize_daily_snapshot(
            {"cumulative_return": 0.01, "drawdown_rate": -0.025}
        )

        self.assertEqual(response["cumulativeReturn"], 0.01)
        self.assertEqual(response["cumulativeReturnPercent"], 1.0)
        self.assertEqual(response["drawdownRate"], -0.025)
        self.assertEqual(response["mddPercent"], -2.5)


if __name__ == "__main__":
    unittest.main()
