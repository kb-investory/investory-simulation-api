import unittest

from app.modules.simulation.report_analysis import (
    DeterministicReportAnalyzer,
    _principle_catalog,
    _resolve_current_value,
)


class RuleValueSourceTests(unittest.TestCase):
    def test_a_user_confirmation_outranks_every_other_source(self):
        value, source = _resolve_current_value(
            "entry.max_5day_return",
            {"entry": {"max_5day_return": 0.12}},
            {"entry": {"max_5day_return": 0.15}},
            {"entry.max_5day_return": 0.08},
        )

        self.assertEqual(value, 0.08)
        self.assertEqual(source, "USER_CONFIRMED")

    def test_the_principle_rule_json_outranks_the_compiled_schema(self):
        value, source = _resolve_current_value(
            "entry.max_5day_return",
            {"entry": {"max_5day_return": 0.12}},
            {"entry": {"max_5day_return": 0.15}},
            {},
        )

        self.assertEqual(value, 0.12)
        self.assertEqual(source, "PRINCIPLE_RULE_JSON")

    def test_a_value_only_the_compiler_produced_is_marked_as_inferred(self):
        value, source = _resolve_current_value(
            "entry.max_5day_return", {}, {"entry": {"max_5day_return": 0.15}}, {}
        )

        self.assertEqual(value, 0.15)
        self.assertEqual(source, "AI_INFERRED")

    def test_a_rule_with_no_value_anywhere_is_missing_not_zero(self):
        value, source = _resolve_current_value("entry.max_5day_return", {}, {}, {})

        self.assertIsNone(value)
        self.assertEqual(source, "MISSING")

    def test_confirmations_reach_the_catalog_from_analytics(self):
        analytics = {
            "principleItems": [{
                "principleSetItemId": 9,
                "principleText": "급등주를 추격매수하지 않는다",
                "ruleJson": {},
                "sortOrder": 1,
            }],
            "ruleConfirmations": [
                {"targetRule": "entry.max_5day_return", "confirmedValue": 0.09},
            ],
        }
        rule_schema = {
            "entry": {"max_5day_return": 0.15},
            "audit": {"interpreted_principles": [{
                "user_natural_text": "급등주를 추격매수하지 않는다",
                "ai_mapped_rule": "entry.max_5day_return",
                "status": "CONFIRMED",
            }]},
        }

        catalog = _principle_catalog(analytics, rule_schema)

        self.assertEqual(catalog[0]["currentValue"], 0.09)
        self.assertEqual(catalog[0]["valueSource"], "USER_CONFIRMED")


class InferredThresholdVerdictTests(unittest.TestCase):
    def setUp(self):
        self.participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": -5.0},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": 3.0},
        ]
        self.trades, prices = [], []
        for index, day in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"], start=1):
            self.trades.append({
                "tradeId": 100 + index, "variantId": 1, "securityId": 7,
                "securityName": "테스트", "tradeSide": "BUY",
                "tradedAt": f"{day}T09:00:00", "appliedTradingDate": day,
            })
            prices.append({
                "securityId": 7, "priceDate": day, "closePrice": 100.0, "day5Return": 0.30,
            })
        self.prices = prices

    def _analytics(self, principle_rule_json, confirmations=None):
        return {
            "ruleSchema": {
                "entry": {"max_5day_return": 0.15},
                "audit": {"interpreted_principles": [{
                    "user_natural_text": "급등주를 추격매수하지 않는다",
                    "ai_mapped_rule": "entry.max_5day_return",
                    "status": "CONFIRMED",
                }]},
            },
            "principleItems": [{
                "principleSetItemId": 9,
                "principleText": "급등주를 추격매수하지 않는다",
                "ruleJson": principle_rule_json,
                "sortOrder": 1,
            }],
            "ruleConfirmations": confirmations or [],
            "dailyPrices": self.prices,
        }

    def test_repeated_violations_of_a_guessed_threshold_ask_before_tightening(self):
        report = DeterministicReportAnalyzer().build(
            self.trades, self.participants, self._analytics({})
        )
        evaluation = report["principleEvaluations"][0]

        self.assertEqual(evaluation["valueSource"], "AI_INFERRED")
        self.assertEqual(evaluation["verdict"], "CONFIRM_THRESHOLD")
        self.assertEqual(report["principleEvaluationSummary"]["confirmThresholdCount"], 1)
        self.assertEqual(report["principleEvaluationSummary"]["strengthenCount"], 0)
        # No tightening is proposed against a bar the user never set.
        self.assertIsNone(evaluation["suggestion"])
        self.assertIn("AI가 추정한 값", evaluation["evaluationReason"])

    def test_confirming_the_threshold_lets_the_strengthening_proceed(self):
        analytics = self._analytics(
            {},
            confirmations=[{"targetRule": "entry.max_5day_return", "confirmedValue": 0.15}],
        )

        report = DeterministicReportAnalyzer().build(self.trades, self.participants, analytics)
        evaluation = report["principleEvaluations"][0]

        self.assertEqual(evaluation["valueSource"], "USER_CONFIRMED")
        self.assertEqual(evaluation["verdict"], "STRENGTHEN")
        self.assertIsNotNone(evaluation["suggestion"])

    def test_a_threshold_the_user_wrote_into_the_principle_needs_no_confirmation(self):
        report = DeterministicReportAnalyzer().build(
            self.trades,
            self.participants,
            self._analytics({"entry": {"max_5day_return": 0.15}}),
        )
        evaluation = report["principleEvaluations"][0]

        self.assertEqual(evaluation["valueSource"], "PRINCIPLE_RULE_JSON")
        self.assertEqual(evaluation["verdict"], "STRENGTHEN")

    def test_an_inferred_threshold_without_repeated_violations_is_judged_normally(self):
        analytics = self._analytics({})
        # All three buys sit inside the bar, so there is nothing to confirm.
        for price in analytics["dailyPrices"]:
            price["day5Return"] = 0.01

        report = DeterministicReportAnalyzer().build(self.trades, self.participants, analytics)

        self.assertEqual(report["principleEvaluations"][0]["verdict"], "EARLY_SIGNAL")


if __name__ == "__main__":
    unittest.main()
