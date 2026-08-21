import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.endpoints.simulation import calculate_initial_capital
from app.modules.simulation.persistence.capital_calculator import InitialCapitalCalculator
from app.modules.simulation.persistence.repository import SimulationDataError


class FakeRepository:
    def __init__(self, snapshot_date):
        self.snapshot_date = snapshot_date

    def load_initial_snapshot(self, account_id, start_date):
        return {
            "snapshotDate": self.snapshot_date,
            "accountId": account_id,
            "initialCapital": 1000.0,
            "holdingsCount": 1,
            "holdings": [{"unrealizedPnl": 10.0}],
            "calculationPolicy": "PREVIOUS_TRADING_DAY_HOLDING_SNAPSHOT",
        }


class InitialCapitalSelectionTests(unittest.TestCase):
    def test_accepts_only_a_snapshot_strictly_before_start_date(self):
        calculator = InitialCapitalCalculator()
        calculator.repository = FakeRepository("2026-07-14")

        result = calculator.calculate("2026-07-15", 21)

        self.assertEqual(result["snapshotDate"], "2026-07-14")

    def test_rejects_a_future_snapshot_even_if_repository_returns_it(self):
        calculator = InitialCapitalCalculator()
        calculator.repository = FakeRepository("2026-08-10")

        with self.assertRaises(SimulationDataError) as context:
            calculator.calculate("2026-08-09", 21)

        self.assertEqual(context.exception.code, "INITIAL_SNAPSHOT_NOT_BEFORE_START")

    def test_endpoint_accepts_camel_case_query_names(self):
        with patch(
            "app.api.endpoints.simulation.SimulationRepository.resolve_account_id",
            return_value=21,
        ), patch(
            "app.api.endpoints.simulation.InitialCapitalCalculator.calculate",
            return_value={"snapshotDate": "2026-07-14"},
        ) as calculate:
            result = calculate_initial_capital(
                start_date=None,
                account_id=None,
                startDate="2026-07-15",
                accountId=21,
                user_id=1,
            )

        calculate.assert_called_once_with(start_date="2026-07-15", account_id=21)
        self.assertEqual(result["snapshotDate"], "2026-07-14")

    def test_endpoint_rejects_conflicting_query_aliases(self):
        with self.assertRaises(HTTPException) as context:
            calculate_initial_capital(
                start_date="2026-07-15",
                account_id=21,
                startDate="2026-08-09",
                accountId=21,
                user_id=1,
            )

        self.assertEqual(context.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
