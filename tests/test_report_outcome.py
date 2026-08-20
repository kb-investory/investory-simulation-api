import unittest

from app.modules.simulation.analytics.report_analysis import _build_outcome, _outcome_branch


def _ranked(**returns_by_type):
    """Build a ranking payload; every participant trades unless told otherwise."""
    items = []
    for index, (variant_type, value) in enumerate(returns_by_type.items(), 1):
        if isinstance(value, tuple):
            percent, trade_count = value
        else:
            percent, trade_count = value, 5
        items.append({
            "variantId": index,
            "variantType": variant_type,
            "variantName": variant_type,
            "cumulativeReturnPercent": percent,
            "mddPercent": -5.0,
            "tradeCount": trade_count,
        })
    items.sort(key=lambda item: item["cumulativeReturnPercent"], reverse=True)
    for position, item in enumerate(items, 1):
        item["rank"] = position
    return items


def _summary(violated=0, assessed=10, total=20):
    return {
        "violatedCount": violated,
        "followedCount": assessed - violated,
        "assessedTradeCount": assessed,
        "totalTradeCount": total,
    }


class OutcomeBranchTests(unittest.TestCase):
    def test_a_first_place_reached_while_breaking_the_rules_is_not_praised(self):
        ranked = _ranked(ACTUAL_USER=10.0, PERSONAL_BOT=4.0, RANDOM_BOT=1.0)

        self.assertEqual(
            _outcome_branch(ranked, _summary(violated=5)),
            "USER_AHEAD_LUCKY",
        )

    def test_a_first_place_with_the_rules_kept_is_praised(self):
        ranked = _ranked(ACTUAL_USER=10.0, PERSONAL_BOT=4.0, RANDOM_BOT=1.0)

        self.assertEqual(
            _outcome_branch(ranked, _summary(violated=0)),
            "USER_AHEAD_DISCIPLINED",
        )

    def test_one_stray_violation_does_not_flip_a_good_run_into_luck(self):
        ranked = _ranked(ACTUAL_USER=10.0, PERSONAL_BOT=4.0)

        self.assertEqual(
            _outcome_branch(ranked, _summary(violated=1)),
            "USER_AHEAD_DISCIPLINED",
        )

    def test_random_trading_winning_overrides_every_other_story(self):
        # Even with the user second and disciplined, this period proves nothing.
        ranked = _ranked(RANDOM_BOT=20.0, ACTUAL_USER=10.0, PERSONAL_BOT=4.0)

        self.assertEqual(_outcome_branch(ranked, _summary()), "MARKET_LUCK")

    def test_the_principle_bot_winning_points_at_the_divergence(self):
        ranked = _ranked(PERSONAL_BOT=12.0, ACTUAL_USER=5.0, RANDOM_BOT=1.0)

        self.assertEqual(_outcome_branch(ranked, _summary()), "BOT_AHEAD")

    def test_the_comparison_strategy_winning_is_its_own_branch(self):
        ranked = _ranked(FAMOUS_STRATEGY=15.0, ACTUAL_USER=5.0, PERSONAL_BOT=4.0)

        self.assertEqual(_outcome_branch(ranked, _summary()), "REFERENCE_AHEAD")

    def test_a_principle_bot_that_never_traded_cannot_be_ranked(self):
        # Its 0% line is an absence of result, not a result.
        ranked = _ranked(ACTUAL_USER=5.0, PERSONAL_BOT=(0.0, 0))

        self.assertEqual(_outcome_branch(ranked, _summary()), "INCONCLUSIVE")

    def test_an_unknown_winner_does_not_borrow_another_branch_story(self):
        # A blank or future variantType used to fall through into
        # REFERENCE_AHEAD, telling the user a comparison strategy won when
        # nothing of the sort had been established.
        for unknown in ("", "SOME_NEW_BOT"):
            with self.subTest(variantType=unknown):
                ranked = _ranked(ACTUAL_USER=5.0, PERSONAL_BOT=4.0)
                ranked.insert(0, {
                    "variantId": 9, "variantType": unknown, "variantName": unknown,
                    "cumulativeReturnPercent": 30.0, "mddPercent": -1.0,
                    "tradeCount": 3, "rank": 1,
                })
                self.assertEqual(_outcome_branch(ranked, _summary()), "INCONCLUSIVE")

    def test_no_participants_at_all_is_inconclusive(self):
        self.assertEqual(_outcome_branch([], _summary()), "INCONCLUSIVE")


