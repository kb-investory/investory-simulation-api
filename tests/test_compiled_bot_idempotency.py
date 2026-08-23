import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

from app.api.endpoints.simulation import compile_simulation_bot
from app.api.endpoints.simulation_helpers import RuleCompileRequest


class CompiledBotIdempotencyTests(unittest.TestCase):
    @patch("app.api.endpoints.simulation.SimulationRepository")
    @patch("app.api.endpoints.simulation.AIRuleCompiler.compile", side_effect=AssertionError("OpenAI must not run"))
    def test_same_compilation_input_reuses_existing_bot(self, compile_call, repository_class):
        repository = MagicMock()
        repository_class.return_value = repository
        repository.resolve_account_id.return_value = 27
        repository.load_principles.return_value = [{"principleText": "한 종목 비중은 20% 이하"}]
        repository.load_latest_investor_profile.return_value = {
            "analysisRunId": 7,
            "analysisVersion": "1.0",
            "axes": {f"AXIS_{index}": {"score": 50} for index in range(6)},
        }
        repository.find_compiled_personal_bot_by_input_hash.return_value = {
            "personalBotId": "BOT_EXISTING",
            "botVersion": 3,
            "analysisRunId": 7,
            "analysisVersion": "1.0",
            "ruleSchema": {"exit": {"take_profit_rate": 0.2}},
            "ruleCompilation": {"source": "OPENAI", "model": "gpt-4o-mini"},
        }

        response = compile_simulation_bot(RuleCompileRequest(actualTrades=[]), BackgroundTasks(), user_id=1)

        compile_call.assert_not_called()
        repository.save_compiled_personal_bot.assert_not_called()
        self.assertEqual(response["personalBotId"], "BOT_EXISTING")
        self.assertEqual(response["botVersion"], "v3.0")
        self.assertTrue(response["compileCacheHit"])
        self.assertTrue(response["ruleCompilation"]["reusedCompiledBot"])


if __name__ == "__main__":
    unittest.main()
