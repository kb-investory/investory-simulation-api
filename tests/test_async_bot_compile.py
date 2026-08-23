import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

from app.api.endpoints.simulation import (
    COMPILE_IN_PROGRESS,
    COMPILE_JOB_CACHE,
    _compile_personal_bot_in_background,
    compile_simulation_bot,
    get_compile_job_status,
)
from app.api.endpoints.simulation_helpers import RuleCompileRequest
from app.modules.simulation.rules.compiler import RuleCompilationError


RULE_SCHEMA = {
    "universe": {"allowed_markets": ["KOSPI"], "min_market_cap": 0.0, "min_daily_trading_value": 0.0,
                 "exclude_halted": True, "exclude_administrative": True},
    "selection": {"factor_weights": {"value": 0.2, "growth": 0.2, "quality": 0.2, "trend": 0.2, "disclosure": 0.2},
                  "min_passing_score": 100.0},
    "entry": {"max_5day_return": 0.15, "moving_average_condition": "NONE", "require_positive_disclosure": False},
    "additional_buy": {"allowed": False},
    "portfolio": {"max_position_count": 5, "max_single_position_weight": 0.2, "max_sector_weight": 0.4},
    "exit": {"take_profit_rate": 0.2, "stop_loss_rate": -0.1, "max_holding_days": 90, "sell_on_negative_disclosure": True},
    "rebalance": {"period": "MONTHLY", "min_holding_days_before_rebalance": 14},
}


def _base_repository():
    repository = MagicMock()
    repository.resolve_account_id.return_value = 27
    repository.load_principles.return_value = [{"principleText": "한 종목 비중은 20% 이하"}]
    repository.load_latest_investor_profile.return_value = {
        "analysisRunId": 7,
        "analysisVersion": "1.0",
        "axes": {f"AXIS_{index}": {"score": 50} for index in range(6)},
    }
    repository.find_compiled_personal_bot_by_input_hash.return_value = None
    return repository


class AsyncBotCompileTests(unittest.TestCase):
    def setUp(self):
        COMPILE_JOB_CACHE.clear()
        COMPILE_IN_PROGRESS.clear()

    def tearDown(self):
        COMPILE_JOB_CACHE.clear()
        COMPILE_IN_PROGRESS.clear()

    @patch("app.api.endpoints.simulation.AIRuleCompiler.compile", side_effect=AssertionError("LLM must not run inline"))
    @patch("app.api.endpoints.simulation.SimulationRepository")
    def test_new_compile_returns_immediately_and_schedules_one_background_task(
        self, repository_class, compile_call,
    ):
        repository_class.return_value = _base_repository()

        background_tasks = BackgroundTasks()
        response = compile_simulation_bot(
            RuleCompileRequest(actualTrades=[]), background_tasks, user_id=1,
        )

        compile_call.assert_not_called()  # the LLM call never runs inline
        self.assertEqual(response["status"], "RUNNING")
        self.assertNotIn("botDetail", response)
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertEqual(COMPILE_JOB_CACHE[response["jobId"]]["status"], "RUNNING")
        self.assertEqual(COMPILE_IN_PROGRESS[1], response["jobId"])

    @patch("app.api.endpoints.simulation.AIRuleCompiler.compile", side_effect=AssertionError("LLM must not run inline"))
    @patch("app.api.endpoints.simulation.SimulationRepository")
    def test_duplicate_request_while_in_flight_reuses_the_same_job(
        self, repository_class, compile_call,
    ):
        repository_class.return_value = _base_repository()

        first = compile_simulation_bot(RuleCompileRequest(actualTrades=[]), BackgroundTasks(), user_id=1)
        second_tasks = BackgroundTasks()
        second = compile_simulation_bot(RuleCompileRequest(actualTrades=[]), second_tasks, user_id=1)

        self.assertEqual(first["jobId"], second["jobId"])
        self.assertEqual(len(second_tasks.tasks), 0)  # no duplicate LLM job queued

    @patch("app.api.endpoints.simulation.SimulationRepository")
    def test_background_success_completes_the_job_and_clears_the_lock(self, repository_class):
        repository = _base_repository()
        saved_bot = {
            "personalBotId": "BOT_NEW",
            "botVersion": 1,
            "analysisRunId": 7,
            "analysisVersion": "1.0",
            "ruleSchema": RULE_SCHEMA,
            "ruleCompilation": {"source": "OPENAI"},
        }
        repository.save_compiled_personal_bot.return_value = saved_bot
        repository.load_compiled_personal_bot.return_value = saved_bot
        repository_class.return_value = repository
        COMPILE_IN_PROGRESS[1] = "JOB_ABC12345"

        with patch(
            "app.api.endpoints.simulation.AIRuleCompiler.compile",
            return_value=MagicMock(to_dict=lambda: RULE_SCHEMA),
        ):
            _compile_personal_bot_in_background(
                "JOB_ABC12345", 1, ["원칙"], {"axes": {}}, [], 27, [], "hash123",
            )

        result = COMPILE_JOB_CACHE["JOB_ABC12345"]
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["personalBotId"], "BOT_NEW")
        self.assertNotIn(1, COMPILE_IN_PROGRESS)

    @patch("app.api.endpoints.simulation.SimulationRepository")
    def test_background_llm_failure_marks_the_job_failed_and_clears_the_lock(self, repository_class):
        repository_class.return_value = _base_repository()
        COMPILE_IN_PROGRESS[1] = "JOB_FAIL0001"

        with patch(
            "app.api.endpoints.simulation.AIRuleCompiler.compile",
            side_effect=RuleCompilationError("LLM_RULE_COMPILATION_FAILED", "OpenAI 응답 검증 실패"),
        ):
            _compile_personal_bot_in_background(
                "JOB_FAIL0001", 1, ["원칙"], {"axes": {}}, [], 27, [], "hash456",
            )

        result = COMPILE_JOB_CACHE["JOB_FAIL0001"]
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "LLM_RULE_COMPILATION_FAILED")
        self.assertNotIn(1, COMPILE_IN_PROGRESS)

    def test_polling_a_running_job_does_not_inject_the_completed_message(self):
        COMPILE_JOB_CACHE["JOB_RUNNING1"] = {
            "jobId": "JOB_RUNNING1", "status": "RUNNING", "progressPercent": 10,
        }

        result = get_compile_job_status("JOB_RUNNING1", user_id=1)

        self.assertEqual(result["status"], "RUNNING")
        self.assertNotIn("message", result)

    def test_polling_a_failed_job_returns_the_error_as_is(self):
        COMPILE_JOB_CACHE["JOB_FAILED01"] = {
            "jobId": "JOB_FAILED01", "status": "FAILED", "progressPercent": 100,
            "error": {"code": "LLM_RULE_COMPILATION_FAILED", "message": "실패"},
        }

        result = get_compile_job_status("JOB_FAILED01", user_id=1)

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "LLM_RULE_COMPILATION_FAILED")


if __name__ == "__main__":
    unittest.main()
