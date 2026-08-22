"""Rebuild an account's daily holding snapshots from its trade history.

The broker sync writes a snapshot for the days it has run, so an account
connected today has holdings for today and nothing before it. The simulation
needs a snapshot from before its start date to know what the account held, and
without one the eligible period collapses to a day or two.

holding_snapshots is a derived table: it is reconstructed from trades, so
rebuilding it is the intended repair, not a workaround. Days the broker
reported are left alone by default -- those came from the broker and are more
accurate than anything reconstructed from end-of-day closes.

    python scripts/backfill_holding_snapshots.py --account 5
    python scripts/backfill_holding_snapshots.py --account 5 --apply
    python scripts/backfill_holding_snapshots.py --account 5 --apply --overwrite-synced
"""

from __future__ import annotations

import argparse

import sys
from pathlib import Path

# 스크립트를 직접 실행해도 app 패키지를 찾도록 프로젝트 루트를 경로에 추가합니다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.simulation.persistence.db_persistence import get_db_connection
from app.modules.simulation.persistence.holding_snapshot_service import (
    HoldingSnapshotBackfillService,
    SnapshotReconstructionError,
)


def _synced_dates(account_id: int) -> set:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT snapshot_date FROM holding_snapshots WHERE account_id = %s",
                (account_id,),
            )
            return {str(row[0]) for row in cur.fetchall()}
    finally:
        conn.close()


def _insert(account_id: int, snapshots: list) -> int:
    conn = get_db_connection()
    inserted = 0
    try:
        with conn.cursor() as cur:
            for item in snapshots:
                cur.execute(
                    """
                    INSERT INTO holding_snapshots
                    (account_id, security_id, snapshot_date, quantity, average_cost,
                     market_value, unrealized_pnl, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (account_id, item["securityId"], item["snapshotDate"], item["quantity"],
                     item["averageCost"], item["marketValue"], item["unrealizedPnl"]),
                )
                inserted += 1
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="계좌의 과거 보유 스냅샷을 거래 내역에서 복원합니다.")
    parser.add_argument("--account", type=int, required=True, help="대상 account_id")
    parser.add_argument("--apply", action="store_true", help="실제로 저장합니다. 없으면 미리보기만 합니다.")
    parser.add_argument(
        "--overwrite-synced",
        action="store_true",
        help="증권사가 직접 준 날짜도 재구성 값으로 덮어씁니다. 기본은 보존입니다.",
    )
    args = parser.parse_args()

    try:
        built = HoldingSnapshotBackfillService().build(args.account)
    except SnapshotReconstructionError as error:
        print(f"복원 불가: {error}")
        return 1

    synced = set() if args.overwrite_synced else _synced_dates(args.account)
    pending = [item for item in built["snapshots"] if str(item["snapshotDate"]) not in synced]
    dates = sorted({str(item["snapshotDate"]) for item in pending})

    print(f"계좌 {args.account} · 기준일 {built['anchorDate']}")
    print(f"재구성 {built['snapshotRows']}행 / {built['snapshotDates']}일")
    print(f"품질 보정 {built['qualityAdjustmentCount']}건")
    if synced:
        print(f"보존할 동기화 날짜 {len(synced)}일: {sorted(synced)}")
    print(f"삽입 대상 {len(pending)}행 / {len(dates)}일" + (f" ({dates[0]} ~ {dates[-1]})" if dates else ""))

    if not args.apply:
        print("\n미리보기입니다. 실제로 저장하려면 --apply 를 붙이세요.")
        return 0
    if not pending:
        print("\n삽입할 행이 없습니다.")
        return 0

    inserted = _insert(args.account, pending)
    print(f"\n{inserted}행 삽입 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