class OutcomePayloadTests(unittest.TestCase):
    def _build(self, participants, trades, summary, coverage=None):
        return _build_outcome(
            participants, trades, summary, coverage or {"uncoveredTradeCount": 0}
        )

    def test_the_outcome_carries_the_ranking_and_where_to_look_next(self):
        participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "variantName": "실제 나",
             "cumulativeReturnPercent": 5.0, "mddPercent": -12.0},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "variantName": "내 원칙봇",
             "cumulativeReturnPercent": 12.0, "mddPercent": -6.0},
        ]
        trades = [{"variantId": 1}, {"variantId": 1}, {"variantId": 2}]

        outcome = self._build(
            participants, trades, _summary(violated=3, assessed=8, total=20),
            coverage={"uncoveredTradeCount": 12},
        )

        self.assertEqual(outcome["branch"], "BOT_AHEAD")
        self.assertEqual(outcome["winnerVariantType"], "PERSONAL_BOT")
        self.assertEqual(outcome["focusSection"], "DIVERGENCE")
        self.assertEqual([item["variantType"] for item in outcome["ranking"]],
                         ["PERSONAL_BOT", "ACTUAL_USER"])
        self.assertEqual(outcome["ranking"][0]["rank"], 1)
        self.assertEqual(outcome["evidence"]["principleBotGapPercentPoint"], 7.0)
        self.assertEqual(outcome["evidence"]["uncoveredTradeCount"], 12)
        self.assertTrue(outcome["headline"])
        self.assertTrue(outcome["detail"])

    def test_trade_counts_come_from_the_run_not_the_summary(self):
        participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": 5.0},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": 12.0},
        ]
        trades = [{"variantId": 1}, {"variantId": 1}, {"variantId": 1}]

        outcome = self._build(participants, trades, _summary())

        counts = {item["variantType"]: item["tradeCount"] for item in outcome["ranking"]}
        self.assertEqual(counts, {"ACTUAL_USER": 3, "PERSONAL_BOT": 0})
        # The bot placed no orders, so the report refuses to declare it the winner.
        self.assertEqual(outcome["branch"], "INCONCLUSIVE")

    def test_every_branch_has_copy_and_a_focus_section(self):
        from app.modules.simulation.analytics.report_analysis import OUTCOME_COPY

        for branch, (headline, detail, focus) in OUTCOME_COPY.items():
            with self.subTest(branch=branch):
                self.assertTrue(headline.strip())
                self.assertTrue(detail.strip())
                self.assertIn(focus, {
                    "COVERAGE", "PRINCIPLE_EVALUATIONS", "DIVERGENCE",
                    "REFERENCE_PRINCIPLES", "PERFORMANCE_CONTEXT",
                })


if __name__ == "__main__":
    unittest.main()


class MarketLuckContextTests(unittest.TestCase):
    """원숭이봇이 1위인 회차는 분포로 설명한다."""

    def _context(self, distribution_percent, actual=3.0):
        from app.modules.simulation.analytics.report_analysis import _build_performance_context
        analytics = {
            "randomDistribution": {
                "runCount": len(distribution_percent),
                "distributionPercent": sorted(distribution_percent),
                "medianReturnPercent": 4.0,
                "lowerQuartileReturnPercent": 1.0,
                "upperQuartileReturnPercent": 9.0,
                "minimumReturnPercent": min(distribution_percent),
                "maximumReturnPercent": max(distribution_percent),
                "personalBotPercentile": 80.0,
            },
        }
        participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": actual},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": 6.0},
        ]
        return _build_performance_context(analytics, participants)["luckCheck"]

    def test_a_generous_period_is_stated_as_how_many_random_runs_profited(self):
        luck = self._context([-2.0, 1.0, 3.0, 5.0, 8.0, 12.0, 15.0, 20.0])

        self.assertEqual(luck["profitableRunPercent"], 87.5)
        self.assertIn("87.5%가 수익을 냈습니다", luck["periodSummary"])

    def test_a_harsh_period_is_worded_the_other_way_round(self):
        luck = self._context([-20.0, -12.0, -8.0, -5.0, -3.0, -1.0, 2.0, 4.0])

        self.assertEqual(luck["profitableRunPercent"], 25.0)
        self.assertIn("수익을 낸 경우는 25%", luck["periodSummary"])

    def test_the_spread_is_carried_so_a_screen_can_place_the_user_in_it(self):
        luck = self._context([-2.0, 1.0, 3.0, 5.0, 8.0, 12.0, 15.0, 20.0])

        for key in ("minimumReturnPercent", "lowerQuartileReturnPercent",
                    "medianReturnPercent", "upperQuartileReturnPercent",
                    "maximumReturnPercent", "actualUserPercentile"):
            self.assertIsNotNone(luck[key], key)

    def test_the_market_luck_branch_looks_at_the_distribution_not_the_coverage(self):
        from app.modules.simulation.analytics.report_analysis import OUTCOME_COPY

        self.assertEqual(OUTCOME_COPY["MARKET_LUCK"][2], "PERFORMANCE_CONTEXT")
