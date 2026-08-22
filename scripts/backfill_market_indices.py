"""Backfill KOSPI/KOSDAQ closes for the latest simulation trading days."""

import json
from urllib.error import HTTPError

import sys
from pathlib import Path

# 스크립트를 직접 실행해도 app 패키지를 찾도록 프로젝트 루트를 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.simulation.persistence.db_persistence import get_db_connection
from app.modules.simulation.collectors.market_index_collector import MarketIndexCollector


def latest_trading_dates(limit: int = 90) -> list[str]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT price_date
                FROM security_daily_prices
                ORDER BY price_date DESC
                LIMIT %s
                """,
                (limit,),
            )
            return sorted(str(row[0]) for row in cur.fetchall())
    finally:
        conn.close()


def stored_summary(period_start: str, period_end: str) -> list[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT index_code, MIN(price_date), MAX(price_date), COUNT(*)
                FROM market_index_daily_prices
                WHERE price_date BETWEEN %s AND %s
                GROUP BY index_code
                ORDER BY index_code
                """,
                (period_start, period_end),
            )
            return [
                {
                    "indexCode": row[0],
                    "periodStart": str(row[1]),
                    "periodEnd": str(row[2]),
                    "rowCount": int(row[3]),
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


def main() -> None:
    collector = MarketIndexCollector()
    if not collector.configured:
        raise RuntimeError("KRX_OPEN_API_KEY is not configured")

    trading_dates = latest_trading_dates()
    if not trading_dates:
        raise RuntimeError("No security trading dates are available")

    try:
        collector._fetch_market_date("KOSPI", trading_dates[-1])
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
            detail = payload.get("message") or payload.get("msg") or payload.get("result")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise RuntimeError(
            f"KRX preflight failed: HTTP {error.code}"
            + (f", response={detail}" if detail else "")
        ) from None
    except Exception as error:
        raise RuntimeError(f"KRX preflight failed: {type(error).__name__}") from None

    result = collector.ensure_period(
        trading_dates[0],
        trading_dates[-1],
        trading_dates,
    )
    print({
        "periodStart": trading_dates[0],
        "periodEnd": trading_dates[-1],
        "tradingDayCount": len(trading_dates),
        **result,
    })
    print({"storedSummary": stored_summary(trading_dates[0], trading_dates[-1])})


if __name__ == "__main__":
    main()
