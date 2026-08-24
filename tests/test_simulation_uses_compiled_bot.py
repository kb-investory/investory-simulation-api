import unittest
from unittest.mock import MagicMock, patch

from app.api.endpoints.simulation_run_service import SimulationRunService
from app.api.endpoints.simulation_helpers import SimulationRunRequest, SIMULATION_RUN_CACHE


RULE_SCHEMA = {
    "universe": {
        "allowed_markets": ["KOSPI"],
        "min_market_cap": 0.0,
        "min_daily_trading_value": 0.0,
        "exclude_halted": True,
        "exclude_administrative": True,
    },
    "selection": {
        "factor_weights": {"value": 0.2, "growth": 0.2, "quality": 0.2, "trend": 0.2, "disclosure": 0.2},
        "min_passing_score": 100.0,
    },
    "entry": {"max_5day_return": 0.15, "moving_average_condition": "NONE", "require_positive_disclosure": False},
    "additional_buy": {"allowed": False},
    "portfolio": {"max_position_count": 5, "max_single_position_weight": 0.2, "max_sector_weight": 0.4},
    "exit": {"take_profit_rate": 0.2, "stop_loss_rate": -0.1, "max_holding_days": 90, "sell_on_negative_disclosure": True},
    "rebalance": {"period": "MONTHLY", "min_holding_days_before_rebalance": 14},
}


class SimulationUsesCompiledBotTests(unittest.TestCase):
    @patch("app.api.endpoints.simulation_run_service.save_simulation_report_to_db")
    @patch("app.api.endpoints.simulation_run_service.save_simulation_run_to_db")
    @patch("app.api.endpoints.simulation_run_service.reserve_simulation_run_to_db", return_value=99)
    @patch("app.api.endpoints.simulation_run_service.find_existing_simulation_from_db", return_value=None)
    @patch("app.api.endpoints.simulation_run_service.MarketIndexCollector.ensure_period", return_value={"status": "DB_HIT"})
    @patch("app.api.endpoints.simulation.AIRuleCompiler.compile", side_effect=AssertionError("LLM compiler must not run"))
    @patch("app.api.endpoints.simulation_run_service.SimulationRepository")
    def test_run_loads_persisted_rule_schema_without_llm(
        self,
        repository_class,
        compiler_call,
        _ensure_period,
        _find_existing,
        reserve_run,
        _save_run,
        _save_report,
    ):
        repository = MagicMock()
        repository_class.return_value = repository
        repository.resolve_account_id.return_value = 27
        repository.load_initial_snapshot.return_value = {
            "initialCapital": 10_000.0,
            "snapshotDate": "2026-01-01",
            "holdings": [],
        }
        repository.load_compiled_personal_bot.return_value = {
            "personalBotId": "BOT_TEST",
            "botVersion": 1,
            "analysisRunId": 2,
            "analysisVersion": "1.0",
            "ruleSchema": RULE_SCHEMA,
            "ruleCompilation": {"source": "OPENAI"},
        }
        repository.load_securities.return_value = [{
            "securityId": 1,
            "securityCode": "000001",
            "securityName": "테스트",
            "marketType": "KOSPI",
            "sectorName": "기술",
            "isActive": True,
        }]
        repository.load_daily_prices.return_value = [
            {"securityId": 1, "priceDate": "2026-01-01", "openPrice": 100.0, "closePrice": 100.0,
             "changeRate": 0.0, "day5Return": 0.0, "tradingValue": 1_000_000_000, "marketCap": 100_000_000_000},
            {"securityId": 1, "priceDate": "2026-01-02", "openPrice": 100.0, "closePrice": 100.0,
             "changeRate": 0.0, "day5Return": 0.0, "tradingValue": 1_000_000_000, "marketCap": 100_000_000_000},
        ]
        repository.load_market_index_prices.return_value = []
        repository.load_actual_trades.return_value = []
        repository.load_principles.return_value = []
        repository.load_disclosures.return_value = []
        repository.assess_trade_price_quality.return_value = {}

        # #34: POST /run은 이제 job 상태만 즉시 반환하고 실제 결과는 bounded executor에서
        # 채워진다 — 유닛테스트에서는 sync/async phase를 곧장 이어 실행해서 SIMULATION_RUN_CACHE에
        # 쌓인 최종 결과를 검증한다.
        service = SimulationRunService(
            SimulationRunRequest(
                periodStart="2026-01-01",
                periodEnd="2026-01-02",
                participantTypes=["PERSONAL_BOT"],
                personalBotId="BOT_TEST",
            ),
            user_id=1,
            schedule_report_enrichment=lambda *args, **kwargs: None,
        )
        cached = service.run_sync_phase()
        self.assertIsNone(cached)
        service.run_async_phase()
        response = SIMULATION_RUN_CACHE[service.db_run_id]

        compiler_call.assert_not_called()
        repository.load_initial_snapshot.assert_called_once_with(27, "2026-01-01")
        repository.load_compiled_personal_bot.assert_called_once_with(1, "BOT_TEST")
        self.assertEqual(response["personalBotId"], "BOT_TEST")
        self.assertEqual(response["ruleSchema"], RULE_SCHEMA)
        self.assertTrue(response["ruleCompilation"]["reusedCompiledBot"])
        reserve_run.assert_called_once()
        self.assertFalse(response["executionTimingMs"]["cacheHit"])
        self.assertGreaterEqual(response["executionTimingMs"]["responseReady"], 0.0)
        self.assertIn("monteCarlo500Runs", response["executionTimingMs"])
        self.assertIn("reportGeneration", response["executionTimingMs"])
        self.assertEqual(
            response["reportJson"]["generationMetadata"]["narrativeStatus"],
            "PENDING",
        )


if __name__ == "__main__":
    unittest.main()
