import unittest

from app.modules.simulation.engine.backtest import BacktestEngine
from app.modules.simulation.counterfactual import build_principle_counterfactuals
from app.modules.simulation.models import Position
from app.modules.simulation.engine.strategies import ActualUserStrategy


def _price_row(security_id, price_date, close_price, open_price=None, day5_return=0.0):
    return {
        "securityId": security_id,
        "priceDate": price_date,
        "openPrice": open_price if open_price is not None else close_price,
        "highPrice": close_price,
        "lowPrice": close_price,
        "closePrice": close_price,
        "volume": 100000,
        "tradingValue": 5_000_000_000,
        "marketCap": 100_000_000_000,
        "day5Return": day5_return,
    }


class PrincipleCounterfactualTests(unittest.TestCase):
    def setUp(self):
        self.securities_map = {
            7: {"securityId": 7, "securityCode": "000007", "securityName": "보유전자",
                "marketType": "KOSPI", "sectorName": "전기전자"},
            8: {"securityId": 8, "securityCode": "000008", "securityName": "급등화학",
                "marketType": "KOSPI", "sectorName": "화학"},
        }
        # The engine measures time-weighted return, so a counterfactual only
        # moves the number when it changes what the account was holding. Stock 7
        # rises and is held throughout; stock 8 is the forbidden buy and halves.
        self.daily_prices = [
            _price_row(7, "2026-07-01", 100.0),
            _price_row(7, "2026-07-02", 105.0),
            _price_row(7, "2026-07-03", 110.0),
            _price_row(7, "2026-07-06", 120.0),
            _price_row(8, "2026-07-01", 95.0),
            _price_row(8, "2026-07-02", 90.0),
            _price_row(8, "2026-07-03", 60.0),
            _price_row(8, "2026-07-06", 45.0),
        ]
        self.trading_days = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
        self.actual_trades = [{
            "tradeId": 501,
            "securityId": 8,
            "tradeSide": "BUY",
            "tradedAt": "2026-07-02T09:00:00",
            "quantity": 100.0,
            "unitPrice": 90.0,
            "transactionCostAmount": 0.0,
            "rationaleText": "급등해서 샀다",
        }]
        self.simulated_trades = [{
            "tradeId": 5001,
            "variantId": 1,
            "securityId": 8,
            "tradeSide": "BUY",
            "tradedAt": "2026-07-02T09:00:00",
            "originalTradedAt": "2026-07-02T09:00:00",
            "appliedTradingDate": "2026-07-02",
        }]
        self.initial_positions = {
            7: Position(
                security_id=7,
                security_code="000007",
                security_name="테스트전자",
                quantity=100.0,
                average_buy_price=100.0,
                current_price=100.0,
                acquired_date="2026-06-30",
            )
        }

    def _report(self, judgment="VIOLATED", action="BUY"):
        return {
            "principleEvaluations": [{
                "evaluationId": "PE_9_entry_max_5day_return",
                "principleSetItemId": 9,
                "principleText": "급등주를 추격매수하지 않는다",
                "targetRule": "entry.max_5day_return",
                "verdict": "STRENGTHEN",
            }],
            "decisionReviews": [{
                "tradeId": 5001,
                "action": action,
                "principleMatches": [{
                    "principleSetItemId": 9,
                    "targetRule": "entry.max_5day_return",
                    "judgment": judgment,
                }],
            }],
        }

    def _actual_baseline(self):
        """Replay every trade so the test compares against a real run, not a guess."""
        engine = BacktestEngine(
            simulation_run_id=0,
            period_start="2026-07-01",
            period_end="2026-07-06",
            initial_capital=10000.0,
            securities_map=self.securities_map,
            daily_prices=self.daily_prices,
        )
        engine.register_variant(
            1,
            ActualUserStrategy(1, self.actual_trades, trading_days=self.trading_days),
            initial_positions=self.initial_positions,
            initial_cash=0.0,
        )
        _, snapshots = engine.run()
        return (
            round(float(snapshots[-1].cumulative_return) * 100, 2),
            round(min(float(item.drawdown_rate) for item in snapshots) * 100, 2),
        )

    def _run(self, report, baseline=None):
        baseline_return, baseline_mdd = baseline or (-30.0, -30.0)
        return build_principle_counterfactuals(
            report,
            period_start="2026-07-01",
            period_end="2026-07-06",
            initial_capital=10000.0,
            securities_map=self.securities_map,
            daily_prices=self.daily_prices,
            trading_days=self.trading_days,
            actual_trades=self.actual_trades,
            simulated_trades=self.simulated_trades,
            initial_positions=self.initial_positions,
            baseline_return_percent=baseline_return,
            baseline_mdd_percent=baseline_mdd,
        )

    def test_removing_the_violating_buy_changes_the_measured_return(self):
        baseline_return, baseline_mdd = self._actual_baseline()
        report = self._report()

        completed = self._run(report, baseline=(baseline_return, baseline_mdd))
        counterfactual = report["principleEvaluations"][0]["counterfactual"]

        self.assertEqual(completed, 1)
        self.assertTrue(counterfactual["supported"])
        self.assertEqual(counterfactual["method"], "VIOLATING_BUY_ORDERS_REMOVED")
        self.assertEqual(counterfactual["removedTradeCount"], 1)
        self.assertEqual(counterfactual["removedTradeIds"], [5001])
        self.assertEqual(counterfactual["baselineReturnPercent"], baseline_return)
        # Skipping a buy into a falling stock has to move the result, otherwise
        # the replay never actually dropped the order.
        self.assertNotEqual(
            counterfactual["counterfactualReturnPercent"], baseline_return
        )
        self.assertEqual(
            counterfactual["differencePercentPoint"],
            round(counterfactual["counterfactualReturnPercent"] - baseline_return, 2),
        )
        self.assertIn("disclaimer", counterfactual)
        self.assertEqual(
            report["generationMetadata"]["counterfactualSource"],
            "DETERMINISTIC_BACKTEST_REPLAY",
        )

    def test_the_source_trade_is_the_only_one_removed(self):
        self.actual_trades.append({
            "tradeId": 502,
            "securityId": 8,
            "tradeSide": "BUY",
            "tradedAt": "2026-07-03T09:00:00",
            "quantity": 50.0,
            "unitPrice": 60.0,
            "transactionCostAmount": 0.0,
        })
        report = self._report()

        self._run(report)

        # Only the flagged 07-02 buy is dropped; the unflagged 07-03 buy stays.
        self.assertEqual(
            report["principleEvaluations"][0]["counterfactual"]["removedTradeCount"], 1
        )

    def test_a_missed_sell_is_refused_instead_of_guessed(self):
        report = self._report(action="SELL")

        completed = self._run(report)
        counterfactual = report["principleEvaluations"][0]["counterfactual"]

        self.assertEqual(completed, 0)
        self.assertFalse(counterfactual["supported"])
        self.assertEqual(counterfactual["reasonCode"], "SELL_SIDE_NOT_SUPPORTED")

    def test_a_principle_with_no_violation_reports_nothing_to_compare(self):
        report = self._report(judgment="FOLLOWED")

        completed = self._run(report)
        counterfactual = report["principleEvaluations"][0]["counterfactual"]

        self.assertEqual(completed, 0)
        self.assertFalse(counterfactual["supported"])
        self.assertEqual(counterfactual["reasonCode"], "NO_VIOLATION")

    def test_an_unmatched_source_trade_is_reported_rather_than_silently_skipped(self):
        self.simulated_trades[0]["originalTradedAt"] = "2026-07-02T09:00:00"
        self.actual_trades[0]["tradedAt"] = "2026-07-05T09:00:00"
        report = self._report()

        completed = self._run(report)
        counterfactual = report["principleEvaluations"][0]["counterfactual"]

        self.assertEqual(completed, 0)
        self.assertFalse(counterfactual["supported"])
        self.assertEqual(counterfactual["reasonCode"], "SOURCE_TRADE_NOT_MATCHED")


if __name__ == "__main__":
    unittest.main()
