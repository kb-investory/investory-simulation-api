import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.endpoints import simulation
from app.modules.simulation.analytics.report_generator import SimulationReportGenerator


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

        # #34: _schedule_report_enrichment는 더 이상 FastAPI BackgroundTasks가 아니라
        # 데몬 스레드로 fire-and-forget한다 — threading.Thread.start()가 호출됐는지로
        # "스케줄됐다"를 확인한다(실제 LLM 호출은 막아야 하니 Thread 자체를 patch).
        with (
            patch.object(simulation, "get_simulation_owner_id", return_value=1),
            patch.object(simulation.threading, "Thread") as thread_cls,
        ):
            result = simulation.get_simulation_report(987654, user_id=1)

        # The stored report is answered in the delivered shape without waiting,
        # and the narrative it is still missing is scheduled behind the response.
        self.assertEqual(result["generationMetadata"]["narrativeStatus"], "PENDING")
        self.assertNotIn("reportVersion", result)
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()

    def test_in_progress_enrichment_is_not_started_twice(self):
        simulation.REPORT_NARRATIVE_IN_PROGRESS.add(987654)

        with patch.object(simulation.threading, "Thread") as thread_cls:
            scheduled = simulation._schedule_report_enrichment(
                987654,
                {"generationMetadata": {"narrativeStatus": "PENDING"}},
            )

        self.assertFalse(scheduled)
        thread_cls.assert_not_called()

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


class SimulationOwnershipTests(unittest.TestCase):
    def tearDown(self):
        simulation.SIMULATION_RUN_CACHE.pop(987654, None)

    def test_another_users_report_is_not_served_from_the_shared_cache(self):
        # The cache is process-wide, so a run cached by whoever executed it last
        # must not become readable to the next caller.
        report = {
            "reportVersion": SimulationReportGenerator.REPORT_VERSION,
            "generationMetadata": {"narrativeStatus": "COMPLETED"},
        }
        simulation.SIMULATION_RUN_CACHE[987654] = {"report_json": report, "reportJson": report}

        with patch.object(simulation, "get_simulation_owner_id", return_value=12):
            with self.assertRaises(HTTPException) as caught:
                simulation.get_simulation_report(987654, user_id=1)

        self.assertEqual(caught.exception.status_code, 404)

    def test_another_users_detail_is_not_served_from_the_shared_cache(self):
        simulation.SIMULATION_RUN_CACHE[987654] = {"periodStart": "2026-01-01"}

        with patch.object(simulation, "get_simulation_owner_id", return_value=12):
            with self.assertRaises(HTTPException) as caught:
                simulation.get_simulation_detail(987654, user_id=1)

        self.assertEqual(caught.exception.status_code, 404)

    def test_a_run_that_does_not_exist_is_refused_the_same_way(self):
        with patch.object(simulation, "get_simulation_owner_id", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                simulation.get_simulation_detail(987654, user_id=1)

        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
