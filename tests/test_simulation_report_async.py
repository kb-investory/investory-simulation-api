import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks

from app.api.endpoints import simulation
from app.modules.simulation.report_generator import SimulationReportGenerator


class SimulationReportAsyncTests(unittest.TestCase):
    def tearDown(self):
        simulation.SIMULATION_RUN_CACHE.pop(987654, None)
        simulation.REPORT_NARRATIVE_IN_PROGRESS.discard(987654)

    def test_pending_cached_report_returns_immediately_and_schedules_enrichment(self):
        report = {
            "reportVersion": SimulationReportGenerator.REPORT_VERSION,
            "generationMetadata": {"narrativeStatus": "PENDING"},
        }
        simulation.SIMULATION_RUN_CACHE[987654] = {
            "report_json": report,
            "reportJson": report,
        }
        background_tasks = BackgroundTasks()

        result = simulation.get_simulation_report(987654, background_tasks)

        self.assertIs(result, report)
        self.assertEqual(len(background_tasks.tasks), 1)

    def test_in_progress_enrichment_is_not_started_twice(self):
        simulation.REPORT_NARRATIVE_IN_PROGRESS.add(987654)
        background_tasks = BackgroundTasks()

        scheduled = simulation._schedule_report_enrichment(
            background_tasks,
            987654,
            {"generationMetadata": {"narrativeStatus": "PENDING"}},
        )

        self.assertFalse(scheduled)
        self.assertEqual(background_tasks.tasks, [])

    def test_background_enrichment_updates_persistence_and_cache(self):
        base_report = {"generationMetadata": {"narrativeStatus": "PENDING"}}
        enriched_report = {"generationMetadata": {"narrativeStatus": "COMPLETED"}}
        simulation.SIMULATION_RUN_CACHE[987654] = {}
        simulation.REPORT_NARRATIVE_IN_PROGRESS.add(987654)
        with (
            patch.object(SimulationReportGenerator, "enrich_report", return_value=enriched_report),
            patch.object(simulation, "save_simulation_report_to_db") as save_report,
        ):
            simulation._enrich_simulation_report_in_background(
                987654,
                base_report,
            )

        save_report.assert_called_once_with(987654, enriched_report)
        self.assertIs(simulation.SIMULATION_RUN_CACHE[987654]["report_json"], enriched_report)
        self.assertNotIn(987654, simulation.REPORT_NARRATIVE_IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
