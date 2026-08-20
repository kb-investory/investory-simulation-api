"""Point-in-time OpenDART financial statement collection and persistence."""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence

from app.config import settings
from app.modules.simulation.dart_collector import DartCollector
from app.modules.simulation.persistence.db_persistence import get_db_connection


REPORT_CODES = {
    "11013": 4.0,       # 1분기
    "11012": 2.0,       # 반기
    "11014": 4.0 / 3.0, # 3분기
    "11011": 1.0,       # 사업보고서
}

ACCOUNT_IDS = {
    "revenue": (
        "ifrs-full_Revenue",
        "ifrs-full_RevenueFromContractsWithCustomers",
    ),
    "net_income": (
        "ifrs-full_ProfitLoss",
        "ifrs-full_ProfitLossAttributableToOwnersOfParent",
    ),
    "assets": ("ifrs-full_Assets",),
    "liabilities": ("ifrs-full_Liabilities",),
    "equity": (
        "ifrs-full_Equity",
        "ifrs-full_EquityAttributableToOwnersOfParent",
    ),
    "operating_cash_flow": ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
}

ACCOUNT_NAMES = {
    "revenue": ("매출액", "영업수익"),
    "net_income": ("당기순이익", "분기순이익", "반기순이익"),
    "assets": ("자산총계",),
    "liabilities": ("부채총계",),
    "equity": ("자본총계",),
    "operating_cash_flow": ("영업활동으로 인한 현금흐름", "영업활동 현금흐름"),
}


def _number(value) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    normalized = str(value).replace(",", "").strip()
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return float(normalized)
    except ValueError:
        return None


def _date_from_text(value: str) -> Optional[str]:
    matches = re.findall(r"(20\d{2})[.\-/](\d{2})[.\-/](\d{2})", value or "")
    if not matches:
        return None
    year, month, day = matches[-1]
    return f"{year}-{month}-{day}"


def _default_fiscal_period_end(business_year: int, report_code: str) -> str:
    month_day = {
        "11013": "03-31",
        "11012": "06-30",
        "11014": "09-30",
        "11011": "12-31",
    }[report_code]
    return f"{business_year}-{month_day}"


