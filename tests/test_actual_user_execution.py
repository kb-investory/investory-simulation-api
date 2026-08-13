import unittest

from app.modules.simulation.backtest import BacktestEngine
from app.modules.simulation.strategies import ActualUserStrategy


class ActualUserExecutionTests(unittest.TestCase):
    def test_actual_trade_uses_database_fill_and_maps_weekend_to_next_day(self):
        prices = [
            {
                "securityId": 1,
                "priceDate": "2026-01-05",
                "openPrice": 200.0,
                "closePrice": 210.0,
            }
        ]
        strategy = ActualUserStrategy(
            1,
            [
                {
                    "tradeId": 10,
                    "securityId": 1,
                    "tradeSide": "BUY",
                    "quantity": 2,
                    "unitPrice": 123.0,
                    "transactionCostAmount": 7.0,
                    "tradedAt": "2026-01-03T12:00:00Z",
                    "rationaleText": "실적 발표 전 수주 증가를 확인해 매수",
                }
            ],
            trading_days=["2026-01-05"],
        )
        engine = BacktestEngine(1, "2026-01-05", "2026-01-05", 1000, {1: {}}, prices)
        engine.register_variant(1, strategy)

        trades, snapshots = engine.run()

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].unit_price, 123.0)
        self.assertEqual(trades[0].transaction_cost_amount, 7.0)
        self.assertEqual(trades[0].traded_at, "2026-01-03T12:00:00Z")
        self.assertEqual(trades[0].applied_trading_date, "2026-01-05")
        self.assertEqual(trades[0].execution_policy, "DATABASE_ACTUAL_FILL")
        self.assertEqual(trades[0].decision_reason, "실적 발표 전 수주 증가를 확인해 매수 (원 거래일 2026-01-03, 반영 거래일 2026-01-05)")
        self.assertEqual(snapshots[0].holdings_market_value, 420.0)

    def test_missing_actual_rationale_is_explicit_and_not_invented(self):
        strategy = ActualUserStrategy(1, [{
            "tradeId": 11,
            "securityId": 1,
            "tradeSide": "BUY",
            "quantity": 1,
            "unitPrice": 100.0,
            "tradedAt": "2026-01-05T09:00:00Z",
            "rationaleText": "",
        }], trading_days=["2026-01-05"])
        engine = BacktestEngine(
            1, "2026-01-05", "2026-01-05", 1_000.0,
            {1: {"securityCode": "000001", "securityName": "테스트전자"}},
            [{"securityId": 1, "priceDate": "2026-01-05", "openPrice": 100.0, "closePrice": 100.0}],
        )
        engine.register_variant(1, strategy)

        trades, _ = engine.run()

        self.assertEqual(trades[0].decision_reason, "사용자가 DB에 입력한 매매 근거 없음")
        self.assertEqual(trades[0].security_code, "000001")
        self.assertEqual(trades[0].security_name, "테스트전자")

    def test_order_outside_database_universe_is_rejected(self):
        strategy = ActualUserStrategy(1, [{
            "tradeId": 12,
            "securityId": 2,
            "tradeSide": "BUY",
            "quantity": 1,
            "unitPrice": 100.0,
            "tradedAt": "2026-01-05T09:00:00Z",
        }], trading_days=["2026-01-05"])
        engine = BacktestEngine(
            1, "2026-01-05", "2026-01-05", 1_000.0,
            {1: {"securityCode": "000001", "securityName": "허용종목"}},
            [
                {"securityId": 1, "priceDate": "2026-01-05", "openPrice": 100.0, "closePrice": 100.0},
                {"securityId": 2, "priceDate": "2026-01-05", "openPrice": 100.0, "closePrice": 100.0},
            ],
        )
        engine.register_variant(1, strategy)

        trades, _ = engine.run()

        self.assertEqual(trades, [])
        self.assertIn("SECURITY_NOT_IN_DB_UNIVERSE", engine.order_audits[0].reason_codes)


if __name__ == "__main__":
    unittest.main()
