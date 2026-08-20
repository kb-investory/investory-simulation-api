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

    def _reinforcement_inputs(self):
        trades = self.trades + [{
            **self.trades[0],
            "tradeId": 12,
            "tradedAt": "2026-07-02T09:00:00",
            "appliedTradingDate": "2026-07-02",
        }]
        analytics = dict(self.analytics)
        analytics["divergenceMoments"] = self.analytics["divergenceMoments"] + [{
            **self.analytics["divergenceMoments"][0],
            "date": "2026-07-02",
        }]
        analytics["behaviorPatterns"] = [{
            **self.analytics["behaviorPatterns"][0],
            "count": 2,
            "evidenceTradeIds": [11, 12],
        }]
        analytics["ruleSchema"] = {
            "entry": {"max_5day_return": 0.15},
            "audit": {
                "interpreted_principles": [{
                    "user_natural_text": "급등주를 추격매수하지 않는다",
                    "ai_mapped_rule": "entry.max_5day_return",
                    "status": "CONFIRMED",
                }]
            },
        }
        analytics["principleItems"] = [{
            "principleSetItemId": 9,
            "principleText": "급등주를 추격매수하지 않는다",
            "ruleJson": {"entry": {"max_5day_return": 0.15}},
            "sortOrder": 1,
        }]
        analytics["dailyPrices"] = [
            {"securityId": 7, "priceDate": "2026-07-01", "closePrice": 100.0, "day5Return": 0.20},
            {"securityId": 7, "priceDate": "2026-07-02", "closePrice": 101.0, "day5Return": 0.20},
        ]
        return trades, analytics

    def test_all_judgments_and_rule_json_are_deterministic(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            self.analytics,
        )

        self.assertEqual(report["reportVersion"], "DETERMINISTIC")
        self.assertEqual(report["decisionReviews"][0]["emotionTag"], "FOMO_BUY")
        self.assertEqual(report["evidenceReviews"][0]["basisType"], "UNKNOWN")
        self.assertEqual(report["evidenceReviews"][0]["confidenceScore"], 10)
        self.assertEqual(report["learningInsights"]["actualReturnPercent"], 5.0)
        self.assertIn("원칙봇 수익률이 실제 투자보다 7.00%p 높았습니다", report["learningInsights"]["narrative"])
        self.assertEqual(report["learningInsights"]["narrativeSource"], "DETERMINISTIC_TEMPLATE")
        self.assertEqual(report["learningInsights"]["principleReturnPercent"], 12.0)
        self.assertEqual(report["learningInsights"]["returnImprovementPercentPoint"], 7.0)
        self.assertEqual(report["principleEvaluations"], [])
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
        self.assertEqual(report["principleReinforcements"], [])
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

    def test_unrealized_theses_add_insight_without_inventing_a_principle(self):
        report = {
            "keyTradeReviews": [
                {"thesisOutcome": {"verificationStatus": "COMPLETED", "verdict": "NOT_REALIZED"}},
                {"thesisOutcome": {"verificationStatus": "COMPLETED", "verdict": "PARTIALLY_REALIZED"}},
            ],
            "learningInsights": {"narrative": "기존 인사이트"},
            "principleReinforcements": [],
        }

        SimulationReportGenerator._apply_thesis_learning_and_principles(report)

        self.assertEqual(report["learningInsights"]["thesisOutcomeSummary"], {
            "assessedTradeCount": 2,
            "realizedTradeCount": 0,
            "partiallyRealizedTradeCount": 1,
            "notRealizedTradeCount": 1,
            "source": "OPENAI_WEB_SEARCH",
        })
        self.assertEqual(report["principleReinforcements"], [])

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
        self.assertEqual(report["decisionReviews"][0]["principleJudgment"], "NOT_APPLICABLE")
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
        trades, analytics = self._reinforcement_inputs()
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "PRINCIPLE:9:entry.max_5day_return",
                        "title": "추격매수 기준 강화",
                        "description": "반복된 패턴을 반영해 진입 기준을 더 명확히 합니다.",
                        "proposedValue": 0.08,
                    }
                ]
            },
        ):
            report = generator.generate_report(
                1,
                trades,
                self.participants,
                analytics=analytics,
            )

        reinforcement = report["principleReinforcements"][0]
        self.assertEqual(reinforcement["currentValue"], 0.15)
        self.assertEqual(reinforcement["proposedValue"], 0.08)
        self.assertEqual(reinforcement["ruleJson"], {"entry": {"max_5day_return": 0.08}})
        self.assertEqual(reinforcement["proposalSource"], "OPENAI_VALIDATED")
        self.assertEqual(report["generationMetadata"]["proposalSource"], "OPENAI_VALIDATED")

    def test_llm_cannot_create_a_new_principle_from_a_behavior_pattern(self):
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "PRINCIPLE:9:entry.max_5day_return",
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

        self.assertEqual(report["principleReinforcements"], [])

    def test_reinforcement_rejects_a_weaker_llm_threshold(self):
        trades, analytics = self._reinforcement_inputs()
        generator = SimulationReportGenerator(api_key="configured-key")
        with patch.object(
            generator,
            "_call_llm_for_narratives",
            return_value={
                "principleProposals": [
                    {
                        "opportunityId": "PRINCIPLE:9:entry.max_5day_return",
                        "proposedValue": 0.18,
                    }
                ]
            },
        ):
            report = generator.generate_report(
                1,
                trades,
                self.participants,
                analytics=analytics,
            )

        reinforcement = report["principleReinforcements"][0]
        # 0.18 is looser than the current 0.15, so the model's number is dropped
        # and the deterministic proposal stands.
        self.assertNotEqual(reinforcement["proposedValue"], 0.18)
        self.assertLess(reinforcement["proposedValue"], reinforcement["currentValue"])
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
        self.assertEqual(report["principleReinforcements"], [])

    def test_non_executable_rationale_prompt_is_only_an_improvement_action(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            analytics={},
        )

        self.assertEqual(report["principleReinforcements"], [])

    def test_all_existing_principles_are_evaluated_and_only_repeated_violations_are_strengthened(self):
        trades, analytics = self._reinforcement_inputs()
        analytics["principleItems"].append({
            "principleSetItemId": 10,
            "principleText": "손실은 10%에서 제한한다",
            "ruleJson": {"exit": {"stop_loss_rate": -0.10}},
            "sortOrder": 2,
        })
        analytics["ruleSchema"]["exit"] = {"stop_loss_rate": -0.10}
        analytics["ruleSchema"]["audit"]["interpreted_principles"].append({
            "user_natural_text": "손실은 10%에서 제한한다",
            "ai_mapped_rule": "exit.stop_loss_rate",
            "status": "CONFIRMED",
        })

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)

        self.assertEqual(report["principleEvaluationSummary"]["totalCount"], 2)
        self.assertEqual(report["principleEvaluationSummary"]["strengthenCount"], 1)
        self.assertEqual(report["principleEvaluationSummary"]["insufficientDataCount"], 1)
        self.assertEqual(report["principleEvaluations"][0]["verdict"], "STRENGTHEN")
        self.assertEqual(report["principleEvaluations"][0]["statistics"]["violatedCount"], 2)
        self.assertEqual(report["principleEvaluations"][0]["suggestion"]["principleSetItemId"], 9)
        self.assertEqual(report["principleEvaluations"][1]["verdict"], "INSUFFICIENT_DATA")

    def test_thin_evidence_is_not_reported_as_a_cleared_principle(self):
        trades, analytics = self._reinforcement_inputs()
        # One applied trade that follows the rule: not enough to clear it.
        analytics["dailyPrices"] = [
            {"securityId": 7, "priceDate": "2026-07-01", "closePrice": 100.0, "day5Return": 0.02},
            {"securityId": 7, "priceDate": "2026-07-02", "closePrice": 101.0, "day5Return": 0.03},
        ]

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        evaluation = report["principleEvaluations"][0]

        self.assertEqual(evaluation["verdict"], "EARLY_SIGNAL")
        self.assertEqual(report["principleEvaluationSummary"]["earlySignalCount"], 1)
        self.assertEqual(report["principleEvaluationSummary"]["keepCount"], 0)
        self.assertEqual(evaluation["statistics"]["sampleShortfall"], 3)
        self.assertEqual(evaluation["statistics"]["evidenceStrength"], "PRELIMINARY")
        self.assertIsNone(evaluation["suggestion"])

    def test_small_outcome_samples_are_withheld_instead_of_averaged(self):
        trades, analytics = self._reinforcement_inputs()

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        outcomes = report["principleEvaluations"][0]["outcomes"]

        self.assertIsNone(outcomes["violated5dAveragePercent"])
        self.assertEqual(outcomes["minimumSampleCount"], 3)
        self.assertEqual(outcomes["sampleCounts"]["violated5d"], 2)

    def test_violation_rate_carries_a_lower_bound_so_two_trades_cannot_look_certain(self):
        trades, analytics = self._reinforcement_inputs()

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        statistics = report["principleEvaluations"][0]["statistics"]

        self.assertEqual(statistics["violationRatePercent"], 100.0)
        self.assertLess(statistics["violationRateLowerBoundPercent"], 100.0)

    def test_principle_set_diagnostics_report_uncovered_trades_and_missing_sections(self):
        trades, analytics = self._reinforcement_inputs()
        trades = trades + [{
            **self.trades[0],
            "tradeId": 13,
            "tradeSide": "SELL",
            "tradedAt": "2026-07-03T09:00:00",
            "appliedTradingDate": "2026-07-03",
        }]

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        diagnostics = report["principleSetDiagnostics"]

        self.assertEqual(diagnostics["principleCount"], 1)
        self.assertEqual(diagnostics["coverage"]["uncoveredTradeCount"], 1)
        self.assertIn(13, diagnostics["coverage"]["uncoveredTradeIds"])
        self.assertEqual(
            [item["sectionGroup"] for item in diagnostics["missingSections"]],
            ["SELL"],
        )
        self.assertEqual(diagnostics["missingSections"][0]["relatedTradeCount"], 1)

    def test_two_principles_bound_to_one_rule_are_reported_as_duplicates(self):
        trades, analytics = self._reinforcement_inputs()
        analytics["principleItems"] = analytics["principleItems"] + [{
            "principleSetItemId": 10,
            "principleText": "급등한 종목은 쳐다보지 않는다",
            "ruleJson": {"entry": {"max_5day_return": 0.12}},
            "sortOrder": 2,
        }]

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        duplicates = report["principleSetDiagnostics"]["duplicateRules"]

        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["targetRule"], "entry.max_5day_return")
        self.assertEqual(duplicates[0]["principleSetItemIds"], [9, 10])

    def test_performance_context_surfaces_benchmark_and_random_distribution(self):
        trades, analytics = self._reinforcement_inputs()
        analytics["randomDistribution"] = {
            "runCount": 4,
            "distributionPercent": [-3.0, 1.0, 4.0, 9.0],
            "medianReturnPercent": 2.5,
            "personalBotPercentile": 100.0,
        }
        analytics["benchmarks"] = [
            {"benchmark": "KOSPI", "returnPercent": 8.0, "method": "시장 지수 종가 기준"},
        ]
        analytics["securityContributions"] = [
            {"variantId": 1, "securityId": 7, "securityName": "테스트전자", "contributionAmount": -120000.0},
            {"variantId": 2, "securityId": 7, "securityName": "테스트전자", "contributionAmount": 50000.0},
        ]

        report = DeterministicReportAnalyzer().build(trades, self.participants, analytics)
        context = report["performanceContext"]

        self.assertEqual(context["luckCheck"]["actualUserPercentile"], 75.0)
        self.assertEqual(context["benchmarks"][0]["actualExcessPercentPoint"], -3.0)
        self.assertEqual(context["benchmarks"][0]["personalBotExcessPercentPoint"], 4.0)
        # Only the user's own contributions belong in the user's report.
        self.assertEqual(len(context["topSecurityContributions"]), 1)
        self.assertEqual(context["topSecurityContributions"][0]["sharePercent"], 100.0)

    def test_comparator_reference_principles_are_secondary_limited_and_non_duplicate(self):
        participants = self.participants + [{
            "variantId": 3,
            "variantType": "FAMOUS_STRATEGY",
            "cumulativeReturnPercent": 8.5,
        }]
        trades = self.trades + [{
            "tradeId": 31,
            "variantId": 3,
            "securityId": 8,
            "securityName": "비교종목",
            "tradeSide": "BUY",
            "tradedAt": "2026-07-03T09:00:00",
            "appliedTradingDate": "2026-07-03",
        }]
        analytics = dict(self.analytics)
        analytics["ruleSchema"] = {
            "selection": {
                "factor_weights": {"value": 0.5, "quality": 0.5},
                "min_passing_score": 70,
            },
            "audit": {
                "interpreted_principles": [{
                    "user_natural_text": "가치와 품질을 함께 본다",
                    "ai_mapped_rule": "selection.factor_weights",
                    "status": "CONFIRMED",
                }]
            },
        }
        analytics["principleItems"] = [{
            "principleSetItemId": 20,
            "principleText": "가치와 품질을 함께 본다",
            "ruleJson": {"selection": {"factor_weights": {"value": 0.5, "quality": 0.5}}},
            "sortOrder": 1,
        }]

        report = DeterministicReportAnalyzer().build(trades, participants, analytics)

        references = report["referencePrinciples"]
        self.assertEqual(len(references), 2)
        self.assertNotIn("REF_VALUE_QUALITY_SELECTION", [item["referenceId"] for item in references])
        self.assertEqual(references[0]["referenceId"], "REF_LIQUID_UNIVERSE")
        self.assertEqual(references[0]["comparisonEvidence"]["botAppliedTradeCount"], 1)
        self.assertEqual(references[0]["comparisonEvidence"]["simulationReturnPercent"], 8.5)
        self.assertFalse(references[0]["comparisonEvidence"]["performanceUsedForSelection"])
        self.assertEqual(references[0]["adoptionMode"], "REVIEW_ONLY")
        self.assertEqual(references[0]["recommendationOrigin"]["originLabel"], "비교 전략 참고")
        self.assertEqual(references[0]["recommendationOrigin"]["botName"], "우량 가치·품질 퀀트 봇")
        self.assertEqual(references[0]["recommendationOrigin"]["ruleSource"], "SYSTEM_STRATEGY_CONFIG")

    def test_one_trade_is_evaluated_against_all_applicable_principles(self):
        trade = {
            "tradeId": 201,
            "variantId": 1,
            "securityId": 7,
            "securityName": "다중매칭",
            "tradeSide": "BUY",
            "tradedAt": "2026-07-01T09:00:00",
            "appliedTradingDate": "2026-07-01",
        }
        analytics = {
            "dailyPrices": [{
                "securityId": 7,
                "priceDate": "2026-07-01",
                "closePrice": 100.0,
                "day5Return": 0.18,
                "tradingValue": 2_000_000_000,
            }],
            "ruleSchema": {
                "entry": {"max_5day_return": 0.10},
                "universe": {"min_daily_trading_value": 1_000_000_000},
                "audit": {"interpreted_principles": [
                    {
                        "user_natural_text": "급등 종목은 추격매수하지 않는다",
                        "ai_mapped_rule": "entry.max_5day_return",
                        "status": "CONFIRMED",
                    },
                    {
                        "user_natural_text": "거래대금 10억원 이상 종목만 산다",
                        "ai_mapped_rule": "universe.min_daily_trading_value",
                        "status": "CONFIRMED",
                    },
                ]},
            },
            "principleItems": [
                {
                    "principleSetItemId": 31,
                    "principleText": "급등 종목은 추격매수하지 않는다",
                    "ruleJson": {"entry": {"max_5day_return": 0.10}},
                    "sortOrder": 1,
                },
                {
                    "principleSetItemId": 32,
                    "principleText": "거래대금 10억원 이상 종목만 산다",
                    "ruleJson": {"universe": {"min_daily_trading_value": 1_000_000_000}},
                    "sortOrder": 2,
                },
            ],
        }

        report = DeterministicReportAnalyzer().build([trade], self.participants, analytics)

        review = report["decisionReviews"][0]
        matches = {item["principleSetItemId"]: item for item in review["principleMatches"]}
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[31]["judgment"], "VIOLATED")
        self.assertEqual(matches[31]["expectedAction"], "HOLD")
        self.assertEqual(matches[31]["evidence"]["actualValue"], 0.18)
        self.assertEqual(matches[32]["judgment"], "FOLLOWED")
        self.assertEqual(matches[32]["evidence"]["actualValue"], 2_000_000_000.0)
        self.assertEqual(review["matchedPrinciple"]["principleSetItemId"], 31)
        self.assertEqual(review["principleJudgment"], "VIOLATED")

    def test_unprovable_sell_principle_is_not_forced_into_followed_or_violated(self):
        trade = {
            "tradeId": 202,
            "variantId": 1,
            "securityId": 7,
            "securityName": "매도테스트",
            "tradeSide": "SELL",
            "tradedAt": "2026-07-01T09:00:00",
            "appliedTradingDate": "2026-07-01",
        }
        analytics = {
            "ruleSchema": {
                "exit": {"take_profit_rate": 0.20},
                "audit": {"interpreted_principles": [{
                    "user_natural_text": "20% 수익이면 매도한다",
                    "ai_mapped_rule": "exit.take_profit_rate",
                    "status": "CONFIRMED",
                }]},
            },
            "principleItems": [{
                "principleSetItemId": 33,
                "principleText": "20% 수익이면 매도한다",
                "ruleJson": {"exit": {"take_profit_rate": 0.20}},
                "sortOrder": 1,
            }],
        }

        report = DeterministicReportAnalyzer().build([trade], self.participants, analytics)

        match = report["decisionReviews"][0]["principleMatches"][0]
        self.assertEqual(match["applicability"], "INSUFFICIENT_DATA")
        self.assertEqual(match["judgment"], "INSUFFICIENT_DATA")

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
                prices.append({
                    "securityId": security_id,
                    "priceDate": price_date,
                    "closePrice": close_price,
                    "day5Return": 0.05 if security_id in {1, 2} else 0.20,
                })
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
                "entry": {"max_5day_return": 0.10},
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
