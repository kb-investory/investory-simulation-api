import unittest
import os
from unittest.mock import patch

from app.modules.simulation.collectors.dart_collector import DartCollector, DisclosureAnalysisError


class DisclosureRuleTests(unittest.TestCase):
    def setUp(self):
        self.collector = DartCollector(api_key="")

    def test_negative_disclosure_is_classified_without_llm(self):
        result = self.collector.evaluate_disclosure_impact("횡령·배임 발생")
        self.assertEqual(result["direction"], "NEGATIVE")
        self.assertLess(result["impactScore"], 50)

    def test_positive_disclosure_is_classified_without_llm(self):
        result = self.collector.evaluate_disclosure_impact("대규모 공급계약 체결", 0.4)
        self.assertEqual(result["direction"], "POSITIVE")
        self.assertGreaterEqual(result["impactScore"], 80)

    def test_historical_rule_only_mode_does_not_call_llm(self):
        self.collector._call_openai_llm = lambda *args, **kwargs: self.fail("LLM must not be called")
        self.collector._call_gemini_llm = lambda *args, **kwargs: self.fail("LLM must not be called")

        result = self.collector.evaluate_disclosure_impact(
            "정기주주총회 결과",
            allow_llm=False,
        )

        self.assertEqual(result["direction"], "NEUTRAL")
        self.assertEqual(result["impactScore"], 75.0)

    def test_required_llm_mode_fails_without_key_instead_of_falling_back(self):
        self.collector.api_key = ""
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(DisclosureAnalysisError):
                self.collector.evaluate_disclosure_impact("대규모 공급계약 체결", require_llm=True)

    def test_required_llm_mode_runs_before_keyword_rules(self):
        self.collector.api_key = "sk-test-valid-key"
        self.collector._call_openai_llm = lambda *_args, **_kwargs: {
            "direction": "NEUTRAL",
            "impactScore": 70.0,
            "reason": "[OpenAI GPT AI 심층분석] 테스트",
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            result = self.collector.evaluate_disclosure_impact(
                "대규모 공급계약 체결",
                require_llm=True,
            )
        self.assertEqual(result["direction"], "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
