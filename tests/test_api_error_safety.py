import logging
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.error_responses import internal_server_error
from app.api.endpoints import principles, simulation
from app.api.endpoints.simulation_helpers import SimulationRunRequest


SECRET_ERROR = "mysql://admin:secret@internal-db:3306 SQL syntax near customer_ssn"


class ApiErrorSafetyTests(unittest.TestCase):
    def assert_safe_500(self, error: HTTPException, expected_code: str):
        self.assertEqual(error.status_code, 500)
        self.assertEqual(error.detail["code"], expected_code)
        self.assertEqual(len(error.detail["errorId"]), 12)
        self.assertNotIn("secret", str(error.detail))
        self.assertNotIn("internal-db", str(error.detail))
        self.assertNotIn("customer_ssn", str(error.detail))

    def test_common_error_response_logs_original_but_does_not_return_it(self):
        test_logger = logging.getLogger("tests.api_error_safety")
        try:
            raise RuntimeError(SECRET_ERROR)
        except RuntimeError as original:
            with self.assertLogs(test_logger, level="ERROR") as captured:
                response_error = internal_server_error(
                    test_logger,
                    original,
                    code="SAFE_TEST_ERROR",
                    message="안전한 오류 메시지",
                )

        self.assert_safe_500(response_error, "SAFE_TEST_ERROR")
        self.assertIn(SECRET_ERROR, "\n".join(captured.output))

    @patch("app.api.endpoints.simulation.InitialCapitalCalculator.calculate")
    def test_initial_capital_endpoint_does_not_expose_internal_exception(self, calculate):
        calculate.side_effect = RuntimeError(SECRET_ERROR)
        with self.assertLogs(simulation.logger, level="ERROR"):
            with self.assertRaises(HTTPException) as raised:
                simulation.calculate_initial_capital("2026-01-01", 21)
        self.assert_safe_500(raised.exception, "INITIAL_CAPITAL_INTERNAL_ERROR")

    @patch("app.api.endpoints.simulation.SimulationRepository.load_initial_snapshot")
    def test_simulation_run_endpoint_does_not_expose_db_exception(self, load_snapshot):
        load_snapshot.side_effect = RuntimeError(SECRET_ERROR)
        request = SimulationRunRequest(periodStart="2026-01-01", periodEnd="2026-02-01")
        with self.assertLogs(simulation.logger, level="ERROR"):
            with self.assertRaises(HTTPException) as raised:
                simulation.run_simulation(request)
        self.assert_safe_500(raised.exception, "SIMULATION_RUN_INTERNAL_ERROR")

    @patch("app.modules.simulation.db_persistence.get_db_connection")
    def test_principles_endpoint_does_not_expose_db_exception(self, get_connection):
        get_connection.side_effect = RuntimeError(SECRET_ERROR)
        with self.assertLogs(principles.logger, level="ERROR"):
            with self.assertRaises(HTTPException) as raised:
                principles.get_recommended_principles()
        self.assert_safe_500(raised.exception, "PRINCIPLES_READ_INTERNAL_ERROR")


if __name__ == "__main__":
    unittest.main()
