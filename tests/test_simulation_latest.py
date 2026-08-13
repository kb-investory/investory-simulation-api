import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.v1.endpoints.simulation import get_latest_simulation
from app.api.v1.endpoints.simulation_helpers import SIMULATION_RUN_CACHE


class LatestSimulationTests(unittest.TestCase):
    def setUp(self):
        SIMULATION_RUN_CACHE.clear()

    def tearDown(self):
        SIMULATION_RUN_CACHE.clear()

    @patch("app.api.v1.endpoints.simulation.get_simulation_detail")
    @patch(
        "app.api.v1.endpoints.simulation.get_latest_completed_simulation_id_from_db",
        return_value=20,
    )
    def test_latest_ignores_newer_incomplete_run(self, latest_id, get_detail):
        get_detail.return_value = {"simulationRun": {"simulationRunId": 20}}

        response = asyncio.run(get_latest_simulation())

        latest_id.assert_called_once_with(1)
        get_detail.assert_called_once_with(20)
        self.assertEqual(response["simulationRun"]["simulationRunId"], 20)

    @patch(
        "app.api.v1.endpoints.simulation.get_latest_completed_simulation_id_from_db",
        return_value=None,
    )
    def test_latest_returns_404_when_no_completed_result_exists(self, latest_id):
        with self.assertRaises(HTTPException) as context:
            asyncio.run(get_latest_simulation())

        latest_id.assert_called_once_with(1)
        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
