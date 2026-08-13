import unittest
from unittest.mock import patch

from app.modules.simulation.report_analysis import DeterministicReportAnalyzer
from app.modules.simulation.report_generator import SimulationReportGenerator


class SimulationReportDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.trades = [
            {
                "tradeId": 11,
                "variantId": 1,
                "securityId": 7,
                "securityName": "테스트전자",
                "tradeSide": "BUY",
                "tradedAt": "2026-07-01T09:00:00",
                "appliedTradingDate": "2026-07-01",
                "decisionReason": "과거 실제 매매 내역 재현",
            }
        ]
        self.participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": 5.0},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": 12.0},
        ]
        self.analytics = {
            "divergenceMoments": [
                {
                    "date": "2026-07-01",
                    "securityId": 7,
                    "actualUserActions": ["BUY"],
                    "personalBotActions": ["HOLD"],
                    "subsequent5TradingDayReturnPercent": -4.0,
                }
            ],
            "behaviorPatterns": [
                {
                    "patternCode": "FOMO_BUY",
                    "label": "추격매수",
                    "count": 1,
                    "evidenceTradeIds": [11],
                    "description": "최근 5거래일 급등 후 매수한 거래입니다.",
                }
            ],
        }

    def test_all_judgments_and_rule_json_are_deterministic(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            self.analytics,
        )

        self.assertEqual(report["reportVersion"], "DETERMINISTIC_V10")
        self.assertEqual(report["decisionReviews"][0]["emotionTag"], "FOMO_BUY")
        self.assertEqual(report["evidenceReviews"][0]["basisType"], "UNKNOWN")
        self.assertEqual(report["evidenceReviews"][0]["confidenceScore"], 10)
        self.assertEqual(report["learningInsights"]["actualReturnPercent"], 5.0)
        self.assertIn("원칙봇 수익률이 실제 투자보다 7.00%p 높았습니다", report["learningInsights"]["narrative"])
        self.assertEqual(report["learningInsights"]["narrativeSource"], "DETERMINISTIC_TEMPLATE")
        self.assertEqual(report["learningInsights"]["principleReturnPercent"], 12.0)
        self.assertEqual(report["learningInsights"]["returnImprovementPercentPoint"], 7.0)
        self.assertEqual(
            report["recommendedPrinciples"][0]["ruleJson"],
            {"entry": {"max_5day_return": 0.10}},
        )
        self.assertEqual(report["principleDiscoveries"][0]["proposalType"], "DISCOVERY")
        self.assertEqual(report["principleReinforcements"], [])

    def test_llm_can_only_add_whitelisted_narratives(self):
        malicious_response = {
            "decisionNarratives": [
                {
                    "tradeId": 11,
                    "explanation": "확정된 행동 차이를 쉽게 설명한 문장입니다.",
                    "emotionTag": "RATIONAL_TRADE",
                    "subsequentReturnPercent": 999,
                }
            ],
            "learningNarrative": "수치가 아니라 확정된 결과의 의미만 설명합니다.",
            "recommendationNarratives": [
                {"recommendationId": 2001, "explanation": "추천 규칙의 적용 이유입니다."}
            ],
            "learningInsights": {"actualReturnPercent": 999},
            "recommendedPrinciples": [{"ruleJson": {"exit": {"stop_loss_rate": 1.0}}}],
            "principleProposals": [
                {
                    "opportunityId": "FOMO_BUY:entry.max_5day_return",
                    "proposedValue": 0.99,
                }
            ],
        }
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value=malicious_response,
        ):
            report = generator.generate_report(
                1,
                self.trades,
                self.participants,
                analytics=self.analytics,
            )

        self.assertEqual(report["decisionReviews"][0]["emotionTag"], "FOMO_BUY")
        self.assertEqual(report["decisionReviews"][0]["subsequentReturnPercent"], -4.0)
        self.assertEqual(report["learningInsights"]["actualReturnPercent"], 5.0)
        self.assertEqual(
            report["recommendedPrinciples"][0]["ruleJson"],
            {"entry": {"max_5day_return": 0.10}},
        )
        self.assertEqual(report["generationMetadata"]["narrativeSource"], "OPENAI")
        self.assertIn("narrative", report["decisionReviews"][0])
        self.assertIn("실제 매매 근거:", report["decisionReviews"][0]["principleFeedback"])
        self.assertNotEqual(
            report["learningInsights"]["narrative"],
            malicious_response["learningNarrative"],
        )
        self.assertEqual(report["generationMetadata"]["narrativeStatus"], "COMPLETED")

    def test_deterministic_report_does_not_wait_for_llm(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(generator, "_call_llm_for_narratives") as llm_call:
            report = generator.build_deterministic_report(
                1,
                self.trades,
                self.participants,
                analytics=self.analytics,
            )

        llm_call.assert_not_called()
        self.assertEqual(report["generationMetadata"]["narrativeStatus"], "PENDING")
        self.assertEqual(report["generationMetadata"]["narrativeSource"], "NOT_REQUESTED")
        self.assertIn("원칙봇 수익률이 실제 투자보다 7.00%p 높았습니다", report["learningInsights"]["narrative"])

    def test_learning_narrative_follows_negative_return_difference(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            [
                {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": -1.07},
                {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": -5.12},
            ],
            self.analytics,
        )

        narrative = report["learningInsights"]["narrative"]
        self.assertIn("실제 투자 수익률은 -1.07%", narrative)
        self.assertIn("원칙봇 수익률은 -5.12%", narrative)
        self.assertIn("실제 투자 수익률이 원칙봇보다 4.05%p 높았습니다", narrative)

    def test_review_sections_keep_only_three_largest_outcomes_with_recorded_reasons(self):
        trades = []
        moments = []
        for index, outcome in enumerate((1.0, -8.0, 3.0, 12.0, -5.0), start=1):
            trades.append({
                "tradeId": index,
                "variantId": 1,
                "securityId": index,
                "securityName": f"종목 {index}",
                "tradeSide": "BUY",
                "tradedAt": f"2026-07-{index:02d}T09:00:00",
                "decisionReason": f"DB에 기록된 매수 근거 {index}",
            })
            moments.append({
                "date": f"2026-07-{index:02d}",
                "securityId": index,
                "actualUserActions": ["BUY"],
                "personalBotActions": ["HOLD"],
                "subsequent5TradingDayReturnPercent": outcome,
            })

        report = DeterministicReportAnalyzer().build(
            trades,
            self.participants,
            {"divergenceMoments": moments, "behaviorPatterns": []},
        )

        self.assertEqual([item["tradeId"] for item in report["decisionReviews"]], [4, 2, 5])
        self.assertEqual([item["tradeId"] for item in report["evidenceReviews"]], [4, 2, 5])
        self.assertEqual(report["decisionReviews"][0]["decisionReason"], "DB에 기록된 매수 근거 4")
        self.assertEqual(report["decisionReviews"][0]["returnPercent"], 12.0)
        self.assertEqual(report["evidenceReviews"][0]["basis"], "DB에 기록된 매수 근거 4")
        self.assertEqual(report["evidenceReviews"][0]["returnPercent"], 12.0)

    def test_hold_only_divergence_is_not_shown_as_an_actual_trade_review(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            {
                "divergenceMoments": [{
                    "date": "2026-07-10",
                    "securityId": 7,
                    "actualUserActions": ["HOLD"],
                    "personalBotActions": ["BUY"],
                    "subsequent5TradingDayReturnPercent": 99.0,
                }],
                "behaviorPatterns": [],
            },
        )

        self.assertEqual(report["decisionReviews"], [])
        self.assertEqual(report["evidenceReviews"], [])

    def test_applied_date_can_match_original_trade_within_three_days(self):
        report = DeterministicReportAnalyzer().build(
            [{
                "tradeId": 12,
                "variantId": 1,
                "securityId": 7,
                "securityName": "테스트 종목",
                "tradeSide": "BUY",
                "tradedAt": "2026-07-31T16:00:00",
                "decisionReason": "DB 원본 매수 근거",
            }],
            self.participants,
            {
                "divergenceMoments": [{
                    "date": "2026-08-03",
                    "securityId": 7,
                    "actualUserActions": ["BUY"],
                    "personalBotActions": ["HOLD"],
                    "subsequent5TradingDayReturnPercent": -4.25,
                }],
                "behaviorPatterns": [],
            },
        )

        self.assertEqual(report["decisionReviews"][0]["tradeId"], 12)
        self.assertEqual(report["decisionReviews"][0]["decisionReason"], "DB 원본 매수 근거")
        self.assertEqual(report["decisionReviews"][0]["returnPercent"], -4.25)

    def test_existing_explicit_rule_is_reinforced_and_valid_llm_value_is_applied(self):
        analytics = dict(self.analytics)
        analytics["ruleSchema"] = {
            "entry": {"max_5day_return": 0.15},
            "audit": {
                "interpreted_principles": [
                    {"ai_mapped_rule": "entry.max_5day_return", "status": "CONFIRMED"}
                ]
            },
        }
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "FOMO_BUY:entry.max_5day_return",
                        "title": "추격매수 기준 강화",
                        "description": "반복된 패턴을 반영해 진입 기준을 더 명확히 합니다.",
                        "proposedValue": 0.08,
                    }
                ]
            },
        ):
            report = generator.generate_report(
                1,
                self.trades,
                self.participants,
                analytics=analytics,
            )

        reinforcement = report["principleReinforcements"][0]
        self.assertEqual(report["principleDiscoveries"], [])
        self.assertEqual(reinforcement["currentValue"], 0.15)
        self.assertEqual(reinforcement["proposedValue"], 0.08)
        self.assertEqual(reinforcement["ruleJson"], {"entry": {"max_5day_return": 0.08}})
        self.assertEqual(reinforcement["proposalSource"], "OPENAI_VALIDATED")
        self.assertEqual(report["generationMetadata"]["proposalSource"], "OPENAI_VALIDATED")

    def test_discovered_rule_accepts_only_a_bounded_llm_proposal(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "FOMO_BUY:entry.max_5day_return",
                        "title": "급등 종목 진입 보류",
                        "description": "반복된 추격매수 패턴에 적용할 신규 원칙입니다.",
                        "proposedValue": 0.12,
                    }
                ]
            },
        ):
            report = generator.generate_report(
                1,
                self.trades,
                self.participants,
                analytics=self.analytics,
            )

        discovery = report["principleDiscoveries"][0]
        self.assertEqual(discovery["proposedValue"], 0.12)
        self.assertEqual(discovery["ruleJson"], {"entry": {"max_5day_return": 0.12}})
        self.assertEqual(discovery["proposalSource"], "OPENAI_VALIDATED")

    def test_reinforcement_rejects_a_weaker_llm_threshold(self):
        analytics = dict(self.analytics)
        analytics["ruleSchema"] = {
            "entry": {"max_5day_return": 0.15},
            "audit": {
                "interpreted_principles": [
                    {"ai_mapped_rule": "entry.max_5day_return", "status": "CONFIRMED"}
                ]
            },
        }
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "FOMO_BUY:entry.max_5day_return",
                        "proposedValue": 0.18,
                    }
                ]
            },
        ):
            report = generator.generate_report(
                1,
                self.trades,
                self.participants,
                analytics=analytics,
            )

        reinforcement = report["principleReinforcements"][0]
        self.assertEqual(reinforcement["proposedValue"], 0.10)
        self.assertEqual(reinforcement["proposalSource"], "DETERMINISTIC_FALLBACK")

    def test_llm_failure_keeps_complete_deterministic_report(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            side_effect=RuntimeError("temporary failure"),
        ):
            report = generator.generate_report(
                1,
                self.trades,
                self.participants,
                analytics=self.analytics,
            )

        self.assertEqual(report["generationMetadata"]["narrativeSource"], "TEMPLATE_FALLBACK")
        self.assertEqual(report["decisionReviews"][0]["emotionTag"], "FOMO_BUY")
        self.assertTrue(report["recommendedPrinciples"])

    def test_non_executable_rationale_prompt_is_only_an_improvement_action(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            analytics={},
        )

        self.assertEqual(report["recommendedPrinciples"], [])
        self.assertEqual(report["improvementActions"][0]["category"], "EVIDENCE_DISCIPLINE")


if __name__ == "__main__":
    unittest.main()
