"""Backfill DART financial fundamentals for registered DB securities."""

from __future__ import annotations

import argparse
import datetime as dt
import json

from app.modules.simulation.fundamentals_collector import FundamentalsCollector


def main() -> None:
    current_year = dt.date.today().year
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=current_year, help="Start business year (default: current year)")
    parser.add_argument("--end-year", type=int, default=current_year, help="End business year (default: current year)")
    parser.add_argument("--codes", nargs="*", help="Optional six-digit security codes. If omitted, backfills all securities in DB.")
    parser.add_argument("--period-start", help="Simulation start date (YYYY-MM-DD) for period-optimized backfill.")
    parser.add_argument("--period-end", help="Simulation end date (YYYY-MM-DD) for period-optimized backfill.")
    args = parser.parse_args()

    collector = FundamentalsCollector()
    if args.period_start and args.period_end:
        result = collector.backfill_for_simulation_period(
            period_start=args.period_start,
            period_end=args.period_end,
            security_codes=args.codes,
        )
    else:
        result = collector.backfill(
            start_year=args.start_year,
            end_year=args.end_year,
            security_codes=args.codes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
