"""
================================================================================
[Investory Engine Module] capital_calculator.py
================================================================================
■ 전체 기능 설명:
  - ERD 명세(holding_snapshots) 및 시뮬레이션 구현 계획에 따라, 선택한 시작일(start_date)
    기준 보유 종목의 평가 금액 합계(market_value 합계)를 자동 계산하여 시뮬레이션 초기 자금으로 산출합니다.
================================================================================
"""

from datetime import date
from typing import Dict, Optional

from app.modules.simulation.persistence.repository import SimulationDataError, SimulationRepository

class InitialCapitalCalculator:
    """ERD holding_snapshots 기반 시뮬레이션 초기 자금 자동 계산기 클래스"""

    def __init__(self, data_path: Optional[str] = None):
        self.repository = SimulationRepository()

    def calculate(
        self,
        start_date: str = "2026-03-01",
        account_id: Optional[int] = None
    ) -> Dict[str, any]:
        """
        특정 시작일(start_date) 및 계좌ID 기준 보유종목 평가금액 합계 DB 연산
        """
        if account_id is None:
            raise SimulationDataError(
                "ACCOUNT_ID_REQUIRED",
                "초기자금 계산에 사용할 계좌 ID가 필요합니다.",
            )
        target_account_id = account_id
        snapshot = self.repository.load_initial_snapshot(target_account_id, start_date)
        try:
            requested_date = date.fromisoformat(start_date)
            snapshot_date = date.fromisoformat(snapshot["snapshotDate"])
        except (TypeError, ValueError) as error:
            raise SimulationDataError(
                "INVALID_INITIAL_CAPITAL_DATE",
                "초기자금 계산 날짜 형식이 올바르지 않습니다.",
                {"startDate": start_date, "snapshotDate": snapshot.get("snapshotDate")},
            ) from error
        if snapshot_date >= requested_date:
            raise SimulationDataError(
                "INITIAL_SNAPSHOT_NOT_BEFORE_START",
                "초기자금 스냅샷은 시뮬레이션 시작일보다 이전이어야 합니다.",
                {
                    "accountId": target_account_id,
                    "startDate": start_date,
                    "snapshotDate": snapshot["snapshotDate"],
                },
            )
        return {
            "startDate": start_date,
            "snapshotDate": snapshot["snapshotDate"],
            "accountId": target_account_id,
            "totalInitialCapital": snapshot["initialCapital"],
            "totalHoldingsCount": snapshot["holdingsCount"],
            "totalUnrealizedPnl": round(
                sum(item["unrealizedPnl"] for item in snapshot["holdings"]), 2
            ),
            "calculationPolicy": snapshot["calculationPolicy"],
            "policyDescription": "시뮬레이션 시작일 직전 보유 스냅샷의 평가금액 합계",
            "holdings": snapshot["holdings"],
        }
