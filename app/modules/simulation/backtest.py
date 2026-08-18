"""
================================================================================
[Investory Engine Module] backtest.py
================================================================================
■ 전체 기능 설명:
  - 지정된 시뮬레이션 기간동안 날짜(일봉) 단위로 루프를 도는 결정론적 백테스트 이벤트 루프 엔진입니다.
================================================================================
"""

from copy import deepcopy
from typing import Dict, List, Optional, Tuple
from app.modules.simulation.models import (
    Portfolio, Position, VirtualOrder, SimulatedTrade, DailySnapshot, OrderAudit
)
from app.modules.simulation.strategies import BaseStrategy

class BacktestEngine:
    """결정론적 초고속 백테스트 이벤트 루프 엔진 클래스"""
    
    SLIPPAGE_RATE = 0.001       # 0.1% 체결 슬리피지
    BUY_FEE_RATE = 0.00015      # 0.015% 매수 증권사 수수료
    SELL_FEE_RATE = 0.00215     # 0.015% 매도 수수료 + 0.20% 증권거래세 합계

    def __init__(
        self,
        simulation_run_id: int,
        period_start: str,
        period_end: str,
        initial_capital: float,
        securities_map: Dict[int, dict],
        daily_prices: List[dict]
    ):
        self.simulation_run_id = simulation_run_id
        self.period_start = period_start
        self.period_end = period_end
        self.initial_capital = initial_capital
        self.securities_map = securities_map
        self.allowed_security_ids = frozenset(securities_map)

        self.price_by_date: Dict[str, Dict[int, dict]] = {}
        for p in daily_prices:
            dt = p["priceDate"]
            sec_id = p["securityId"]
            if sec_id not in self.allowed_security_ids:
                continue
            if dt not in self.price_by_date:
                self.price_by_date[dt] = {}
            self.price_by_date[dt][sec_id] = p

        self.trading_days = sorted(list(self.price_by_date.keys()))
        self.portfolios: Dict[int, Portfolio] = {}
        self.strategies: Dict[int, BaseStrategy] = {}
        self.peak_equities: Dict[int, float] = {}
        self.pending_orders: Dict[int, List[VirtualOrder]] = {}
        self.executed_trades: List[SimulatedTrade] = []
        self.daily_snapshots: List[DailySnapshot] = []
        self.position_snapshots: List[dict] = []
        self.order_audits: List[OrderAudit] = []
        self.screening_audits: List[dict] = []
        self.initial_positions_by_variant: Dict[int, Dict[int, Position]] = {}
        self.trade_counter = 5000

    def register_variant(
        self,
        variant_id: int,
        strategy: BaseStrategy,
        initial_positions: Optional[Dict[int, Position]] = None,
        initial_cash: Optional[float] = None,
    ):
        self.strategies[variant_id] = strategy
        allowed_initial_positions = {
            security_id: position
            for security_id, position in (initial_positions or {}).items()
            if security_id in self.allowed_security_ids
        }
        self.portfolios[variant_id] = Portfolio(
            variant_type=strategy.variant_type,
            cash_balance=self.initial_capital if initial_cash is None else initial_cash,
            initial_capital=self.initial_capital,
            positions=deepcopy(allowed_initial_positions),
        )
        self.initial_positions_by_variant[variant_id] = deepcopy(allowed_initial_positions)
        self.peak_equities[variant_id] = self.initial_capital
        self.pending_orders[variant_id] = []

    def run(self, disclosures_by_date: Optional[dict] = None) -> Tuple[List[SimulatedTrade], List[DailySnapshot]]:
        self.pending_orders = {vid: [] for vid in self.portfolios.keys()}
        self.executed_trades = []
        self.daily_snapshots = []
        self.position_snapshots = []
        self.order_audits = []
        self.screening_audits = []
        self.peak_equities = {vid: p.total_equity for vid, p in self.portfolios.items()}
        prev_equities = {vid: p.total_equity for vid, p in self.portfolios.items()}
        cumulative_growth = {vid: 1.0 for vid in self.portfolios}
        baseline_recorded = {vid: False for vid in self.portfolios}
        disc_map = disclosures_by_date or {}

        for date_idx, current_date in enumerate(self.trading_days):
            if current_date < self.period_start or current_date > self.period_end:
                continue

            prices_today = self.price_by_date.get(current_date, {})
            disclosures_today = disc_map.get(current_date, {})
            cash_flows_today = {vid: 0.0 for vid in self.portfolios}

            for variant_id, orders in list(self.pending_orders.items()):
                if orders:
                    executed, flows = self._execute_orders(
                        variant_id=variant_id,
                        orders=orders,
                        execution_date=current_date,
                        prices_today=prices_today
                    )
                    self.executed_trades.extend(executed)
                    for flow_vid, amount in flows.items():
                        cash_flows_today[flow_vid] += amount
                    self.pending_orders[variant_id] = []

            price_map_today = {sec_id: p["closePrice"] for sec_id, p in prices_today.items()}
            for vid, portfolio in self.portfolios.items():
                portfolio.update_prices(price_map_today)

            for vid, strategy in self.strategies.items():
                portfolio = self.portfolios[vid]
                strategy.last_screening_audit = None
                signals = strategy.generate_signals(
                    current_date=current_date,
                    portfolio=portfolio,
                    daily_prices_today=prices_today,
                    securities_map=self.securities_map,
                    context={"disclosures_today": disclosures_today}
                )
                if strategy.last_screening_audit:
                    self.screening_audits.append(dict(strategy.last_screening_audit))
                if strategy.variant_type == "ACTUAL_USER" and signals:
                    executed, flows = self._execute_orders(
                        variant_id=vid,
                        orders=signals,
                        execution_date=current_date,
                        prices_today=prices_today,
                    )
                    self.executed_trades.extend(executed)
                    for flow_vid, amount in flows.items():
                        cash_flows_today[flow_vid] += amount
                    portfolio.update_prices(price_map_today)
                else:
                    self.pending_orders[vid].extend(signals)

            for vid, portfolio in self.portfolios.items():
                curr_equity = portfolio.total_equity
                prev_eq = prev_equities[vid]
                external_flow = cash_flows_today[vid]
                if not baseline_recorded[vid]:
                    # The first displayed point is the common 0% baseline. In
                    # particular, do not count the valuation gap between an
                    # older holding snapshot and the first simulation close as
                    # investment performance.
                    daily_ret = 0.0
                    cumulative_growth[vid] = 1.0
                    self.peak_equities[vid] = curr_equity
                    drawdown = 0.0
                    baseline_recorded[vid] = True
                else:
                    daily_ret = (curr_equity - prev_eq - external_flow) / prev_eq if prev_eq > 0 else 0.0
                    cumulative_growth[vid] *= 1.0 + daily_ret

                    # 외부 입금은 성과가 아니므로 고점 기준에도 동일 금액을 더해 MDD를 중립화한다.
                    if external_flow:
                        self.peak_equities[vid] += external_flow

                    if curr_equity > self.peak_equities[vid]:
                        self.peak_equities[vid] = curr_equity

                    peak = self.peak_equities[vid]
                    drawdown = (curr_equity - peak) / peak if peak > 0 else 0.0

                snapshot = DailySnapshot(
                    simulation_variant_id=vid,
                    performance_date=current_date,
                    cash_balance=portfolio.cash_balance,
                    holdings_market_value=portfolio.holdings_market_value,
                    portfolio_value=curr_equity,
                    daily_return=round(daily_ret, 6),
                    # Store rate fields as decimal ratios (0.01 == 1%).
                    # Percentage conversion belongs exclusively to the API layer.
                    cumulative_return=round(cumulative_growth[vid] - 1.0, 6),
                    drawdown_rate=round(drawdown, 6),
                    net_cash_flow=round(external_flow, 2),
                )
                self.daily_snapshots.append(snapshot)
                for position in portfolio.positions.values():
                    self.position_snapshots.append({
                        "simulationVariantId": vid,
                        "snapshotDate": current_date,
                        "securityId": position.security_id,
                        "securityCode": position.security_code,
                        "securityName": position.security_name,
                        "quantity": round(position.quantity, 8),
                        "averagePrice": round(position.average_buy_price, 2),
                        "currentPrice": round(position.current_price, 2),
                        "marketValue": round(position.market_value, 2),
                        "unrealizedPnl": round(position.unrealized_pnl, 2),
                        "returnPercent": round(position.return_rate * 100, 4),
                    })
                prev_equities[vid] = curr_equity

        # 마지막 거래일 장 마감 후 생성된 주문은 t+1 체결일이 없으므로 명시적으로 거절 기록을 남긴다.
        for variant_id, orders in self.pending_orders.items():
            for order in orders:
                self.order_audits.append(OrderAudit(
                    simulation_variant_id=variant_id,
                    order_id=order.order_id,
                    security_id=order.security_id,
                    action=order.trade_side,
                    signal_date=order.signal_date,
                    execution_date=None,
                    requested_quantity=order.quantity,
                    approved_quantity=0.0,
                    status="REJECTED",
                    reason_codes=["NO_NEXT_TRADING_DAY"],
                ))

        return self.executed_trades, self.daily_snapshots

    def _execute_orders(
        self,
        variant_id: int,
        orders: List[VirtualOrder],
        execution_date: str,
        prices_today: Dict[int, dict]
    ) -> Tuple[List[SimulatedTrade], Dict[int, float]]:
        executed = []
        external_flows: Dict[int, float] = {vid: 0.0 for vid in self.portfolios}
        portfolio = self.portfolios[variant_id]

        for order in orders:
            sec_id = order.security_id
            if sec_id not in self.allowed_security_ids:
                self._record_order_audit(order, execution_date, 0.0, "REJECTED", ["SECURITY_NOT_IN_DB_UNIVERSE"])
                continue
            if sec_id not in prices_today:
                self._record_order_audit(order, execution_date, 0.0, "REJECTED", ["PRICE_NOT_AVAILABLE"])
                continue

            open_price = prices_today[sec_id]["openPrice"]
            trade_side = order.trade_side

            if trade_side == "BUY" or trade_side == "ADD":
                is_actual_fill = order.execution_policy == "DATABASE_ACTUAL_FILL"
                unit_price = order.requested_price if is_actual_fill else open_price * (1.0 + self.SLIPPAGE_RATE)
                gross_amount = order.quantity * unit_price
                fee = (
                    float(order.transaction_cost_amount or 0.0)
                    if is_actual_fill
                    else gross_amount * self.BUY_FEE_RATE
                )
                total_cost = gross_amount + fee

                if portfolio.variant_type == "ACTUAL_USER" and portfolio.cash_balance < total_cost:
                    deficit = total_cost - portfolio.cash_balance
                    for p in self.portfolios.values():
                        p.add_cash(deficit, execution_date)
                    for flow_vid in external_flows:
                        external_flows[flow_vid] += deficit

                if portfolio.cash_balance < total_cost:
                    available_qty = int(portfolio.cash_balance / (unit_price * (1 + self.BUY_FEE_RATE)))
                    if available_qty <= 0:
                        self._record_order_audit(order, execution_date, 0.0, "REJECTED", ["INSUFFICIENT_CASH"])
                        continue
                    requested_qty = order.quantity
                    order.quantity = float(available_qty)
                    gross_amount = order.quantity * unit_price
                    fee = (
                        float(order.transaction_cost_amount or 0.0)
                        if is_actual_fill
                        else gross_amount * self.BUY_FEE_RATE
                    )
                    total_cost = gross_amount + fee
                    adjustment_codes = ["QUANTITY_REDUCED_INSUFFICIENT_CASH"]
                else:
                    requested_qty = order.quantity
                    adjustment_codes = []

                portfolio.cash_balance -= total_cost
                if sec_id in portfolio.positions:
                    pos = portfolio.positions[sec_id]
                    new_qty = pos.quantity + order.quantity
                    new_avg = ((pos.quantity * pos.average_buy_price) + gross_amount) / new_qty
                    pos.quantity = new_qty
                    pos.average_buy_price = new_avg
                    pos.current_price = prices_today[sec_id]["closePrice"]
                    if trade_side == "ADD":
                        pos.additional_buy_count += 1
                else:
                    sec_info = self.securities_map.get(sec_id, {})
                    portfolio.positions[sec_id] = Position(
                        security_id=sec_id,
                        security_code=sec_info.get("securityCode", ""),
                        security_name=sec_info.get("securityName", "알수없음"),
                        quantity=order.quantity,
                        average_buy_price=unit_price,
                        current_price=prices_today[sec_id]["closePrice"],
                        acquired_date=execution_date,
                    )

                self.trade_counter += 1
                executed.append(
                    SimulatedTrade(
                        simulated_trade_id=self.trade_counter,
                        simulation_variant_id=variant_id,
                        security_id=sec_id,
                        trade_side=trade_side,
                        traded_at=order.original_traded_at or f"{execution_date}T09:00:00Z",
                        quantity=order.quantity,
                        unit_price=round(unit_price, 2),
                        transaction_cost_amount=round(fee, 2),
                        decision_reason=order.rationale,
                        security_code=self.securities_map.get(sec_id, {}).get("securityCode", ""),
                        security_name=self.securities_map.get(sec_id, {}).get("securityName", f"종목 {sec_id}"),
                        triggered_principle_set_item_id=order.triggered_principle_id,
                        execution_policy=order.execution_policy,
                        original_traded_at=order.original_traded_at,
                        applied_trading_date=execution_date,
                        rationale_label_type=order.rationale_label_type,
                    )
                )
                self._record_order_audit(
                    order,
                    execution_date,
                    order.quantity,
                    "ADJUSTED" if adjustment_codes else "EXECUTED",
                    list(order.reason_codes) + adjustment_codes,
                    requested_quantity=requested_qty,
                )

            elif trade_side == "SELL" or trade_side == "REDUCE":
                if sec_id not in portfolio.positions:
                    self._record_order_audit(order, execution_date, 0.0, "REJECTED", ["POSITION_NOT_FOUND"])
                    continue

                pos = portfolio.positions[sec_id]
                sell_qty = min(order.quantity, pos.quantity)
                if sell_qty <= 0:
                    self._record_order_audit(order, execution_date, 0.0, "REJECTED", ["INVALID_QUANTITY"])
                    continue

                is_actual_fill = order.execution_policy == "DATABASE_ACTUAL_FILL"
                unit_price = order.requested_price if is_actual_fill else open_price * (1.0 - self.SLIPPAGE_RATE)
                gross_amount = sell_qty * unit_price
                cost_amount = (
                    float(order.transaction_cost_amount or 0.0)
                    if is_actual_fill
                    else gross_amount * self.SELL_FEE_RATE
                )
                net_proceeds = gross_amount - cost_amount

                portfolio.cash_balance += net_proceeds
                pos.quantity -= sell_qty
                if pos.quantity <= 0:
                    del portfolio.positions[sec_id]

                self.trade_counter += 1
                executed.append(
                    SimulatedTrade(
                        simulated_trade_id=self.trade_counter,
                        simulation_variant_id=variant_id,
                        security_id=sec_id,
                        trade_side=trade_side,
                        traded_at=order.original_traded_at or f"{execution_date}T09:00:00Z",
                        quantity=sell_qty,
                        unit_price=round(unit_price, 2),
                        transaction_cost_amount=round(cost_amount, 2),
                        decision_reason=order.rationale,
                        security_code=self.securities_map.get(sec_id, {}).get("securityCode", ""),
                        security_name=self.securities_map.get(sec_id, {}).get("securityName", f"종목 {sec_id}"),
                        triggered_principle_set_item_id=order.triggered_principle_id,
                        execution_policy=order.execution_policy,
                        original_traded_at=order.original_traded_at,
                        applied_trading_date=execution_date,
                        rationale_label_type=order.rationale_label_type,
                    )
                )
                status = "ADJUSTED" if sell_qty < order.quantity else "EXECUTED"
                codes = list(order.reason_codes)
                if status == "ADJUSTED":
                    codes.append("QUANTITY_REDUCED_TO_POSITION")
                self._record_order_audit(order, execution_date, sell_qty, status, codes)

        return executed, external_flows

    def _record_order_audit(
        self,
        order: VirtualOrder,
        execution_date: Optional[str],
        approved_quantity: float,
        status: str,
        reason_codes: List[str],
        requested_quantity: Optional[float] = None,
    ) -> None:
        self.order_audits.append(OrderAudit(
            simulation_variant_id=order.simulation_variant_id,
            order_id=order.order_id,
            security_id=order.security_id,
            action=order.trade_side,
            signal_date=order.signal_date,
            execution_date=execution_date,
            requested_quantity=float(order.quantity if requested_quantity is None else requested_quantity),
            approved_quantity=float(approved_quantity),
            status=status,
            reason_codes=reason_codes,
        ))
