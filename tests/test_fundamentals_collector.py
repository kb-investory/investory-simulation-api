import unittest
from unittest.mock import patch

from app.modules.simulation.fundamentals_collector import (
    FundamentalsCollector,
    _date_from_text,
    _default_fiscal_period_end,
    _number,
)


class FundamentalsCollectorTests(unittest.TestCase):
    def setUp(self):
        self.collector = FundamentalsCollector(api_key="x" * 40)

    @staticmethod
    def _row(account_id, account_name, statement, current, previous):
        return {
            "rcept_no": "20260315000001",
            "sj_div": statement,
            "account_id": account_id,
            "account_nm": account_name,
            "thstrm_dt": "2025.01.01 ~ 2025.12.31",
            "thstrm_amount": str(current),
            "frmtrm_amount": str(previous),
        }

    def test_number_and_period_date_normalization(self):
        self.assertEqual(_number("1,234"), 1234.0)
        self.assertEqual(_number("(500)"), -500.0)
        self.assertEqual(_date_from_text("2025.01.01 ~ 2025.12.31"), "2025-12-31")
        self.assertEqual(_default_fiscal_period_end(2025, "11012"), "2025-06-30")

    def test_collect_report_calculates_point_in_time_factors(self):
        rows = [
            self._row("ifrs-full_Revenue", "매출액", "IS", 1100, 1000),
            self._row("ifrs-full_ProfitLoss", "당기순이익", "IS", 100, 80),
            self._row("ifrs-full_Assets", "자산총계", "BS", 2000, 1800),
            self._row("ifrs-full_Liabilities", "부채총계", "BS", 500, 450),
            self._row("ifrs-full_Equity", "자본총계", "BS", 1000, 900),
            self._row(
                "ifrs-full_CashFlowsFromUsedInOperatingActivities",
                "영업활동으로 인한 현금흐름",
                "CF",
                120,
                100,
            ),
        ]
        with patch.object(self.collector, "_fetch_statement", return_value=(rows, "CFS")), patch.object(
            self.collector,
            "_fetch_shares",
            return_value=1_000,
        ):
            report = self.collector.collect_report("00126380", 2025, "11011")

        self.assertEqual(report["effectiveDate"], "2026-03-15")
        self.assertEqual(report["fiscalPeriodEnd"], "2025-12-31")
        self.assertEqual(report["sharesOutstanding"], 1_000)
        self.assertAlmostEqual(report["roe"], 0.10)
        self.assertAlmostEqual(report["debtRatio"], 0.50)
        self.assertAlmostEqual(report["revenueGrowth"], 0.10)
        self.assertAlmostEqual(report["earningsGrowth"], 0.25)
        self.assertTrue(report["operatingCashFlowPositive"])


if __name__ == "__main__":
    unittest.main()