class FundamentalsCollector:
    """Collects report-effective fundamentals without using future information."""

    def __init__(self, api_key: Optional[str] = None, connection_factory=get_db_connection):
        self.api_key = api_key or settings.OPENDART_API_KEY
        if not self.api_key or self.api_key.startswith("your_"):
            raise ValueError("유효한 OPENDART_API_KEY가 설정되지 않았습니다.")
        self.connection_factory = connection_factory

    def _request_json(self, endpoint: str, params: dict) -> dict:
        query = {"crtfc_key": self.api_key, **params}
        url = f"https://opendart.fss.or.kr/api/{endpoint}.json?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"User-Agent": "Investory-AI/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        status = payload.get("status")
        if status not in {"000", "013"}:
            raise RuntimeError(f"OpenDART {endpoint} 오류({status}): {payload.get('message')}")
        return payload

    @staticmethod
    def _find_account(items: Sequence[dict], key: str, statement_types: Iterable[str]) -> Optional[dict]:
        types = set(statement_types)
        candidates = [item for item in items if item.get("sj_div") in types]
        for account_id in ACCOUNT_IDS[key]:
            found = next((item for item in candidates if item.get("account_id") == account_id), None)
            if found:
                return found
        for account_name in ACCOUNT_NAMES[key]:
            found = next((item for item in candidates if item.get("account_nm") == account_name), None)
            if found:
                return found
        return None

    @staticmethod
    def _period_amount(item: Optional[dict], report_code: str, previous: bool = False) -> Optional[float]:
        if not item:
            return None
        if previous:
            keys = (
                ("frmtrm_amount", "frmtrm_q_amount")
                if report_code == "11011"
                else ("frmtrm_add_amount", "frmtrm_q_amount", "frmtrm_amount")
            )
        else:
            keys = ("thstrm_amount",) if report_code == "11011" else ("thstrm_add_amount", "thstrm_amount")
        for key in keys:
            amount = _number(item.get(key))
            if amount is not None:
                return amount
        return None

    def _fetch_statement(self, corp_code: str, business_year: int, report_code: str) -> tuple[List[dict], str]:
        for fs_div in ("CFS", "OFS"):
            payload = self._request_json(
                "fnlttSinglAcntAll",
                {
                    "corp_code": corp_code,
                    "bsns_year": str(business_year),
                    "reprt_code": report_code,
                    "fs_div": fs_div,
                },
            )
            if payload.get("status") == "000" and payload.get("list"):
                return payload["list"], fs_div
        return [], ""

    def _fetch_shares(self, corp_code: str, business_year: int, report_code: str) -> Optional[float]:
        payload = self._request_json(
            "stockTotqySttus",
            {"corp_code": corp_code, "bsns_year": str(business_year), "reprt_code": report_code},
        )
        for item in payload.get("list", []):
            shares = _number(item.get("istc_totqy"))
            if shares is not None and shares > 0:
                return shares
        return None

    def collect_report(self, corp_code: str, business_year: int, report_code: str) -> Optional[dict]:
        if report_code not in REPORT_CODES:
            raise ValueError(f"지원하지 않는 OpenDART 보고서 코드입니다: {report_code}")
        items, fs_div = self._fetch_statement(corp_code, business_year, report_code)
        if not items:
            return None

        accounts = {
            "revenue": self._find_account(items, "revenue", ("IS", "CIS")),
            "net_income": self._find_account(items, "net_income", ("IS", "CIS")),
            "assets": self._find_account(items, "assets", ("BS",)),
            "liabilities": self._find_account(items, "liabilities", ("BS",)),
            "equity": self._find_account(items, "equity", ("BS",)),
            "operating_cash_flow": self._find_account(items, "operating_cash_flow", ("CF",)),
        }
        revenue = self._period_amount(accounts["revenue"], report_code)
        previous_revenue = self._period_amount(accounts["revenue"], report_code, previous=True)
        net_income = self._period_amount(accounts["net_income"], report_code)
        previous_net_income = self._period_amount(accounts["net_income"], report_code, previous=True)
        assets = self._period_amount(accounts["assets"], report_code)
        liabilities = self._period_amount(accounts["liabilities"], report_code)
        equity = self._period_amount(accounts["equity"], report_code)
        operating_cash_flow = self._period_amount(accounts["operating_cash_flow"], report_code)
        receipt_no = str(items[0].get("rcept_no") or "")
        effective_date = (
            f"{receipt_no[:4]}-{receipt_no[4:6]}-{receipt_no[6:8]}"
            if len(receipt_no) >= 8 else None
        )
        fiscal_period_end = next(
            (
                _date_from_text(item.get("thstrm_dt", ""))
                for item in items
                if _date_from_text(item.get("thstrm_dt", ""))
            ),
            _default_fiscal_period_end(business_year, report_code),
        )
        if not effective_date or not fiscal_period_end:
            return None

        annualized_net_income = net_income * REPORT_CODES[report_code] if net_income is not None else None
        return {
            "effectiveDate": effective_date,
            "fiscalPeriodEnd": fiscal_period_end,
            "sharesOutstanding": self._fetch_shares(corp_code, business_year, report_code),
            "roe": annualized_net_income / equity if annualized_net_income is not None and equity and equity > 0 else None,
            "debtRatio": liabilities / equity if liabilities is not None and equity and equity > 0 else None,
            "revenueGrowth": (
                (revenue - previous_revenue) / abs(previous_revenue)
                if revenue is not None and previous_revenue not in (None, 0) else None
            ),
            "earningsGrowth": (
                (net_income - previous_net_income) / abs(previous_net_income)
                if net_income is not None and previous_net_income not in (None, 0) else None
            ),
            "operatingCashFlowPositive": operating_cash_flow > 0 if operating_cash_flow is not None else None,
            "revenue": revenue,
            "netIncome": net_income,
            "annualizedNetIncome": annualized_net_income,
            "totalAssets": assets,
            "totalLiabilities": liabilities,
            "totalEquity": equity,
            "operatingCashFlow": operating_cash_flow,
            "sourceReceiptNo": receipt_no,
            "reportCode": report_code,
            "fsDiv": fs_div,
        }

    def save_report(self, security_id: int, report: dict) -> None:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                shares_outstanding = report["sharesOutstanding"]
                if shares_outstanding is None:
                    cur.execute(
                        """
                        SELECT shares_outstanding
                        FROM security_fundamentals_daily
                        WHERE security_id = %s AND effective_date < %s
                          AND shares_outstanding IS NOT NULL
                        ORDER BY effective_date DESC LIMIT 1
                        """,
                        (security_id, report["effectiveDate"]),
                    )
                    previous_shares = cur.fetchone()
                    shares_outstanding = float(previous_shares[0]) if previous_shares else None
                cur.execute(
                    """
                    SELECT close_price
                    FROM security_daily_prices
                    WHERE security_id = %s AND price_date <= %s
                    ORDER BY price_date DESC LIMIT 1
                    """,
                    (security_id, report["effectiveDate"]),
                )
                row = cur.fetchone()
                close_price = float(row[0]) if row else None
                market_cap = (
                    close_price * shares_outstanding
                    if close_price is not None and shares_outstanding is not None else None
                )
                per = (
                    market_cap / report["annualizedNetIncome"]
                    if market_cap is not None and report["annualizedNetIncome"] is not None
                    and report["annualizedNetIncome"] > 0 else None
                )
                pbr = (
                    market_cap / report["totalEquity"]
                    if market_cap is not None and report["totalEquity"] is not None
                    and report["totalEquity"] > 0 else None
                )
                cur.execute(
                    """
                    INSERT INTO security_fundamentals_daily
                    (security_id, effective_date, fiscal_period_end, shares_outstanding,
                     per, pbr, roe, debt_ratio, revenue_growth, earnings_growth,
                     operating_cash_flow_positive, revenue, net_income,
                     annualized_net_income, total_assets, total_liabilities,
                     total_equity, operating_cash_flow, source_receipt_no,
                     report_code, fs_div, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPENDART')
                    ON DUPLICATE KEY UPDATE
                        fiscal_period_end = VALUES(fiscal_period_end),
                        shares_outstanding = VALUES(shares_outstanding),
                        per = VALUES(per), pbr = VALUES(pbr), roe = VALUES(roe),
                        debt_ratio = VALUES(debt_ratio), revenue_growth = VALUES(revenue_growth),
                        earnings_growth = VALUES(earnings_growth),
                        operating_cash_flow_positive = VALUES(operating_cash_flow_positive),
                        revenue = VALUES(revenue), net_income = VALUES(net_income),
                        annualized_net_income = VALUES(annualized_net_income),
                        total_assets = VALUES(total_assets), total_liabilities = VALUES(total_liabilities),
                        total_equity = VALUES(total_equity), operating_cash_flow = VALUES(operating_cash_flow),
                        source_receipt_no = VALUES(source_receipt_no), report_code = VALUES(report_code),
                        fs_div = VALUES(fs_div), source = VALUES(source)
                    """,
                    (
                        security_id, report["effectiveDate"], report["fiscalPeriodEnd"],
                        shares_outstanding, per, pbr, report["roe"], report["debtRatio"],
                        report["revenueGrowth"], report["earningsGrowth"],
                        report["operatingCashFlowPositive"], report["revenue"], report["netIncome"],
                        report["annualizedNetIncome"], report["totalAssets"], report["totalLiabilities"],
                        report["totalEquity"], report["operatingCashFlow"], report["sourceReceiptNo"],
                        report["reportCode"], report["fsDiv"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def backfill(
        self,
        start_year: int,
        end_year: int,
        security_codes: Optional[List[str]] = None,
        report_codes: Sequence[str] = tuple(REPORT_CODES),
    ) -> dict:
        dart = DartCollector()
        corp_codes = dart.load_corp_code_map()
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_id, security_code FROM securities ORDER BY security_id")
                securities = [(int(row[0]), str(row[1])) for row in cur.fetchall()]
        finally:
            conn.close()
        selected = set(security_codes or [code for _, code in securities])
        saved = 0
        missing = 0
        errors = []
        for security_id, security_code in securities:
            if security_code not in selected:
                continue
            corp_code = corp_codes.get(security_code)
            if not corp_code:
                missing += 1
                continue
            for year in range(start_year, end_year + 1):
                for report_code in report_codes:
                    try:
                        report = self.collect_report(corp_code, year, report_code)
                        if not report:
                            continue
                        self.save_report(security_id, report)
                        saved += 1
                    except Exception as error:
                        errors.append({
                            "securityCode": security_code,
                            "businessYear": year,
                            "reportCode": report_code,
                            "errorType": type(error).__name__,
                        })
        return {
            "startYear": start_year,
            "endYear": end_year,
            "securityCount": len(selected),
            "savedReportCount": saved,
            "missingCorpCodeCount": missing,
            "errorCount": len(errors),
            "errors": errors[:20],
        }

    def backfill_for_simulation_period(
        self,
        period_start: str,
        period_end: str,
        security_codes: Optional[List[str]] = None,
    ) -> dict:
        """Backfill only anchor reports and reports that can affect the requested period."""
        start_date = dt.date.fromisoformat(period_start)
        end_date = dt.date.fromisoformat(period_end)
        report_specs = {
            (start_date.year - 2, "11011"),  # latest-report fallback if last annual filing was late
            (start_date.year - 1, "11011"),  # normal opening anchor
        }
        for business_year in range(start_date.year, end_date.year + 1):
            earliest_filing_dates = {
                "11013": dt.date(business_year, 5, 1),
                "11012": dt.date(business_year, 8, 1),
                "11014": dt.date(business_year, 11, 1),
                "11011": dt.date(business_year + 1, 3, 1),
            }
            report_specs.update(
                (business_year, report_code)
                for report_code, earliest_date in earliest_filing_dates.items()
                if earliest_date <= end_date
            )

        corp_codes = DartCollector().load_corp_code_map()
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_id, security_code FROM securities ORDER BY security_id")
                securities = [(int(row[0]), str(row[1])) for row in cur.fetchall()]
        finally:
            conn.close()
        selected = set(security_codes or [code for _, code in securities])
        saved = 0
        errors = []
        for security_id, security_code in securities:
            if security_code not in selected:
                continue
            corp_code = corp_codes.get(security_code)
            if not corp_code:
                continue
            for business_year, report_code in sorted(report_specs):
                try:
                    report = self.collect_report(corp_code, business_year, report_code)
                    if not report or report["effectiveDate"] > period_end:
                        continue
                    self.save_report(security_id, report)
                    saved += 1
                except Exception as error:
                    errors.append({
                        "securityCode": security_code,
                        "businessYear": business_year,
                        "reportCode": report_code,
                        "errorType": type(error).__name__,
                    })
        return {
            "periodStart": period_start,
            "periodEnd": period_end,
            "securityCount": len(selected),
            "requestedReportSpecs": len(report_specs),
            "savedReportCount": saved,
            "errorCount": len(errors),
            "errors": errors[:20],
        }

    def update_from_disclosure_date(self, target_date: Optional[str] = None) -> dict:
        target_date = target_date or dt.date.today().isoformat()
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT d.security_id, d.source_json
                    FROM disclosure_events d
                    WHERE d.event_date = %s
                    """,
                    (target_date,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        saved = 0
        for security_id, source_json in rows:
            source = json.loads(source_json) if isinstance(source_json, str) else (source_json or {})
            report_code = str(source.get("reprt_code") or "")
            business_year = source.get("bsns_year")
            corp_code = source.get("corp_code")
            if report_code not in REPORT_CODES or not business_year or not corp_code:
                continue
            report = self.collect_report(str(corp_code), int(business_year), report_code)
            if report:
                self.save_report(int(security_id), report)
                saved += 1
        return {"targetDate": target_date, "savedReportCount": saved}
