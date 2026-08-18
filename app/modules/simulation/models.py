"""
================================================================================
[Investory Engine Module] models.py
================================================================================
■ 전체 기능 설명:
  - 백테스트 엔진과 매매 전략에서 사용되는 핵심 도메인 데이터 모델(Data Classes)을 정의합니다.
================================================================================
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class Position:
    """개별 보유 종목 클래스"""
    security_id: int
    security_code: str
    security_name: str
    quantity: float
    average_buy_price: float
    current_price: float = 0.0
    acquired_date: Optional[str] = None
    additional_buy_count: int = 0

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.average_buy_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def return_rate(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.market_value - self.cost_basis) / self.cost_basis

@dataclass
class Portfolio:
    """계좌 및 포트폴리오 종합 관리 클래스"""
    variant_type: str
    cash_balance: float
    initial_capital: float
    positions: Dict[int, Position] = field(default_factory=dict)
    total_cash_inflow: float = 0.0
    cash_flow_history: List[dict] = field(default_factory=list)

    @property
    def holdings_market_value(self) -> float:
        return sum(pos.market_value for pos in self.positions.values())

    @property
    def total_equity(self) -> float:
        return self.cash_balance + self.holdings_market_value

    @property
    def adjusted_initial_capital(self) -> float:
        return self.initial_capital + self.total_cash_inflow

    @property
    def cumulative_return(self) -> float:
        base = self.adjusted_initial_capital
        if base == 0:
            return 0.0
        return (self.total_equity - base) / base

    def add_cash(self, amount: float, flow_date: Optional[str] = None):
        self.cash_balance += amount
        self.total_cash_inflow += amount
        self.cash_flow_history.append({"date": flow_date, "amount": amount})

    def update_prices(self, price_map: Dict[int, float]):
        for sec_id, pos in self.positions.items():
            if sec_id in price_map:
                pos.current_price = price_map[sec_id]

@dataclass
class VirtualOrder:
    """투자봇이 장마감 후 발행하는 가상 주문 요청 객체"""
    order_id: str
    simulation_variant_id: int
    security_id: int
    trade_side: str
    quantity: float
    requested_price: float
    signal_date: str
    rationale: str
    triggered_principle_id: Optional[int] = None
    execution_policy: str = "NEXT_TRADING_DAY_OPEN"
    transaction_cost_amount: Optional[float] = None
    original_traded_at: Optional[str] = None
    reason_codes: List[str] = field(default_factory=list)
    target_weight: Optional[float] = None
    rationale_label_type: str = "UNCLASSIFIED"

@dataclass
class SimulatedTrade:
    """가상 체결 완료 내역 레코드"""
    simulated_trade_id: int
    simulation_variant_id: int
    security_id: int
    trade_side: str
    traded_at: str
    quantity: float
    unit_price: float
    transaction_cost_amount: float
    decision_reason: str
    security_code: str = ""
    security_name: str = ""
    triggered_principle_set_item_id: Optional[int] = None
    execution_policy: str = "NEXT_TRADING_DAY_OPEN"
    original_traded_at: Optional[str] = None
    applied_trading_date: Optional[str] = None
    rationale_label_type: str = "UNCLASSIFIED"

@dataclass
class DailySnapshot:
    """일별 계좌 성과 스냅샷 (그래프 및 결과 화면 출력용)"""
    simulation_variant_id: int
    performance_date: str
    cash_balance: float
    holdings_market_value: float
    portfolio_value: float
    daily_return: float
    cumulative_return: float
    drawdown_rate: float
    net_cash_flow: float = 0.0


@dataclass
class OrderAudit:
    """주문 요청부터 승인·조정·거절까지의 검증 기록."""

    simulation_variant_id: int
    order_id: str
    security_id: int
    action: str
    signal_date: str
    execution_date: Optional[str]
    requested_quantity: float
    approved_quantity: float
    status: str
    reason_codes: List[str] = field(default_factory=list)
