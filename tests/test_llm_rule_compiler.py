import json
import os
import unittest
from unittest.mock import patch

from app.modules.simulation.rules.compiler import AIRuleCompiler, RuleCompilationError


PROFILE = {
    "analysisRunId": 7,
    "axes": {f"AXIS_{index}": {"typeName": f"TYPE_{index}", "score": 60 + index} for index in range(1, 7)},
}

LLM_RULE = {
    "universe": {
        "allowed_markets": ["KOSPI", "KOSDAQ"],
        "min_market_cap": 50000000000.0,
        "min_daily_trading_value": 1000000000.0,
        "exclude_halted": True,
        "exclude_administrative": True,
    },
    "selection": {
        "factor_weights": {"value": 0.2, "growth": 0.3, "quality": 0.2, "trend": 0.15, "disclosure": 0.15},
        "min_passing_score": 70.0,
    },
    "entry": {"max_5day_return": 0.15, "moving_average_condition": "NONE", "require_positive_disclosure": False},
    "additional_buy": {"allowed": True, "max_additional_count": 2, "trigger_drop_rate": -0.05, "additional_weight": 0.05},
    "portfolio": {"max_position_count": 5, "max_single_position_weight": 0.2, "max_sector_weight": 0.4},
    "exit": {"take_profit_rate": 0.2, "stop_loss_rate": -0.1, "max_holding_days": 90, "sell_on_negative_disclosure": True},
    "rebalance": {"period": "MONTHLY", "min_holding_days_before_rebalance": 14},
    "audit": {"ai_confidence": 0.9, "interpreted_principles": [], "needs_user_confirmation": []},
}


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": json.dumps(LLM_RULE)}}]}).encode("utf-8")


class LLMRuleCompilerTests(unittest.TestCase):
    def test_input_fingerprint_is_stable_for_equivalent_inputs(self):
        compiler = AIRuleCompiler(api_key="sk-test-valid-key", model="test-model")
        trades = [{
            "tradeSide": "BUY",
            "securityId": 1,
            "quantity": 2,
            "unitPrice": 1000,
            "tradedAt": "2026-01-02T09:00:00Z",
            "transactionCostAmount": 10,
        }]

        first = compiler.build_input_fingerprint([" 원칙 A "], PROFILE, trades)
        second = compiler.build_input_fingerprint(["원칙 A"], dict(PROFILE), list(trades))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_input_fingerprint_changes_when_semantic_input_changes(self):
        compiler = AIRuleCompiler(api_key="sk-test-valid-key", model="test-model")
        base = compiler.build_input_fingerprint(["원칙 A"], PROFILE, [])
        changed_principle = compiler.build_input_fingerprint(["원칙 B"], PROFILE, [])
        changed_profile = compiler.build_input_fingerprint(
            ["원칙 A"],
            {**PROFILE, "analysisRunId": 8},
            [],
        )
        changed_trades = compiler.build_input_fingerprint(
            ["원칙 A"],
            PROFILE,
            [{"tradeSide": "BUY", "securityId": 1, "quantity": 1, "unitPrice": 100}],
        )

        self.assertNotEqual(base, changed_principle)
        self.assertNotEqual(base, changed_profile)
        self.assertNotEqual(base, changed_trades)

    def test_missing_key_fails_instead_of_using_local_fallback(self):
        compiler = AIRuleCompiler(api_key="placeholder")
        compiler.api_key = ""
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(RuleCompilationError) as context:
                compiler.compile(["원칙"], PROFILE, [])
        self.assertEqual(context.exception.code, "LLM_CONFIGURATION_REQUIRED")

    @patch("app.modules.simulation.llm_client.urlopen", return_value=FakeResponse())
    def test_valid_llm_response_is_used_and_records_no_fallback(self, mocked_urlopen):
        compiler = AIRuleCompiler(api_key="sk-test-valid-key")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            result = compiler.compile(
                ["한 종목 비중은 25%를 넘지 않는다."],
                PROFILE,
                [{"tradeSide": "BUY", "securityId": 1, "quantity": 2, "unitPrice": 1000, "tradedAt": "2026-01-02"}],
            )

        self.assertEqual(result.portfolio.max_position_count, 5)
        self.assertEqual(compiler.last_compilation_metadata["source"], "OPENAI")
        self.assertFalse(compiler.last_compilation_metadata["fallbackUsed"])
        self.assertEqual(compiler.last_compilation_metadata["profileAnalysisRunId"], 7)
        request = mocked_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])


if __name__ == "__main__":
    unittest.main()
