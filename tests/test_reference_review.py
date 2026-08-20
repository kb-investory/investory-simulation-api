import unittest

from app.modules.simulation.report_analysis import _build_reference_review
from app.modules.simulation.strategy_catalog import VALUE_QUALITY_STRATEGY


def _participants(strategy_return=15.0):
    return [
        {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": 3.0},
        {"variantId": VALUE_QUALITY_STRATEGY["variantId"],
         "variantType": VALUE_QUALITY_STRATEGY["variantType"],
         "cumulativeReturnPercent": strategy_return},
    ]


def _catalog(*target_rules):
    return [
        {"principleSetItemId": index, "principleText": f"원칙 {index}", "targetRule": rule}
        for index, rule in enumerate(target_rules, 1)
    ]


class ReferenceGapTests(unittest.TestCase):
    def test_areas_the_user_has_no_rule_for_are_the_recommendation_basis(self):
        # Only an exit rule, so selection and universe are wide open.
        review = _build_reference_review(
            _catalog("exit.stop_loss_rate"), [], _participants()
        )
        sections = {item["section"] for item in review["missingSections"]}

        self.assertIn("selection", sections)
        self.assertIn("universe", sections)
        self.assertNotIn("exit", sections)
        self.assertEqual(review["missingSectionCount"], len(review["missingSections"]))

    def test_a_covered_area_is_not_offered_back_to_the_user(self):
        review = _build_reference_review(
            _catalog("selection.min_passing_score", "universe.min_market_cap",
                     "exit.take_profit_rate", "entry.max_5day_return",
                     "portfolio.max_single_position_weight"),
            [], _participants(),
        )

        self.assertEqual(review["missingSections"], [])
        self.assertEqual(review["missingSectionCount"], 0)

    def test_each_missing_area_names_the_rules_the_strategy_applies_there(self):
        review = _build_reference_review(_catalog("exit.stop_loss_rate"), [], _participants())
        selection = next(i for i in review["missingSections"] if i["section"] == "selection")

        self.assertEqual(
            selection["strategyRules"],
            ["selection.factor_weights", "selection.min_passing_score"],
        )
        self.assertTrue(selection["sectionLabel"])


class ReturnIsNotEvidenceTests(unittest.TestCase):
    def test_the_return_is_carried_but_flagged_as_not_the_reason(self):
        review = _build_reference_review(_catalog("exit.stop_loss_rate"), [], _participants(15.0))

        self.assertEqual(review["referenceReturnPercent"], 15.0)
        self.assertIs(review["returnUsedAsEvidence"], False)
        self.assertIn("근거가 되지 않습니다", review["disclaimer"])

    def test_a_losing_quarter_surfaces_the_same_gaps(self):
        # The gap is structural, so it does not appear or vanish with the return.
        winning = _build_reference_review(_catalog("exit.stop_loss_rate"), [], _participants(15.0))
        losing = _build_reference_review(_catalog("exit.stop_loss_rate"), [], _participants(-9.0))

        self.assertEqual(winning["missingSections"], losing["missingSections"])
        self.assertEqual(losing["referenceReturnPercent"], -9.0)

    def test_nothing_here_can_be_applied_automatically(self):
        review = _build_reference_review(_catalog(), [], _participants())

        self.assertEqual(review["adoptionMode"], "REVIEW_ONLY")
        self.assertEqual(review["ruleSource"], "SYSTEM_STRATEGY_CONFIG")
        self.assertEqual(review["strategyName"], VALUE_QUALITY_STRATEGY["strategyName"])


if __name__ == "__main__":
    unittest.main()
