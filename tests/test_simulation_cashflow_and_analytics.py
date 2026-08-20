import unittest

from app.modules.simulation.analytics import (
    add_personal_bot_percentile,
    calculate_action_contributions,
    calculate_benchmarks,
    run_random_monte_carlo,
)
from app.modules.simulation.engine.backtest import BacktestEngine
from app.modules.simulation.models import Position
from app.modules.simulation.engine.strategies import ActualUserStrategy, BaseStrategy


class HoldStrategy(BaseStrategy):
    def __init__(self, variant_id=2):
        super().__init__(variant_id, "HOLD", "Hold")

    def generate_signals(self, current_date, portfolio, daily_prices_today, securities_map, context=None):
        return []


class CashflowAndAnalyticsTests(unittest.TestCase):
    def test_external_cash_inflow_is_not_counted_as_return(self):
        prices = [{
            "securityId": 1,
            "priceDate": "2026-01-05",
            "openPrice": 100.0,
            "closePrice": 100.0,
        }]
        actual = ActualUserStrategy(1, [{
            "tradeId": 1,
            "securityId": 1,
            "tradeSide": "BUY",
            "quantity": 1,
            "unitPrice": 100.0,
            "transactionCostAmount": 0.0,
            "tradedAt": "2026-01-05T09:00:00Z",
        }], trading_days=["2026-01-05"])
        initial_position = {1: Position(1, "000001", "테스트", 1, 100.0, 100.0)}
        engine = BacktestEngine(1, "2026-01-05", "2026-01-05", 100.0, {1: {}}, prices)
        engine.register_variant(1, actual, initial_positions=initial_position, initial_cash=0.0)
        engine.register_variant(2, HoldStrategy(), initial_cash=100.0)

        _, snapshots = engine.run()

        self.assertEqual([item.net_cash_flow for item in snapshots], [100.0, 100.0])
        self.assertEqual([item.daily_return for item in snapshots], [0.0, 0.0])
        self.assertEqual([item.cumulative_return for item in snapshots], [0.0, 0.0])

    def test_late_cash_inflow_preserves_prior_time_weighted_return(self):
        prices = [
            {"securityId": 1, "priceDate": "2026-01-05", "openPrice": 110.0, "closePrice": 110.0},
            {"securityId": 1, "priceDate": "2026-01-06", "openPrice": 110.0, "closePrice": 110.0},
        ]
        actual = ActualUserStrategy(1, [{
            "tradeId": 2,
            "securityId": 1,
            "tradeSide": "BUY",
            "quantity": 1,
            "unitPrice": 110.0,
            "transactionCostAmount": 0.0,
            "tradedAt": "2026-01-06T09:00:00Z",
        }], trading_days=["2026-01-05", "2026-01-06"])
        initial_position = {1: Position(1, "000001", "테스트", 1, 100.0, 100.0)}
        engine = BacktestEngine(1, "2026-01-05", "2026-01-06", 100.0, {1: {}}, prices)
        engine.register_variant(1, actual, initial_positions=initial_position, initial_cash=0.0)

        _, snapshots = engine.run()

        self.assertEqual([item.daily_return for item in snapshots], [0.0, 0.0])
        self.assertEqual([item.cumulative_return for item in snapshots], [0.0, 0.0])

    def test_first_displayed_day_rebases_existing_holdings_to_zero_percent(self):
        prices = [
            {"securityId": 1, "priceDate": "2026-01-05", "openPrice": 200.0, "closePrice": 200.0},
            {"securityId": 1, "priceDate": "2026-01-06", "openPrice": 220.0, "closePrice": 220.0},
        ]
        initial_position = {1: Position(1, "000001", "테스트", 1, 100.0, 100.0)}
        engine = BacktestEngine(1, "2026-01-05", "2026-01-06", 100.0, {1: {}}, prices)
        engine.register_variant(1, HoldStrategy(1), initial_positions=initial_position, initial_cash=0.0)

        _, snapshots = engine.run()

        self.assertEqual([item.cumulative_return for item in snapshots], [0.0, 0.1])
        self.assertEqual([item.daily_return for item in snapshots], [0.0, 0.1])
        self.assertEqual([item.drawdown_rate for item in snapshots], [0.0, 0.0])

    def test_random_distribution_reports_quantiles_and_percentile(self):
        securities = {1: {"marketType": "KOSPI", "isActive": True}}
        prices = [
            {
                "securityId": 1,
                "priceDate": f"2026-01-{day:02d}",
                "openPrice": 100.0,
                "closePrice": 100.0 + day,
                "tradingValue": 2_000_000_000,
                "marketCap": 100_000_000_000,
            }
            for day in range(1, 11)
        ]
        result = run_random_monte_carlo("2026-01-01", "2026-01-10", 10_000.0, securities, prices, run_count=20)
        add_personal_bot_percentile(result, 1.0)

        self.assertEqual(result["runCount"], 20)
        self.assertEqual(len(result["distributionPercent"]), 20)
        self.assertGreater(len(set(result["distributionPercent"])), 1)
        self.assertIn("medianReturnPercent", result)
        self.assertIsNotNone(result["personalBotPercentile"])

    def test_action_contribution_uses_directional_five_day_outcome(self):
        prices = [
            {
                "securityId": 1,
                "priceDate": f"2026-01-{day:02d}",
                "closePrice": 100.0 if day < 6 else 110.0,
            }
            for day in range(1, 7)
        ]
        trades = [{
            "variantId": 1,
            "securityId": 1,
            "tradeSide": "BUY",
            "quantity": 10,
            "unitPrice": 100.0,
            "appliedTradingDate": "2026-01-01",
        }]

        contribution = calculate_action_contributions(trades, prices)[0]

        self.assertEqual(contribution["observedOutcomeCount"], 1)
        self.assertAlmostEqual(contribution["estimated5DayDirectionalContributionAmount"], 100.0)

    def test_benchmark_uses_actual_index_and_falls_back_per_missing_market(self):
        securities = {
            1: {"marketType": "KOSPI"},
            2: {"marketType": "KOSDAQ"},
        }
        prices = [
            {"securityId": 1, "priceDate": "2026-01-01", "closePrice": 100.0},
            {"securityId": 1, "priceDate": "2026-01-02", "closePrice": 105.0},
            {"securityId": 2, "priceDate": "2026-01-01", "closePrice": 100.0},
            {"securityId": 2, "priceDate": "2026-01-02", "closePrice": 110.0},
        ]
        index_prices = [
            {"indexCode": "KOSPI", "priceDate": "2026-01-01", "closePrice": 2_500.0},
            {"indexCode": "KOSPI", "priceDate": "2026-01-02", "closePrice": 2_525.0},
        ]

        result = calculate_benchmarks(prices, securities, index_prices)

        self.assertEqual(result[0]["benchmark"], "KOSPI")
        self.assertEqual(result[0]["returnPercent"], 1.0)
        self.assertEqual(result[1]["benchmark"], "KOSDAQ_EQUAL_WEIGHT_UNIVERSE")
        self.assertEqual(result[1]["returnPercent"], 10.0)


if __name__ == "__main__":
    unittest.main()
