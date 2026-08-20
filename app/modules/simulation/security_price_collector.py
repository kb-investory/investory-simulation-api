"""FinanceDataReader-based daily OHLCV backfill for registered securities."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Callable, Iterable, Optional

import FinanceDataReader as fdr
import pandas as pd

from app.modules.simulation.persistence.db_persistence import get_db_connection


class SecurityPriceCollector:
    REQUIRED_COLUMNS = {"Open", "High", "Low", "Close", "Volume", "Change"}

    def __init__(
        self,
        reader: Callable = fdr.DataReader,
        connection_factory: Callable = get_db_connection,
    ):
        self.reader = reader
        self.connection_factory = connection_factory

    @staticmethod
    def _validate_period(start_date: str, end_date: str) -> None:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if start > end:
            raise ValueError("start_date must be on or before end_date")

    def _load_securities(self, security_codes: Optional[Iterable[str]] = None) -> list[tuple[int, str]]:
        selected_codes = {str(code).zfill(6) for code in security_codes or []}
        conn = self.connection_factory()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT security_id, security_code FROM securities "
                    "WHERE is_active = 1 AND market_type IN ('KOSPI', 'KOSDAQ') "
                    "ORDER BY security_id"
                )
                securities = [(int(row[0]), str(row[1]).zfill(6)) for row in cursor.fetchall()]
        finally:
            conn.close()

        if selected_codes:
            securities = [row for row in securities if row[1] in selected_codes]
            found = {code for _, code in securities}
            missing = sorted(selected_codes - found)
            if missing:
                raise ValueError(f"Unknown or inactive security codes: {', '.join(missing)}")
        return securities

    @classmethod
    def _rows_from_frame(cls, security_id: int, frame: pd.DataFrame) -> list[tuple]:
        missing_columns = cls.REQUIRED_COLUMNS - set(frame.columns)
        if missing_columns:
            raise ValueError(f"Daily price data is missing columns: {sorted(missing_columns)}")

        rows = []
        for index, item in frame.sort_index().iterrows():
            values = [item[column] for column in ("Open", "High", "Low", "Close", "Volume")]
            if any(pd.isna(value) for value in values):
                continue

            open_price, high_price, low_price, close_price = (
                Decimal(str(item[column])) for column in ("Open", "High", "Low", "Close")
            )
            volume = int(item["Volume"])
            change = None if pd.isna(item["Change"]) else Decimal(str(item["Change"])) * 100
            rows.append(
                (
                    security_id,
                    pd.Timestamp(index).date(),
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    change,
                    volume,
                    close_price * volume,
                )
            )
        return rows

    def backfill(
        self,
        start_date: str,
        end_date: str,
        security_codes: Optional[Iterable[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Fetch all requested data first, then persist it in one DB transaction."""
        self._validate_period(start_date, end_date)
        securities = self._load_securities(security_codes)
        all_rows: list[tuple] = []
        per_security: dict[str, int] = {}

        for security_id, security_code in securities:
            frame = self.reader(security_code, start_date, end_date)
            rows = self._rows_from_frame(security_id, frame)
            if not rows:
                raise ValueError(
                    f"No daily price data returned for {security_code} between {start_date} and {end_date}"
                )
            all_rows.extend(rows)
            per_security[security_code] = len(rows)

        if not dry_run:
            conn = self.connection_factory()
            try:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO security_daily_prices
                        (security_id, price_date, open_price, high_price, low_price, close_price,
                         daily_return_rate, trading_volume, trading_value)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            open_price = VALUES(open_price),
                            high_price = VALUES(high_price),
                            low_price = VALUES(low_price),
                            close_price = VALUES(close_price),
                            daily_return_rate = VALUES(daily_return_rate),
                            trading_volume = VALUES(trading_volume),
                            trading_value = VALUES(trading_value)
                        """,
                        all_rows,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return {
            "startDate": start_date,
            "endDate": end_date,
            "securityCount": len(securities),
            "rowCount": len(all_rows),
            "perSecurity": per_security,
            "dryRun": dry_run,
        }
