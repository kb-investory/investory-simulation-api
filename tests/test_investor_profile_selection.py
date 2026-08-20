import unittest

from app.modules.simulation.persistence.repository import SimulationRepository


class FakeCursor:
    def __init__(self):
        self.query_index = 0
        self.first_query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params):
        self.query_index += 1
        if self.query_index == 1:
            self.first_query = query

    def fetchone(self):
        if self.query_index == 1:
            return (2, "2026-01-01", "2026-03-31", 10, 2, "1.0")
        return ("조합 요약", "강점 요약", "주의 요약")

    def fetchall(self):
        return [
            (f"AXIS_{index}", f"축 {index}", f"TYPE_{index}", f"유형 {index}", {"score": 50 + index})
            for index in range(1, 7)
        ]


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


class InvestorProfileSelectionTests(unittest.TestCase):
    def test_selects_latest_run_with_all_six_axes(self):
        connection = FakeConnection()
        repository = SimulationRepository(connection_factory=lambda: connection)

        profile = repository.load_latest_investor_profile(1)

        self.assertIn("COUNT(DISTINCT res.analysis_dimension_code)", connection.cursor_instance.first_query)
        self.assertEqual(profile["analysisRunId"], 2)
        self.assertEqual(len(profile["axes"]), 6)


if __name__ == "__main__":
    unittest.main()
