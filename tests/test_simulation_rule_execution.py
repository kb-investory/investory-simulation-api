import unittest

from app.modules.simulation.evaluator import StockEvaluator
from app.modules.simulation.backtest import BacktestEngine
from app.modules.simulation.rule_schema import SelectionRule
from app.modules.simulation.models import Portfolio, Position
from app.modules.simulation.strategies import FamousStrategyBot, PersonalBotStrategy


class SimulationRuleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.evaluator = StockEvaluator()
        self.security = {
            "securityId": 1,
            "securityCode": "000001",
            "securityName": "테스트",
            "marketType": "KOSPI",
            "sectorName": "기술",
            "isActive": True,
        }
        self.price = {
            "securityId": 1,
            "closePrice": 100.0,
            "changeRate": 0.01,
            "day5Return": 0.05,
            "movingAverage5": 98.0,
            "movingAverage20": 95.0,
            "tradingValue": 2_000_000_000,
            "marketCap": 100_000_000_000,
            "per": 10.0,
            "pbr": 1.0,
            "roe": 0.15,
            "debtRatio": 0.5,
            "revenueGrowth": 0.20,
            "earningsGrowth": 0.25,
            "operatingCashFlowPositive": True,
        }

    def test_real_inputs_drive_factor_scores(self):
        scores = self.evaluator.calculate_factor_scores(1, self.price, self.security)
        self.assertNotEqual(scores["value"], 75.0)
        self.assertNotEqual(scores["quality"], 85.0)
        self.assertIsNotNone(scores["growth"])

    def test_missing_fundamentals_are_explicit_not_placeholder(self):
        scores = self.evaluator.calculate_factor_scores(
            1,
            {"closePrice": 100.0, "changeRate": 0.0, "day5Return": 0.0},
            self.security,
        )
        self.assertIsNone(scores["value"])
        self.assertIsNone(scores["quality"])

    def test_universe_and_entry_rules_filter_candidates(self):
        selection = SelectionRule(
            factor_weights={"value": 0.2, "growth": 0.2, "quality": 0.2, "trend": 0.2, "disclosure": 0.2},
            min_passing_score=0.0,
        )
        candidates = self.evaluator.screen_candidates(
            {1: self.price},
            {1: self.security},
            selection,
            universe_rule={
                "allowed_markets": ["KOSPI"],
                "min_market_cap": 50_000_000_000,
                "min_daily_trading_value": 1_000_000_000,
                "exclude_halted": True,
                "exclude_administrative": True,
            },
            entry_rule={
                "max_5day_return": 0.15,
                "moving_average_condition": "ABOVE_MA20",
                "require_positive_disclosure": False,
            },
        )
        self.assertEqual(len(candidates), 1)

        blocked = dict(self.price, day5Return=0.20)
        self.assertEqual(
            self.evaluator.screen_candidates(
                {1: blocked},
                {1: self.security},
                selection,
                universe_rule={
                    "allowed_markets": ["KOSPI"],
                    "min_market_cap": 50_000_000_000,
                    "min_daily_trading_value": 1_000_000_000,
                },
                entry_rule={"max_5day_return": 0.15, "moving_average_condition": "NONE"},
            ),
            [],
        )
        audit = self.evaluator.last_screening_audit
        self.assertEqual(audit["evaluatedCount"], 1)
        self.assertEqual(audit["passedCount"], 0)
        self.assertEqual(audit["rejectedByReason"]["MAX_5DAY_RETURN"], 1)
        self.assertEqual(audit["notableRejectedCandidates"][0]["securityId"], 1)

    def _rule_schema(self):
        return {
            "universe": {
                "allowed_markets": ["KOSPI"],
                "min_market_cap": 50_000_000_000,
                "min_daily_trading_value": 1_000_000_000,
                "exclude_halted": True,
                "exclude_administrative": True,
            },
            "selection": {
                "factor_weights": {"value": 0.2, "growth": 0.2, "quality": 0.2, "trend": 0.2, "disclosure": 0.2},
                "min_passing_score": 0.0,
            },
            "entry": {"max_5day_return": 0.15, "moving_average_condition": "NONE", "require_positive_disclosure": False},
            "additional_buy": {"allowed": True, "max_additional_count": 2, "trigger_drop_rate": -0.05, "additional_weight": 0.05},
            "portfolio": {"max_position_count": 1, "max_single_position_weight": 0.20, "max_sector_weight": 0.40},
            "exit": {"take_profit_rate": 1.0, "stop_loss_rate": -1.0, "max_holding_days": 90, "sell_on_negative_disclosure": True},
            "rebalance": {"period": "MONTHLY", "min_holding_days_before_rebalance": 14},
        }

    def test_additional_buy_rule_is_executed(self):
        strategy = PersonalBotStrategy(2, [], self._rule_schema())
        portfolio = Portfolio(
            "PERSONAL_BOT",
            cash_balance=9_100.0,
            initial_capital=10_000.0,
            positions={1: Position(1, "000001", "테스트", 10, 100.0, 90.0, "2026-01-01")},
        )
        price = dict(self.price, closePrice=90.0, day5Return=-0.05)
        orders = strategy.generate_signals("2026-01-10", portfolio, {1: price}, {1: self.security})
        self.assertTrue(any(order.trade_side == "ADD" for order in orders))

    def test_max_holding_days_rule_is_executed(self):
        strategy = PersonalBotStrategy(2, [], self._rule_schema())
        portfolio = Portfolio(
            "PERSONAL_BOT",
            cash_balance=0.0,
            initial_capital=10_000.0,
            positions={1: Position(1, "000001", "테스트", 100, 100.0, 100.0, "2026-01-01")},
        )
        orders = strategy.generate_signals("2026-04-02", portfolio, {1: self.price}, {1: self.security})
        self.assertTrue(any("EXIT_MAX_HOLDING_DAYS" in order.reason_codes for order in orders))

    def test_monthly_rebalance_reduces_overweight_position(self):
        schema = self._rule_schema()
        schema["exit"]["max_holding_days"] = 999
        strategy = PersonalBotStrategy(2, [], schema)
        portfolio = Portfolio(
            "PERSONAL_BOT",
            cash_balance=0.0,
            initial_capital=10_000.0,
            positions={1: Position(1, "000001", "테스트", 100, 100.0, 100.0, "2026-01-01")},
        )
        strategy.generate_signals("2026-01-20", portfolio, {1: self.price}, {1: self.security})
        orders = strategy.generate_signals("2026-02-02", portfolio, {1: self.price}, {1: self.security})
        self.assertTrue(any("REBALANCE_MAX_POSITION_WEIGHT" in order.reason_codes for order in orders))
        self.assertTrue(any("REBALANCE_MAX_SECTOR_WEIGHT" in order.reason_codes for order in orders))

    def test_monthly_rebalance_enforces_max_position_count(self):
        schema = self._rule_schema()
        schema["exit"]["max_holding_days"] = 999
        strategy = PersonalBotStrategy(2, [], schema)
        second_security = dict(self.security, securityId=2, securityCode="000002", securityName="테스트2", sectorName="금융")
        second_price = dict(self.price, securityId=2)
        portfolio = Portfolio(
            "PERSONAL_BOT",
            cash_balance=0.0,
            initial_capital=20_000.0,
            positions={
                1: Position(1, "000001", "테스트", 100, 100.0, 100.0, "2026-01-01"),
                2: Position(2, "000002", "테스트2", 100, 100.0, 100.0, "2026-01-01"),
            },
        )
        prices = {1: self.price, 2: second_price}
        securities = {1: self.security, 2: second_security}
        strategy.generate_signals("2026-01-20", portfolio, prices, securities)
        orders = strategy.generate_signals("2026-02-02", portfolio, prices, securities)
        self.assertEqual(sum("REBALANCE_MAX_POSITION_COUNT" in order.reason_codes for order in orders), 1)

    def test_engine_collects_daily_screening_summary_without_full_rejection_log(self):
        strategy = PersonalBotStrategy(2, [], self._rule_schema())
        prices = [
            dict(self.price, priceDate="2026-01-05", openPrice=100.0),
            dict(self.price, priceDate="2026-01-06", openPrice=100.0),
        ]
        engine = BacktestEngine(1, "2026-01-05", "2026-01-06", 10_000.0, {1: self.security}, prices)
        engine.register_variant(2, strategy)

        engine.run()

        self.assertEqual(len(engine.screening_audits), 2)
        self.assertEqual(engine.screening_audits[0]["variantType"], "PERSONAL_BOT")
        self.assertLessEqual(len(engine.screening_audits[0]["notableRejectedCandidates"]), 10)

    def test_personal_bot_buy_reason_contains_security_specific_db_values(self):
        strategy = PersonalBotStrategy(2, [], self._rule_schema())
        portfolio = Portfolio("PERSONAL_BOT", cash_balance=10_000.0, initial_capital=10_000.0)

        orders = strategy.generate_signals(
            "2026-01-05", portfolio, {1: self.price}, {1: self.security}
        )

        reason = orders[0].rationale
        self.assertIn("테스트(000001)를", reason)
        self.assertIn("종가 100원 기준으로 매수했습니다", reason)
        self.assertIn("PER 10.0배", reason)
        self.assertIn("ROE +15.0%", reason)
        self.assertIn("최근 5일 수익률은 +5.0%", reason)
        self.assertNotIn("스크리닝", reason)

    def test_famous_bot_buy_reason_contains_security_specific_db_values(self):
        strategy = FamousStrategyBot(3)
        portfolio = Portfolio("FAMOUS_STRATEGY", cash_balance=10_000.0, initial_capital=10_000.0)

        orders = strategy.generate_signals(
            "2026-01-05", portfolio, {1: self.price}, {1: self.security}
        )

        reason = orders[0].rationale
        self.assertIn("테스트(000001)를", reason)
        self.assertIn("기준으로 매수했습니다", reason)
        self.assertIn("PBR 1.0배", reason)
        self.assertIn("영업현금흐름 흑자", reason)
        self.assertNotIn("저PER/고ROE", reason)


if __name__ == "__main__":
    unittest.main()
