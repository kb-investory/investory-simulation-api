"""Backfill registered KOSPI/KOSDAQ securities with FinanceDataReader."""

from __future__ import annotations

import argparse
import json

import sys
from pathlib import Path

# 스크립트를 직접 실행해도 app 패키지를 찾도록 프로젝트 루트를 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.simulation.collectors.security_price_collector import SecurityPriceCollector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Inclusive end date (YYYY-MM-DD)")
    parser.add_argument("--codes", nargs="*", help="Optional six-digit security codes")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing to DB")
    args = parser.parse_args()

    result = SecurityPriceCollector().backfill(
        start_date=args.start,
        end_date=args.end,
        security_codes=args.codes,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
