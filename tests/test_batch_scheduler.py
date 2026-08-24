import datetime
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from app.modules.simulation.collectors import batch_cron
from app.modules.simulation.collectors.batch_cron import seconds_until_next_run


class BatchSchedulerTests(unittest.TestCase):
    def test_schedules_same_day_before_market_close_batch(self):
        now = datetime.datetime(2026, 8, 11, 15, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(seconds_until_next_run(now), 3600)

    def test_schedules_next_day_after_batch_time(self):
        now = datetime.datetime(2026, 8, 11, 17, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(seconds_until_next_run(now), 23.5 * 3600)


class BatchJobDedupTests(unittest.TestCase):
    """uvicorn --workers N일 때 프로세스마다 뜨는 스케줄러가 같은 날 배치를 중복
    실행하지 않도록, MySQL GET_LOCK으로 한 워커만 실제로 실행하는지 검증한다."""

    @patch("app.modules.simulation.collectors.batch_cron._run_batch_job_locked")
    @patch("app.modules.simulation.collectors.batch_cron.get_db_connection")
    def test_worker_that_fails_to_acquire_lock_skips_the_job(self, get_connection, run_locked):
        conn = MagicMock()
        get_connection.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (0,)  # GET_LOCK returned 0: someone else already holds it

        batch_cron._run_batch_job()

        run_locked.assert_not_called()
        conn.close.assert_called_once()

    @patch("app.modules.simulation.collectors.batch_cron._run_batch_job_locked")
    @patch("app.modules.simulation.collectors.batch_cron.get_db_connection")
    def test_worker_that_acquires_lock_runs_the_job_and_releases_it(self, get_connection, run_locked):
        conn = MagicMock()
        get_connection.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # GET_LOCK returned 1: acquired

        batch_cron._run_batch_job()

        run_locked.assert_called_once()
        release_calls = [
            call for call in cur.execute.call_args_list
            if call.args and "RELEASE_LOCK" in call.args[0]
        ]
        self.assertEqual(len(release_calls), 1)
        conn.close.assert_called_once()

    @patch("app.modules.simulation.collectors.batch_cron.get_db_connection")
    def test_lock_is_released_even_if_the_job_raises(self, get_connection):
        conn = MagicMock()
        get_connection.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)

        with patch(
            "app.modules.simulation.collectors.batch_cron._run_batch_job_locked",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                batch_cron._run_batch_job()

        release_calls = [
            call for call in cur.execute.call_args_list
            if call.args and "RELEASE_LOCK" in call.args[0]
        ]
        self.assertEqual(len(release_calls), 1)


if __name__ == "__main__":
    unittest.main()
