"""Lazy KOSPI/KOSDAQ benchmark collection through the official KRX Open API."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Optional

from app.config import settings
from app.modules.simulation.db_persistence import get_db_connection


INDEX_ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
}


def _index_number(value) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


class MarketIndexCollector:
    def __init__(self, api_key: Optional[str] = None, connection_factory=get_db_connection):
        self.api_key = api_key if api_key is not None else settings.KRX_OPEN_API_KEY
        self.connection_factory = connection_factory

    @property
    def configured(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def _fetch_market_date(self, index_code: str, price_date: str) -> Optional[dict]:
        endpoint = INDEX_ENDPOINTS[index_code]
        url = endpoint + "?" + urllib.parse.urlencode({"basDd": price_date.replace("-", "")})
        request = urllib.request.Request(
            url,
            headers={"AUTH_KEY": self.api_key, "User-Agent": "Investory-AI/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("OutBlock_1", [])
        aliases = {index_code, "코스피" if index_code == "KOSPI" else "코스닥"}
        normalized_aliases = {alias.replace(" ", "").upper() for alias in aliases}
        selected = next(
            (
                row for row in rows
                if str(row.get("IDX_NM", "")).replace(" ", "").upper() in normalized_aliases
            ),
            None,
        )
        if not selected:
            return None
        close_price = _index_number(selected.get("CLSPRC_IDX"))
        base_date = str(selected.get("BAS_DD") or price_date.replace("-", ""))
        if close_price is None or len(base_date) != 8:
            return None
        return {
            "indexCode": index_code,
            "priceDate": f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:8]}",
            "closePrice": close_price,
        }

    def _table_exists(self) -> bool:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'market_index_daily_prices'
                    """
                )
                return bool(cur.fetchone()[0])
        finally:
            conn.close()

    def _existing_pairs(self, period_start: str, period_end: str) -> set[tuple[str, str]]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT index_code, price_date
                    FROM market_index_daily_prices
                    WHERE price_date BETWEEN %s AND %s
                      AND index_code IN ('KOSPI', 'KOSDAQ')
                    """,
                    (period_start, period_end),
                )
                return {(str(row[0]), str(row[1])) for row in cur.fetchall()}
        finally:
            conn.close()

    def _save(self, rows: List[dict]) -> None:
        if not rows:
            return
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(
                        """
                        INSERT INTO market_index_daily_prices
                        (index_code, price_date, close_price, source)
                        VALUES (%s, %s, %s, 'KRX_OPEN_API')
                        ON DUPLICATE KEY UPDATE
                            close_price = VALUES(close_price), source = VALUES(source)
                        """,
                        (row["indexCode"], row["priceDate"], row["closePrice"]),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_period(
        self,
        period_start: str,
        period_end: str,
        trading_dates: Iterable[str],
    ) -> dict:
        # Index prices are optional: the benchmark falls back to an equal-weight
        # universe without them. Report that plainly instead of raising a table
        # error the caller can only swallow.
        if not self._table_exists():
            return {
                "status": "TABLE_NOT_AVAILABLE",
                "fetchedCount": 0,
                "missingCount": 0,
            }
        dates = sorted({date for date in trading_dates if period_start <= date <= period_end})
        existing = self._existing_pairs(period_start, period_end)
        missing = [
            (index_code, price_date)
            for price_date in dates
            for index_code in INDEX_ENDPOINTS
            if (index_code, price_date) not in existing
        ]
        if not missing:
            return {"status": "DB_HIT", "fetchedCount": 0, "missingCount": 0}
        if not self.configured:
            return {
                "status": "KEY_NOT_CONFIGURED",
                "fetchedCount": 0,
                "missingCount": len(missing),
            }
        fetched = []
        errors = []
        blocked_index_codes = set()

        def fetch_one(index_code: str, price_date: str):
            try:
                row = self._fetch_market_date(index_code, price_date)
                return index_code, price_date, row, None
            except Exception as error:
                return index_code, price_date, None, error

        preflight_pairs = []
        for index_code in INDEX_ENDPOINTS:
            pair = next((item for item in missing if item[0] == index_code), None)
            if pair:
                preflight_pairs.append(pair)

        for index_code, price_date in preflight_pairs:
            _, _, row, error = fetch_one(index_code, price_date)
            if row:
                fetched.append(row)
            else:
                blocked_index_codes.add(index_code)
            if error:
                errors.append({
                    "indexCode": index_code,
                    "priceDate": price_date,
                    "errorType": type(error).__name__,
                })

        remaining_pairs = [
            pair for pair in missing
            if pair not in preflight_pairs and pair[0] not in blocked_index_codes
        ]
        with ThreadPoolExecutor(max_workers=min(6, len(remaining_pairs) or 1)) as executor:
            futures = [executor.submit(fetch_one, *pair) for pair in remaining_pairs]
            for future in as_completed(futures):
                index_code, price_date, row, error = future.result()
                if row:
                    fetched.append(row)
                if error:
                    errors.append({
                        "indexCode": index_code,
                        "priceDate": price_date,
                        "errorType": type(error).__name__,
                    })
        self._save(fetched)
        remaining_count = max(0, len(missing) - len(fetched))
        if fetched and not remaining_count:
            status = "FETCHED"
        elif fetched:
            status = "PARTIAL"
        else:
            status = "FETCH_FAILED"
        return {
            "status": status,
            "fetchedCount": len(fetched),
            "missingCount": remaining_count,
            "errorCount": len(errors),
        }
