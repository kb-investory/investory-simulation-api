import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from app.api.endpoints.simulation import run_simulation
from app.api.endpoints.simulation_helpers import SimulationRunRequest
from app.modules.simulation.persistence.repository import SimulationDataError


def _not_compiled_error():
    return SimulationDataError(
        "PERSONAL_BOT_NOT_COMPILED",
        "저장된 개인 투자봇이 없습니다. 먼저 투자봇 생성 API를 실행해 주세요.",
        {},
    )


class PersonalBotExclusionTests(unittest.TestCase):
    @patch("app.api.endpoints.simulation_run_service.reserve_simulation_run_to_db", return_value=88801)
    @patch("app.api.endpoints.simulation_run_service.find_existing_simulation_from_db", return_value=None)
    @patch("app.api.endpoints.simulation_run_service.MarketIndexCollector.ensure_period", return_value={"status": "DB_HIT"})
    @patch("app.api.endpoints.simulation_run_service.SimulationRepository")
    def test_uncompiled_personal_bot_is_excluded_and_the_rest_still_runs(
        self, repository_class, _ensure_period, _find_existing, reserve_run,
    ):
        repository = MagicMock()
        repository_class.return_value = repository
        repository.resolve_account_id.return_value = 27
        repository.load_initial_snapshot.return_value = {
            "initialCapital": 10_000.0,
            "snapshotDate": "2026-01-01",
            "holdings": [],
        }
        repository.load_compiled_personal_bot.side_effect = _not_compiled_error()
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

        background_tasks = BackgroundTasks()
        response = run_simulation(SimulationRunRequest(
            periodStart="2026-01-01",
            periodEnd="2026-01-02",
            participantTypes=["ACTUAL_USER", "PERSONAL_BOT", "RANDOM_BOT"],
        ), background_tasks, user_id=1)

        self.assertEqual(
            response["excludedParticipants"],
            [{"variantType": "PERSONAL_BOT", "reason": "PERSONAL_BOT_NOT_COMPILED"}],
        )
        variant_types = {item["variantType"] for item in response["participantSummary"]}
        self.assertEqual(variant_types, {"ACTUAL_USER", "RANDOM_BOT"})
        reserve_run.assert_called_once()

    @patch("app.api.endpoints.simulation_run_service.SimulationRepository")
    def test_personal_bot_as_the_only_participant_fails_after_exclusion(self, repository_class):
        repository = MagicMock()
        repository_class.return_value = repository
        repository.resolve_account_id.return_value = 27
        repository.load_initial_snapshot.return_value = {
            "initialCapital": 10_000.0,
            "snapshotDate": "2026-01-01",
            "holdings": [],
        }
        repository.load_compiled_personal_bot.side_effect = _not_compiled_error()

        background_tasks = BackgroundTasks()
        with self.assertRaises(HTTPException) as context:
            run_simulation(SimulationRunRequest(
                periodStart="2026-01-01",
                periodEnd="2026-01-02",
                participantTypes=["PERSONAL_BOT"],
            ), background_tasks, user_id=1)

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(context.exception.detail["code"], "NO_RUNNABLE_PARTICIPANTS")

    @patch("app.api.endpoints.simulation_run_service.SimulationRepository")
    def test_explicit_personal_bot_id_that_does_not_exist_still_fails_the_whole_request(
        self, repository_class,
    ):
        repository = MagicMock()
        repository_class.return_value = repository
        repository.resolve_account_id.return_value = 27
        repository.load_initial_snapshot.return_value = {
            "initialCapital": 10_000.0,
            "snapshotDate": "2026-01-01",
            "holdings": [],
        }
        repository.load_compiled_personal_bot.side_effect = _not_compiled_error()

        background_tasks = BackgroundTasks()
        with self.assertRaises(HTTPException) as context:
            run_simulation(SimulationRunRequest(
                periodStart="2026-01-01",
                periodEnd="2026-01-02",
                participantTypes=["ACTUAL_USER", "PERSONAL_BOT"],
                personalBotId="BOT_DOES_NOT_EXIST",
            ), background_tasks, user_id=1)

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(context.exception.detail["code"], "PERSONAL_BOT_NOT_COMPILED")


if __name__ == "__main__":
    unittest.main()
