import unittest
from unittest.mock import patch

from app.modules.simulation.market_index_collector import MarketIndexCollector


class MarketIndexCollectorTests(unittest.TestCase):
    def test_missing_key_reports_fallback_state_without_network_call(self):
        collector = MarketIndexCollector(api_key="")
        with patch.object(collector, "_existing_pairs", return_value=set()), patch.object(
            collector,
            "_fetch_market_date",
        ) as fetch:
            result = collector.ensure_period(
                "2026-08-10",
                "2026-08-11",
                ["2026-08-10", "2026-08-11"],
            )

        self.assertEqual(result["status"], "KEY_NOT_CONFIGURED")
        self.assertEqual(result["missingCount"], 4)
        fetch.assert_not_called()

    def test_only_missing_index_dates_are_fetched_and_saved(self):
        collector = MarketIndexCollector(api_key="configured-key")
        existing = {
            ("KOSPI", "2026-08-10"),
            ("KOSDAQ", "2026-08-10"),
            ("KOSPI", "2026-08-11"),
        }
        fetched_row = {
            "indexCode": "KOSDAQ",
            "priceDate": "2026-08-11",
            "closePrice": 912.34,
        }
        with patch.object(collector, "_existing_pairs", return_value=existing), patch.object(
            collector,
            "_fetch_market_date",
            return_value=fetched_row,
        ) as fetch, patch.object(collector, "_save") as save:
            result = collector.ensure_period(
                "2026-08-10",
                "2026-08-11",
                ["2026-08-10", "2026-08-11"],
            )

        fetch.assert_called_once_with("KOSDAQ", "2026-08-11")
        save.assert_called_once_with([fetched_row])
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual(result["fetchedCount"], 1)
        self.assertEqual(result["missingCount"], 0)

    def test_complete_period_is_served_from_database(self):
        collector = MarketIndexCollector(api_key="")
        complete = {
            ("KOSPI", "2026-08-11"),
            ("KOSDAQ", "2026-08-11"),
        }
        with patch.object(collector, "_existing_pairs", return_value=complete):
            result = collector.ensure_period(
                "2026-08-11",
                "2026-08-11",
                ["2026-08-11"],
            )

        self.assertEqual(result["status"], "DB_HIT")
        self.assertEqual(result["missingCount"], 0)

    def test_failed_preflight_stops_repeated_requests_for_each_index(self):
        collector = MarketIndexCollector(api_key="configured-key")
        with patch.object(collector, "_existing_pairs", return_value=set()), patch.object(
            collector,
            "_fetch_market_date",
            side_effect=RuntimeError("forbidden"),
        ) as fetch, patch.object(collector, "_save"):
            result = collector.ensure_period(
                "2026-08-10",
                "2026-08-11",
                ["2026-08-10", "2026-08-11"],
            )

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["status"], "FETCH_FAILED")
        self.assertEqual(result["missingCount"], 4)
        self.assertEqual(result["errorCount"], 2)


if __name__ == "__main__":
    unittest.main()
