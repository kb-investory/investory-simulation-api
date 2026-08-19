import unittest

from app.modules.simulation.rule_schema import InvestmentBotStrategySchema
from app.modules.simulation.strengthen_spec import (
    RULE_STRENGTHEN_SPEC,
    build_strengthen_proposal,
    format_value,
)


def _leaf_rule_paths() -> set:
    """Every dotted rule path the compiled schema can actually carry."""
    schema = InvestmentBotStrategySchema().to_dict()
    paths = set()
    for section, values in schema.items():
        if section == "audit" or not isinstance(values, dict):
            continue
        for field in values:
            paths.add(f"{section}.{field}")
    return paths


class StrengthenSpecCoverageTests(unittest.TestCase):
    def test_every_executable_rule_path_has_a_strengthening(self):
        missing = sorted(_leaf_rule_paths() - set(RULE_STRENGTHEN_SPEC))
        self.assertEqual(missing, [], f"강화 사양이 없는 규칙: {missing}")

    def test_no_spec_targets_a_rule_the_schema_does_not_have(self):
        extra = sorted(set(RULE_STRENGTHEN_SPEC) - _leaf_rule_paths())
        self.assertEqual(extra, [])

    def test_every_rule_returns_a_proposal_so_no_verdict_is_left_without_a_remedy(self):
        schema = InvestmentBotStrategySchema().to_dict()
        for target_rule in sorted(RULE_STRENGTHEN_SPEC):
            section, field = target_rule.split(".", 1)
            current = schema[section][field]
            with self.subTest(rule=target_rule):
                proposal = build_strengthen_proposal(target_rule, current, [])
                self.assertIsNotNone(proposal)
                self.assertTrue(proposal["description"])
                self.assertIn(
                    proposal["changeType"],
                    {
                        "THRESHOLD_ADJUSTMENT",
                        "SWITCH_TO_STRICT",
                        "CONDITION_TIGHTENED",
                        "ENFORCEMENT_REINFORCEMENT",
                    },
                )

    def test_an_unknown_rule_path_returns_nothing_rather_than_a_guess(self):
        self.assertIsNone(build_strengthen_proposal("entry.made_up_rule", 1.0, []))


class StrengthenValueTests(unittest.TestCase):
    def test_an_upper_bound_rule_tightens_toward_the_followed_trades(self):
        # Followed buys sat at 2-8%; the stated ceiling of 15% is loose.
        proposal = build_strengthen_proposal(
            "entry.max_5day_return", 0.15, [0.02, 0.04, 0.06, 0.08]
        )

        self.assertEqual(proposal["changeType"], "THRESHOLD_ADJUSTMENT")
        self.assertEqual(proposal["valueBasis"], "FOLLOWED_TRADE_DISTRIBUTION")
        self.assertLess(proposal["proposedValue"], 0.15)
        self.assertGreaterEqual(proposal["proposedValue"], 0.05)

    def test_a_lower_bound_rule_tightens_upward(self):
        proposal = build_strengthen_proposal(
            "universe.min_daily_trading_value",
            1_000_000_000.0,
            [4_000_000_000.0, 5_000_000_000.0, 9_000_000_000.0],
        )

        self.assertEqual(proposal["strengthDirection"], "INCREASE")
        self.assertGreater(proposal["proposedValue"], 1_000_000_000.0)

    def test_evidence_that_would_loosen_the_rule_is_not_used(self):
        # Followed values above the current ceiling mean the stored threshold and
        # the judged trades disagree. Raising the ceiling to match would weaken
        # the principle, so the bounded step is taken instead.
        proposal = build_strengthen_proposal("entry.max_5day_return", 0.06, [0.20, 0.25])

        self.assertEqual(proposal["valueBasis"], "FIXED_STEP_FROM_CURRENT")
        self.assertLess(proposal["proposedValue"], 0.06)

    def test_a_single_followed_trade_is_not_treated_as_a_distribution(self):
        proposal = build_strengthen_proposal("entry.max_5day_return", 0.15, [0.02])

        self.assertEqual(proposal["valueBasis"], "FIXED_STEP_FROM_CURRENT")

    def test_a_value_already_at_the_limit_becomes_an_enforcement_reminder(self):
        proposal = build_strengthen_proposal("entry.max_5day_return", 0.05, [])

        self.assertEqual(proposal["changeType"], "ENFORCEMENT_REINFORCEMENT")
        self.assertEqual(proposal["proposedValue"], 0.05)

    def test_a_proposal_never_leaves_the_allowed_band(self):
        proposal = build_strengthen_proposal(
            "portfolio.max_single_position_weight", 0.06, [0.01, 0.01, 0.01]
        )

        self.assertGreaterEqual(proposal["proposedValue"], proposal["allowedMinimum"])
        self.assertLessEqual(proposal["proposedValue"], proposal["allowedMaximum"])

    def test_an_integer_rule_stays_an_integer(self):
        proposal = build_strengthen_proposal("portfolio.max_position_count", 5, [])

        self.assertEqual(proposal["proposedValue"], float(int(proposal["proposedValue"])))
        self.assertLess(proposal["proposedValue"], 5)

    def test_a_boolean_rule_switches_to_its_strict_side(self):
        off = build_strengthen_proposal("universe.exclude_halted", False, [])
        self.assertEqual(off["changeType"], "SWITCH_TO_STRICT")
        self.assertIs(off["proposedValue"], True)

        already_on = build_strengthen_proposal("universe.exclude_halted", True, [])
        self.assertEqual(already_on["changeType"], "ENFORCEMENT_REINFORCEMENT")

    def test_allowing_extra_buys_is_strict_in_the_off_direction(self):
        proposal = build_strengthen_proposal("additional_buy.allowed", True, [])

        self.assertEqual(proposal["changeType"], "SWITCH_TO_STRICT")
        self.assertIs(proposal["proposedValue"], False)

    def test_an_enum_rule_climbs_one_rung(self):
        proposal = build_strengthen_proposal("entry.moving_average_condition", "NONE", [])

        self.assertEqual(proposal["changeType"], "CONDITION_TIGHTENED")
        self.assertEqual(proposal["proposedValue"], "ABOVE_MA5")

        top = build_strengthen_proposal("entry.moving_average_condition", "MA5_ABOVE_MA20", [])
        self.assertEqual(top["changeType"], "ENFORCEMENT_REINFORCEMENT")

    def test_a_rule_with_no_single_number_offers_an_action_instead(self):
        proposal = build_strengthen_proposal("selection.factor_weights", {"value": 0.5}, [])

        self.assertEqual(proposal["changeType"], "ENFORCEMENT_REINFORCEMENT")
        self.assertEqual(proposal["valueBasis"], "NOT_TUNABLE")
        self.assertIn("확인", proposal["description"])


class ValueFormattingTests(unittest.TestCase):
    def test_values_are_rendered_the_way_a_person_reads_them(self):
        self.assertEqual(format_value(0.1, "RATE"), "10%")
        self.assertEqual(format_value(-0.1, "SIGNED_RATE"), "-10%")
        self.assertEqual(format_value(50_000_000_000, "KRW"), "500억원")
        self.assertEqual(format_value(5, "COUNT"), "5개")
        self.assertEqual(format_value(90, "DAYS"), "90일")
        self.assertEqual(format_value(70.0, "SCORE"), "70점")
        self.assertEqual(format_value(True, "BOOLEAN"), "예")
        self.assertEqual(format_value(None, "RATE"), "미설정")


if __name__ == "__main__":
    unittest.main()
