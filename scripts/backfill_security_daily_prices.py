"""Backfill registered KOSPI/KOSDAQ securities with FinanceDataReader."""

from __future__ import annotations

import argparse
import json

from app.modules.simulation.security_price_collector import SecurityPriceCollector


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
