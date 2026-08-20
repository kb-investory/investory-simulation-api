import unittest

from app.modules.simulation.report_analysis import _build_divergence_review


class DivergenceScoringTests(unittest.TestCase):
    """Who was right at each split, judged by the move that followed it."""

    def _review(self, moments, decision_reviews=None, bot_trades=None):
        analytics = {
            "divergenceMoments": moments,
            "securitySnapshots": [{"securityId": 7, "securityName": "테스트전자"}],
        }
        return _build_divergence_review(
            analytics,
            bot_trades or [],
            decision_reviews or [],
            {"principleBotGapPercentPoint": 7.0},
        )

    def _moment(self, user, bot, return_percent):
        return {
            "date": "2026-07-01", "securityId": 7,
            "actualUserActions": [user], "personalBotActions": [bot],
            "subsequent5TradingDayReturnPercent": return_percent,
        }

    def test_buying_into_a_fall_while_the_bot_stayed_out_favours_the_bot(self):
        review = self._review([self._moment("BUY", "HOLD", -8.0)])
        moment = review["moments"][0]

        self.assertEqual(moment["userScore"], -8.0)
        self.assertEqual(moment["botScore"], 0.0)
        self.assertEqual(moment["betterSide"], "PERSONAL_BOT")
        self.assertEqual(review["botBetterCount"], 1)

    def test_buying_into_a_rise_while_the_bot_stayed_out_favours_the_user(self):
        review = self._review([self._moment("BUY", "HOLD", 6.0)])

        self.assertEqual(review["moments"][0]["betterSide"], "ACTUAL_USER")
        self.assertEqual(review["userBetterCount"], 1)

    def test_selling_before_a_fall_counts_as_the_right_call(self):
        # A sell is scored by the drop it avoided, not by the raw return.
        review = self._review([self._moment("SELL", "HOLD", -9.0)])
        moment = review["moments"][0]

        self.assertEqual(moment["userScore"], 9.0)
        self.assertEqual(moment["betterSide"], "ACTUAL_USER")

    def test_the_bot_buying_into_a_rise_beats_the_user_holding(self):
        review = self._review([self._moment("HOLD", "BUY", 11.0)])

        self.assertEqual(review["moments"][0]["betterSide"], "PERSONAL_BOT")

    def test_a_split_with_no_price_data_is_left_undetermined(self):
        review = self._review([self._moment("BUY", "HOLD", None)])
        moment = review["moments"][0]

        self.assertIsNone(moment["userScore"])
        self.assertEqual(moment["betterSide"], "UNKNOWN")
        self.assertEqual(review["undeterminedCount"], 1)
        self.assertEqual(review["botBetterCount"], 0)


class DivergenceContextTests(unittest.TestCase):
    def test_the_moment_carries_why_each_side_acted(self):
        moments = [{
            "date": "2026-07-01", "securityId": 7,
            "actualUserActions": ["BUY"], "personalBotActions": ["HOLD"],
            "subsequent5TradingDayReturnPercent": -8.0,
        }]
        decision_reviews = [{
            "tradedAt": "2026-07-01T09:00:00", "securityId": 7,
            "principleMatches": [
                {"judgment": "VIOLATED", "principleSetItemId": 9,
                 "principleText": "급등주를 추격매수하지 않는다",
                 "targetRule": "entry.max_5day_return",
                 "reason": "최근 5거래일 수익률이 기준을 벗어났습니다."},
                {"judgment": "FOLLOWED", "principleSetItemId": 10,
                 "principleText": "거래대금 기준", "targetRule": "universe.min_daily_trading_value"},
            ],
        }]
        bot_trades = [{
            "variantId": 2, "securityId": 7, "appliedTradingDate": "2026-07-01",
            "decisionReason": "진입 조건을 충족하지 못해 매수하지 않았습니다.",
        }]

        review = _build_divergence_review(
            {"divergenceMoments": moments,
             "securitySnapshots": [{"securityId": 7, "securityName": "테스트전자"}]},
            bot_trades, decision_reviews, {"principleBotGapPercentPoint": 7.0},
        )
        moment = review["moments"][0]

        self.assertEqual(moment["securityName"], "테스트전자")
        self.assertEqual(moment["botReason"], "진입 조건을 충족하지 못해 매수하지 않았습니다.")
        # Only the broken principles explain the split; followed ones do not.
        self.assertEqual(len(moment["violatedPrinciples"]), 1)
        self.assertEqual(moment["violatedPrinciples"][0]["principleSetItemId"], 9)

    def test_the_section_refuses_to_claim_it_explains_the_whole_gap(self):
        review = _build_divergence_review(
            {"divergenceMoments": [], "securitySnapshots": []},
            [], [], {"principleBotGapPercentPoint": 7.0},
        )

        self.assertEqual(review["gapPercentPoint"], 7.0)
        self.assertEqual(review["momentCount"], 0)
        self.assertIn("모두 설명한다는 뜻은 아닙니다", review["attributionNote"])
        self.assertEqual(review["measurementPeriod"], "5_TRADING_DAYS_AFTER_DIVERGENCE")


if __name__ == "__main__":
    unittest.main()
