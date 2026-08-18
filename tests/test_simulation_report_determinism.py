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

        self.assertEqual(report["reportVersion"], "DETERMINISTIC_V12")
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

    def test_web_search_verification_is_added_only_for_key_trades(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        report = generator.build_deterministic_report(
            1, self.trades, self.participants, analytics=self.analytics,
        )
        verified = {
            "verdict": "REALIZED",
            "verdictLabel": "근거 실현",
            "summary": "실적 개선이 이후 공시로 확인되었습니다.",
            "checkedUntil": "2026-08-13",
            "claimResults": [{"claim": "실적 개선", "status": "REALIZED", "sources": [{"url": "https://example.com"}]}],
            "sourceCount": 1,
            "verificationStatus": "COMPLETED",
        }
        with (
            patch.object(generator, "_call_llm_for_narratives", return_value={}),
            patch.object(generator, "_call_web_thesis_verifier", return_value=verified) as verifier,
        ):
            enriched = generator.enrich_report(report)

        verifier.assert_called_once_with(enriched["keyTradeReviews"][0])
        self.assertEqual(enriched["keyTradeReviews"][0]["thesisOutcome"], verified)
        self.assertEqual(enriched["generationMetadata"]["thesisVerificationStatus"], "COMPLETED")
        self.assertEqual(enriched["learningInsights"]["thesisOutcomeSummary"]["realizedTradeCount"], 1)

    def test_unrealized_theses_add_insight_and_evidence_discipline_principle(self):
        report = {
            "keyTradeReviews": [
                {"thesisOutcome": {"verificationStatus": "COMPLETED", "verdict": "NOT_REALIZED"}},
                {"thesisOutcome": {"verificationStatus": "COMPLETED", "verdict": "PARTIALLY_REALIZED"}},
            ],
            "learningInsights": {"narrative": "기존 인사이트"},
            "principleDiscoveries": [],
            "principleReinforcements": [],
            "recommendedPrinciples": [],
            "improvementActions": [],
        }

        SimulationReportGenerator._apply_thesis_learning_and_principles(report)

        self.assertEqual(report["learningInsights"]["thesisOutcomeSummary"], {
            "assessedTradeCount": 2,
            "realizedTradeCount": 0,
            "partiallyRealizedTradeCount": 1,
            "notRealizedTradeCount": 1,
            "source": "OPENAI_WEB_SEARCH",
        })
        self.assertEqual(report["principleDiscoveries"][0]["recommendationCode"], "THESIS_VALIDATION")
        self.assertEqual(report["recommendedPrinciples"][0]["targetRule"], "audit.pre_trade_thesis_validation")
        self.assertEqual(report["improvementActions"][0]["category"], "EVIDENCE_DISCIPLINE")

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

    def test_review_sections_keep_all_trades_and_select_three_key_outcomes(self):
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

        self.assertEqual([item["tradeId"] for item in report["decisionReviews"]], [5, 4, 3, 2, 1])
        self.assertEqual([item["tradeId"] for item in report["keyTradeReviews"]], [4, 2, 5])
        self.assertEqual([item["tradeId"] for item in report["evidenceReviews"]], [5, 4, 3, 2, 1])
        self.assertEqual(report["keyTradeReviews"][0]["decisionReason"], "DB에 기록된 매수 근거 4")
        self.assertEqual(report["keyTradeReviews"][0]["returnPercent"], 12.0)
        evidence = next(item for item in report["evidenceReviews"] if item["tradeId"] == 4)
        self.assertEqual(evidence["basis"], "DB에 기록된 매수 근거 4")
        self.assertEqual(evidence["returnPercent"], 12.0)

    def test_key_trade_review_contains_execution_principle_and_outcome(self):
        trade = {
            "tradeId": 31,
            "variantId": 1,
            "securityId": 9,
            "securityName": "테스트 종목",
            "tradeSide": "BUY",
            "quantity": 12,
            "unitPrice": 15000,
            "transactionCostAmount": 90,
            "tradedAt": "2026-07-03T09:00:00",
            "decisionReason": "급등세가 계속될 것으로 판단",
        }
        report = DeterministicReportAnalyzer().build(
            [trade],
            self.participants,
            {
                "divergenceMoments": [{
                    "date": "2026-07-03",
                    "securityId": 9,
                    "actualUserActions": ["BUY"],
                    "personalBotActions": ["HOLD"],
                    "subsequent5TradingDayReturnPercent": -6.5,
                }],
                "behaviorPatterns": [{
                    "patternCode": "FOMO_BUY",
                    "count": 1,
                    "evidenceTradeIds": [31],
                }],
            },
        )

        review = report["keyTradeReviews"][0]
        self.assertEqual(report["keyTradeReviews"][0], report["decisionReviews"][0])
        self.assertEqual(review["trade"], {
            "quantity": 12.0,
            "unitPrice": 15000.0,
            "notionalAmount": 180000.0,
            "transactionCostAmount": 90.0,
        })
        self.assertEqual(review["principleReview"]["status"], "VIOLATION_PATTERN_DETECTED")
        self.assertEqual(review["principleReview"]["targetRule"], "entry.max_5day_return")
        self.assertTrue(review["principleReview"]["recommendedAction"])
        self.assertEqual(review["outcome"]["priceReturnPercent"], -6.5)
        self.assertEqual(review["outcome"]["measurementPeriod"], "5_TRADING_DAYS_AFTER_EXECUTION")

    def test_decision_review_is_not_duplicated_for_multiple_matching_moments(self):
        duplicate_moment = {
            "date": "2026-07-01",
            "securityId": 7,
            "actualUserActions": ["BUY"],
            "personalBotActions": ["HOLD"],
            "subsequent5TradingDayReturnPercent": -4.0,
        }

        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            {
                "divergenceMoments": [duplicate_moment, dict(duplicate_moment)],
                "behaviorPatterns": [],
            },
        )

        self.assertEqual([item["tradeId"] for item in report["decisionReviews"]], [11])
        self.assertEqual([item["tradeId"] for item in report["evidenceReviews"]], [11])

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

        self.assertEqual([item["tradeId"] for item in report["decisionReviews"]], [11])
        self.assertEqual(report["decisionReviews"][0]["principleJudgment"], "DECISION_DIFFERENCE")
        self.assertEqual([item["tradeId"] for item in report["evidenceReviews"]], [11])

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

    def test_database_rationale_type_precedes_keyword_fallback(self):
        trade = {
            "tradeId": 91,
            "variantId": 1,
            "securityId": 19,
            "securityName": "근거테스트",
            "tradeSide": "BUY",
            "tradedAt": "2026-07-01T09:00:00",
            "decisionReason": "좋아질 것으로 예상",
            "rationaleLabelType": "EVENT_REACTION",
        }

        report = DeterministicReportAnalyzer().build([trade], self.participants, {})

        evidence = report["evidenceReviews"][0]
        self.assertEqual(evidence["basisType"], "EVENT")
        self.assertEqual(evidence["databaseBasisType"], "EVENT_REACTION")
        self.assertEqual(evidence["basisTypeSource"], "DATABASE")
        self.assertEqual(evidence["verifiability"], "VERIFIABLE")

    def test_principle_and_price_axes_create_four_review_cases(self):
        actual = [
            {
                "tradeId": index,
                "variantId": 1,
                "securityId": index,
                "securityName": f"종목 {index}",
                "tradeSide": "BUY",
                "tradedAt": "2026-07-01T09:00:00",
                "decisionReason": "실적 확인 후 매수",
            }
            for index in range(1, 5)
        ]
        personal = [
            {
                "tradeId": 100 + index,
                "variantId": 2,
                "securityId": index,
                "tradeSide": "BUY",
                "tradedAt": "2026-07-01T09:00:00",
            }
            for index in (1, 2)
        ]
        prices = []
        dates = [f"2026-07-{day:02d}" for day in range(1, 7)]
        for security_id, final_price in ((1, 110), (2, 90), (3, 110), (4, 90)):
            for offset, price_date in enumerate(dates):
                close_price = 100 + (final_price - 100) * offset / 5
                prices.append({"securityId": security_id, "priceDate": price_date, "closePrice": close_price})
        analytics = {
            "dailyPrices": prices,
            "divergenceMoments": [
                {"date": "2026-07-01", "securityId": 3, "actualUserActions": ["BUY"], "personalBotActions": ["HOLD"], "subsequent5TradingDayReturnPercent": 10},
                {"date": "2026-07-01", "securityId": 4, "actualUserActions": ["BUY"], "personalBotActions": ["HOLD"], "subsequent5TradingDayReturnPercent": -10},
            ],
            "actualPrincipleCompliance": {
                "violations": [
                    {"tradeId": 3, "reasonCodes": ["ENTRY_RULE_VIOLATED"]},
                    {"tradeId": 4, "reasonCodes": ["ENTRY_RULE_VIOLATED"]},
                ]
            },
            "ruleSchema": {
                "audit": {
                    "interpreted_principles": [{
                        "user_natural_text": "진입 조건을 충족할 때만 매수한다",
                        "ai_mapped_rule": "entry.max_5day_return",
                        "status": "CONFIRMED",
                    }]
                }
            },
        }

        report = DeterministicReportAnalyzer().build(actual + personal, self.participants, analytics)
        reviews = {item["tradeId"]: item for item in report["decisionReviews"]}

        self.assertEqual(reviews[1]["reviewCase"], "GOOD_PROCESS_GOOD_OUTCOME")
        self.assertEqual(reviews[2]["reviewCase"], "GOOD_PROCESS_BAD_OUTCOME")
        self.assertEqual(reviews[3]["reviewCase"], "BAD_PROCESS_LUCKY_OUTCOME")
        self.assertEqual(reviews[4]["reviewCase"], "BAD_PROCESS_BAD_OUTCOME")
        self.assertEqual(report["principleReviewSummary"]["followedCount"], 2)
        self.assertEqual(report["principleReviewSummary"]["violatedCount"], 2)
        self.assertEqual(reviews[1]["matchedPrinciple"]["source"], "USER_PRINCIPLE")
        security = next(item for item in report["securityEvidenceReviews"] if item["securityId"] == 1)
        self.assertEqual(len(security["priceSeries"]), 6)
        self.assertTrue(any(item["type"] == "OUTCOME_CHECKPOINT" for item in security["chartAnnotations"]))

    def test_web_search_and_evidence_judgment_are_separate_agents(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        review = {
            "tradeId": 77,
            "securityName": "테스트전자",
            "action": "BUY",
            "tradedAt": "2026-07-01",
            "decisionReason": "신규 계약 기대",
            "marketOutcome": {"return5dPercent": 99.0},
        }
        dossier = {"claimEvidence": [], "searchSource": "OPENAI_WEB_SEARCH"}
        judgment = {
            "verdict": "UNCONFIRMED",
            "verdictLabel": "확인 불가",
            "summary": "자료 부족",
            "claimResults": [],
            "sourceCount": 0,
            "verificationStatus": "COMPLETED",
        }
        with (
            patch("app.modules.simulation.report_generator.EvidenceSearchAgent.search", return_value=dossier) as search,
            patch("app.modules.simulation.report_generator.EvidenceJudgmentAgent.judge", return_value=judgment) as judge,
        ):
            result = generator._call_web_thesis_verifier(review)

        search.assert_called_once()
        judge.assert_called_once()
        self.assertIs(judge.call_args.args[1], dossier)
        self.assertEqual(result, judgment)


if __name__ == "__main__":
    unittest.main()
