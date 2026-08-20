import datetime
import unittest
from zoneinfo import ZoneInfo

from app.modules.simulation.collectors.batch_cron import seconds_until_next_run


class BatchSchedulerTests(unittest.TestCase):
    def test_schedules_same_day_before_market_close_batch(self):
        now = datetime.datetime(2026, 8, 11, 15, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(seconds_until_next_run(now), 3600)

    def test_schedules_next_day_after_batch_time(self):
        now = datetime.datetime(2026, 8, 11, 17, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        self.assertEqual(seconds_until_next_run(now), 23.5 * 3600)


if __name__ == "__main__":
    unittest.main()
