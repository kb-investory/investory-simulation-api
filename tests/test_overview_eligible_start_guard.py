import asyncio
import unittest
from unittest.mock import patch

from app.api.endpoints.simulation import get_simulation_overview


OVERVIEW_FIELDS = dict(
    accountId=5221,
    eligibleEndDate="2026-08-21",
    journalDays=0,
    connectedAccountsCount=1,
    recentSimulationCount=0,
    priceStartDate="2024-01-02",
    priceEndDate="2026-08-21",
    tradingDayCount=642,
    securityCount=30,
)


class OverviewEligibleStartGuardTests(unittest.TestCase):
    @patch("app.api.endpoints.simulation.InitialCapitalCalculator.calculate")
    @patch("app.api.endpoints.simulation._get_overview_db_task")
    def test_unknown_eligible_start_short_circuits_without_calling_calculate(
        self, get_overview_task, calculate
    ):
        get_overview_task.return_value = {**OVERVIEW_FIELDS, "eligibleStartDate": None}

        response = asyncio.run(get_simulation_overview(user_id=1))

        calculate.assert_not_called()
        self.assertFalse(response["isReady"])
        self.assertIsNone(response["initialCapitalBreakdown"])
        self.assertEqual(response["dataError"]["code"], "ELIGIBLE_START_DATE_UNKNOWN")
        self.assertEqual(response["dataError"]["details"]["accountId"], 5221)

    @patch("app.api.endpoints.simulation.InitialCapitalCalculator.calculate")
    @patch("app.api.endpoints.simulation._get_overview_db_task")
    def test_known_eligible_start_still_calls_calculate_as_before(
        self, get_overview_task, calculate
    ):
        get_overview_task.return_value = {**OVERVIEW_FIELDS, "eligibleStartDate": "2026-06-02"}
        calculate.return_value = {
            "totalInitialCapital": 52000000.0,
            "calculationPolicy": "RECONSTRUCTED_FROM_TRADE_MATCHES",
        }

        response = asyncio.run(get_simulation_overview(user_id=1))

        calculate.assert_called_once_with(start_date="2026-06-02", account_id=5221)
        self.assertTrue(response["isReady"])
        self.assertIsNone(response["dataError"])
        self.assertEqual(response["recommendedInitialCapital"], 52000000.0)


if __name__ == "__main__":
    unittest.main()
