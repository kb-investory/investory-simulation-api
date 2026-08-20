"""
================================================================================
[Investory Engine Module] strategies.py
================================================================================
■ 전체 기능 설명:
  - 시뮬레이션에 참가하는 4개 대조군 봇의 매매 판단 알고리즘 클래스를 정의합니다.
================================================================================
""" 
import random
from bisect import bisect_left
from datetime import date
from typing import Dict, List, Optional
from app.modules.simulation.models import Portfolio, VirtualOrder
from app.modules.simulation.evaluator import StockEvaluator
from app.modules.simulation.rules.rule_schema import SelectionRule
from app.modules.simulation.strategy_catalog import VALUE_QUALITY_STRATEGY


def _security_label(security_id: int, securities_map: Dict[int, dict]) -> str:
    security = securities_map.get(security_id, {})
    name = security.get("securityName") or f"종목 {security_id}"
    code = security.get("securityCode")
    return f"{name}({code})" if code else name


def _as_percent(value: object) -> str:
    number = float(value)
    percent = number * 100 if abs(number) <= 2 else number
    return f"{percent:+.1f}%"


def _candidate_reason(candidate: dict, current_date: str, action: str) -> str:
    """후보 종목의 해당 시점 DB 수치를 사람이 검증 가능한 직접 근거로 만든다."""
    name = candidate.get("securityName") or f"종목 {candidate['securityId']}"
    if name.endswith("보통주"):
        name = name[:-3]
    code = candidate.get("securityCode")
    label = f"{name}({code})" if code else name
    price = candidate.get("priceInfo", {})
    sentences = []
    if price.get("closePrice") is not None:
        sentences.append(
            f"{label}를 {current_date} 종가 {float(price['closePrice']):,.0f}원 기준으로 {action}했습니다."
        )
    else:
        sentences.append(f"{label}를 {current_date} 기준으로 {action}했습니다.")

    profitability = []
    if price.get("roe") is not None:
        profitability.append(f"ROE {_as_percent(price['roe'])}")
    if price.get("revenueGrowth") is not None:
        profitability.append(f"매출 성장률 {_as_percent(price['revenueGrowth'])}")
    if price.get("earningsGrowth") is not None:
        profitability.append(f"이익 성장률 {_as_percent(price['earningsGrowth'])}")
    if profitability:
        sentences.append(f"수익성·성장 지표는 {', '.join(profitability)}입니다.")

    valuation = []
    if price.get("per") is not None:
        valuation.append(f"PER {float(price['per']):.1f}배")
    if price.get("pbr") is not None:
        valuation.append(f"PBR {float(price['pbr']):.1f}배")
    if price.get("operatingCashFlowPositive") is not None:
        valuation.append("영업현금흐름 흑자" if price["operatingCashFlowPositive"] else "영업현금흐름 적자")
    if valuation:
        sentences.append(f"확인한 재무 지표는 {', '.join(valuation)}입니다.")

    disclosure = candidate.get("disclosureInfo")
    if disclosure and disclosure.get("reportName"):
        sentences.append(f"당일 확인된 공시는 '{disclosure['reportName']}'입니다.")

    if price.get("day5Return") is not None:
        day5_return = float(price["day5Return"])
        day5_text = _as_percent(day5_return)
        if day5_return <= -0.15:
            sentences.append(f"다만 최근 5일 {day5_text} 하락해 단기 변동성에 주의가 필요합니다.")
        elif day5_return >= 0.15:
            sentences.append(f"다만 최근 5일 {day5_text} 상승해 추격 매수 위험에 주의가 필요합니다.")
        else:
            sentences.append(f"최근 5일 수익률은 {day5_text}입니다.")

    if len(sentences) == 1:
        sentences.append("해당 시점에 활용 가능한 DB 재무 지표가 없어 가격 정보만 반영했습니다.")
    return " ".join(sentences)

class BaseStrategy:
    """모든 투자 봇 전략 클래스의 기본 추상 인터페이스"""
    def __init__(self, variant_id: int, variant_type: str, variant_name: str):
        self.variant_id = variant_id
        self.variant_type = variant_type
        self.variant_name = variant_name
        self.last_screening_audit: Optional[dict] = None

    def generate_signals(
        self,
        current_date: str,
        portfolio: Portfolio,
        daily_prices_today: Dict[int, dict],
        securities_map: Dict[int, dict],
        context: Optional[dict] = None
    ) -> List[VirtualOrder]:
        raise NotImplementedError

class ActualUserStrategy(BaseStrategy):
    """1. 실제 사용자 과거 매매 내역 재현 전략 클래스"""
    def __init__(self, variant_id: int, actual_trades: List[dict], trading_days: Optional[List[str]] = None):
        super().__init__(variant_id, "ACTUAL_USER", "실제 나")
        self.trading_days = sorted(trading_days or [])
        self.actual_trades = [self._with_application_date(trade) for trade in actual_trades]

    def _with_application_date(self, trade: dict) -> dict:
        normalized = dict(trade)
        original_date = str(trade["tradedAt"])[:10]
        applied_date = original_date
        if self.trading_days:
            index = bisect_left(self.trading_days, original_date)
            if index >= len(self.trading_days):
                applied_date = ""
            else:
                applied_date = self.trading_days[index]
        normalized["appliedTradeDate"] = applied_date
        return normalized

    def generate_signals(
        self,
        current_date: str,
        portfolio: Portfolio,
        daily_prices_today: Dict[int, dict],
        securities_map: Dict[int, dict],
        context: Optional[dict] = None
    ) -> List[VirtualOrder]:
        orders = []
        for trade in self.actual_trades:
            trade_date = trade.get("appliedTradeDate") or trade["tradedAt"][:10]
            if trade_date == current_date:
                sec_id = trade["securityId"]
                side = trade["tradeSide"]
                qty = float(trade["quantity"])
                unit_price = float(trade["unitPrice"])
                original_date = trade["tradedAt"][:10]
                rationale = str(trade.get("rationaleText") or "").strip()
                if not rationale:
                    rationale = "사용자가 DB에 입력한 매매 근거 없음"
                if original_date != current_date:
                    rationale += f" (원 거래일 {original_date}, 반영 거래일 {current_date})"

                orders.append(
                    VirtualOrder(
                        order_id=f"actual_{trade['tradeId']}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side=side,
                        quantity=qty,
                        requested_price=unit_price,
                        signal_date=current_date,
                        rationale=rationale,
                        execution_policy="DATABASE_ACTUAL_FILL",
                        transaction_cost_amount=float(trade.get("transactionCostAmount", 0.0)),
                        original_traded_at=trade.get("tradedAt"),
                        rationale_label_type=str(trade.get("rationaleLabelType") or "UNCLASSIFIED"),
                    )
                )
        return orders

class PersonalBotStrategy(BaseStrategy):
    """2. 사용자 확정 원칙 및 6축 성향 기반 개인 투자봇 전략 클래스"""
    def __init__(self, variant_id: int, principle_items: List[dict], rule_schema: Optional[dict] = None):
        super().__init__(variant_id, "PERSONAL_BOT", "나의 투자봇 v1")
        self.principle_items = principle_items
        self.rule_schema = rule_schema or {}
        self.evaluator = StockEvaluator()
        self.last_rebalance_month: Optional[str] = None

    @staticmethod
    def _holding_days(acquired_date: Optional[str], current_date: str) -> int:
        if not acquired_date:
            return 0
        return (date.fromisoformat(current_date) - date.fromisoformat(acquired_date)).days

    @staticmethod
    def _position_weight(portfolio: Portfolio, security_id: int) -> float:
        if portfolio.total_equity <= 0 or security_id not in portfolio.positions:
            return 0.0
        return portfolio.positions[security_id].market_value / portfolio.total_equity

    @staticmethod
    def _sector_weight(portfolio: Portfolio, sector: str, securities_map: Dict[int, dict]) -> float:
        if portfolio.total_equity <= 0:
            return 0.0
        value = sum(
            position.market_value
            for security_id, position in portfolio.positions.items()
            if securities_map.get(security_id, {}).get("sectorName") == sector
        )
        return value / portfolio.total_equity

    def generate_signals(
        self,
        current_date: str,
        portfolio: Portfolio,
        daily_prices_today: Dict[int, dict],
        securities_map: Dict[int, dict],
        context: Optional[dict] = None
    ) -> List[VirtualOrder]:
        orders = []

        exit_rule = self.rule_schema.get("exit", {})
        tp_rate = float(exit_rule.get("take_profit_rate", 0.20))
        sl_rate = float(exit_rule.get("stop_loss_rate", -0.10))
        sell_on_neg_disc = exit_rule.get("sell_on_negative_disclosure", True)
        max_holding_days = int(exit_rule.get("max_holding_days", 90))
        portfolio_rule = self.rule_schema.get("portfolio", {})
        max_position_count = int(portfolio_rule.get("max_position_count", 5))
        max_single_weight = float(portfolio_rule.get("max_single_position_weight", 0.20))
        max_sector_weight = float(portfolio_rule.get("max_sector_weight", 0.40))
        rebalance_rule = self.rule_schema.get("rebalance", {})
        rebalance_period = str(rebalance_rule.get("period", "MONTHLY")).upper()
        min_rebalance_days = int(rebalance_rule.get("min_holding_days_before_rebalance", 14))

        disclosures_today = (context or {}).get("disclosures_today", {})
        exiting_security_ids = set()

        for sec_id, pos in list(portfolio.positions.items()):
            if sec_id in daily_prices_today:
                curr_price = daily_prices_today[sec_id]["closePrice"]
                pos.current_price = curr_price
                return_rate = pos.return_rate
                disc_info = disclosures_today.get(sec_id)

                if sell_on_neg_disc and disc_info and disc_info.get("direction") == "NEGATIVE":
                    security_label = _security_label(sec_id, securities_map)
                    exiting_security_ids.add(sec_id)
                    orders.append(
                        VirtualOrder(
                            order_id=f"personal_negdisc_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="SELL",
                            quantity=pos.quantity,
                            requested_price=curr_price,
                            signal_date=current_date,
                            rationale=f"{security_label} 매도: 악재 공시 '{disc_info.get('reportName')}' 확인 ({disc_info.get('analysisReason')})",
                            reason_codes=["EXIT_NEGATIVE_DISCLOSURE"],
                        )
                    )
                elif return_rate >= tp_rate:
                    security_label = _security_label(sec_id, securities_map)
                    exiting_security_ids.add(sec_id)
                    orders.append(
                        VirtualOrder(
                            order_id=f"personal_sell_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="SELL",
                            quantity=pos.quantity,
                            requested_price=curr_price,
                            signal_date=current_date,
                            rationale=(f"{security_label} 매도: 평균매입가 {pos.average_buy_price:,.0f}원 대비 "
                                       f"현재가 {curr_price:,.0f}원, 수익률 +{return_rate*100:.1f}%로 "
                                       f"목표 +{tp_rate*100:.1f}% 도달"),
                            triggered_principle_id=1002,
                            reason_codes=["EXIT_TAKE_PROFIT"],
                        )
                    )
                elif return_rate <= sl_rate:
                    security_label = _security_label(sec_id, securities_map)
                    exiting_security_ids.add(sec_id)
                    orders.append(
                        VirtualOrder(
                            order_id=f"personal_stoploss_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="SELL",
                            quantity=pos.quantity,
                            requested_price=curr_price,
                            signal_date=current_date,
                            rationale=(f"{security_label} 매도: 평균매입가 {pos.average_buy_price:,.0f}원 대비 "
                                       f"현재가 {curr_price:,.0f}원, 수익률 {return_rate*100:.1f}%로 "
                                       f"손절 기준 {sl_rate*100:.1f}% 도달"),
                            triggered_principle_id=1003,
                            reason_codes=["EXIT_STOP_LOSS"],
                        )
                    )
                elif self._holding_days(pos.acquired_date, current_date) >= max_holding_days:
                    security_label = _security_label(sec_id, securities_map)
                    exiting_security_ids.add(sec_id)
                    orders.append(VirtualOrder(
                        order_id=f"personal_maxholding_{sec_id}_{current_date}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side="SELL",
                        quantity=pos.quantity,
                        requested_price=curr_price,
                        signal_date=current_date,
                        rationale=(f"{security_label} 매도: {pos.acquired_date} 매수 후 "
                                   f"{self._holding_days(pos.acquired_date, current_date)}일 보유하여 "
                                   f"최대 보유 기간 {max_holding_days}일 도달"),
                        reason_codes=["EXIT_MAX_HOLDING_DAYS"],
                    ))

        current_month = current_date[:7]
        should_rebalance = rebalance_period == "MONTHLY" and self.last_rebalance_month not in (None, current_month)
        if self.last_rebalance_month is None:
            self.last_rebalance_month = current_month
        elif should_rebalance:
            self.last_rebalance_month = current_month
            eligible_positions = [
                (sec_id, pos)
                for sec_id, pos in portfolio.positions.items()
                if sec_id not in exiting_security_ids
                and self._holding_days(pos.acquired_date, current_date) >= min_rebalance_days
            ]
            projected_position_count = len(portfolio.positions) - len(exiting_security_ids)
            excess_count = max(0, projected_position_count - max_position_count)
            count_reduction_ids = {
                sec_id
                for sec_id, _ in sorted(eligible_positions, key=lambda item: item[1].market_value)[:excess_count]
            }
            for sec_id, pos in list(portfolio.positions.items()):
                if sec_id in exiting_security_ids:
                    continue
                if self._holding_days(pos.acquired_date, current_date) < min_rebalance_days:
                    continue
                if sec_id in count_reduction_ids and sec_id in daily_prices_today:
                    exiting_security_ids.add(sec_id)
                    orders.append(VirtualOrder(
                        order_id=f"personal_rebalance_count_{sec_id}_{current_date}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side="SELL",
                        quantity=pos.quantity,
                        requested_price=daily_prices_today[sec_id]["closePrice"],
                        signal_date=current_date,
                        rationale=(f"{_security_label(sec_id, securities_map)} 매도: 월간 점검일 "
                                   f"보유 종목 {projected_position_count}개로 최대 {max_position_count}개를 "
                                   f"초과해 보유금액이 작은 종목부터 정리"),
                        reason_codes=["REBALANCE_MAX_POSITION_COUNT"],
                    ))
                    continue
                weight = self._position_weight(portfolio, sec_id)
                sector = securities_map.get(sec_id, {}).get("sectorName", "")
                sector_weight = self._sector_weight(portfolio, sector, securities_map)
                sector_scale = min(1.0, max_sector_weight / sector_weight) if sector_weight > 0 else 1.0
                target_value = min(
                    portfolio.total_equity * max_single_weight,
                    pos.market_value * sector_scale,
                )
                if pos.market_value > target_value and sec_id in daily_prices_today:
                    reduce_qty = int(max(0.0, pos.market_value - target_value) / daily_prices_today[sec_id]["closePrice"])
                    if reduce_qty > 0:
                        reason_codes = []
                        if weight > max_single_weight:
                            reason_codes.append("REBALANCE_MAX_POSITION_WEIGHT")
                        if sector_weight > max_sector_weight:
                            reason_codes.append("REBALANCE_MAX_SECTOR_WEIGHT")
                        orders.append(VirtualOrder(
                            order_id=f"personal_rebalance_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="REDUCE",
                            quantity=float(reduce_qty),
                            requested_price=daily_prices_today[sec_id]["closePrice"],
                            signal_date=current_date,
                            rationale=(f"{_security_label(sec_id, securities_map)} 일부 매도: 현재 종목 비중 "
                                       f"{weight*100:.1f}%(한도 {max_single_weight*100:.1f}%), "
                                       f"{sector or '미분류'} 업종 비중 {sector_weight*100:.1f}%"
                                       f"(한도 {max_sector_weight*100:.1f}%)"),
                            reason_codes=reason_codes,
                            target_weight=target_value / portfolio.total_equity if portfolio.total_equity > 0 else 0.0,
                        ))

        sel_rule_dict = self.rule_schema.get("selection", {})
        selection_rule = SelectionRule(
            factor_weights=sel_rule_dict.get("factor_weights", SelectionRule().factor_weights),
            min_passing_score=sel_rule_dict.get("min_passing_score", 70.0)
        )

        universe_rule = self.rule_schema.get("universe", {})
        entry_rule = self.rule_schema.get("entry", {})
        candidates = self.evaluator.screen_candidates(
            daily_prices_today,
            securities_map,
            selection_rule,
            disclosures_today,
            universe_rule=universe_rule,
            entry_rule=entry_rule,
        )
        self.last_screening_audit = {
            "simulationVariantId": self.variant_id,
            "variantType": self.variant_type,
            "screeningDate": current_date,
            **self.evaluator.last_screening_audit,
        }

        additional_rule = self.rule_schema.get("additional_buy", {})
        if additional_rule.get("allowed", True):
            for candidate in candidates:
                sec_id = candidate["securityId"]
                if sec_id not in portfolio.positions or sec_id in exiting_security_ids:
                    continue
                pos = portfolio.positions[sec_id]
                if pos.additional_buy_count >= int(additional_rule.get("max_additional_count", 2)):
                    continue
                if pos.return_rate > float(additional_rule.get("trigger_drop_rate", -0.05)):
                    continue
                current_weight = self._position_weight(portfolio, sec_id)
                add_weight = min(
                    float(additional_rule.get("additional_weight", 0.05)),
                    max(0.0, max_single_weight - current_weight),
                )
                price = candidate["priceInfo"]["closePrice"]
                quantity = int(portfolio.total_equity * add_weight / price)
                if quantity > 0 and portfolio.cash_balance >= quantity * price:
                    orders.append(VirtualOrder(
                        order_id=f"personal_add_{sec_id}_{current_date}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side="ADD",
                        quantity=float(quantity),
                        requested_price=price,
                        signal_date=current_date,
                        rationale=(f"{_security_label(sec_id, securities_map)} 추가매수: 평균매입가 "
                                   f"{pos.average_buy_price:,.0f}원 대비 현재가 {price:,.0f}원으로 "
                                   f"{pos.return_rate*100:.1f}% 하락, 추가매수 기준 "
                                   f"{float(additional_rule.get('trigger_drop_rate', -0.05))*100:.1f}% 도달"),
                        reason_codes=["ADDITIONAL_BUY_TRIGGER_DROP"],
                        target_weight=current_weight + add_weight,
                    ))
                    return orders

        for candidate in candidates:
            sec_id = candidate["securityId"]
            if sec_id not in portfolio.positions:
                if len(portfolio.positions) >= max_position_count:
                    continue
                close_price = candidate["priceInfo"]["closePrice"]
                sector = securities_map.get(sec_id, {}).get("sectorName", "")
                sector_room = max(0.0, max_sector_weight - self._sector_weight(portfolio, sector, securities_map))
                target_weight = min(0.15, max_single_weight, sector_room)
                max_buy_amount = portfolio.total_equity * target_weight

                if portfolio.cash_balance >= close_price:
                    buy_qty = int(max_buy_amount / close_price)
                    if buy_qty > 0:
                        reason = _candidate_reason(candidate, current_date, "매수")

                        orders.append(
                            VirtualOrder(
                                order_id=f"personal_buy_{sec_id}_{current_date}",
                                simulation_variant_id=self.variant_id,
                                security_id=sec_id,
                                trade_side="BUY",
                                quantity=float(buy_qty),
                                requested_price=close_price,
                                signal_date=current_date,
                                rationale=reason,
                                triggered_principle_id=1001,
                                reason_codes=["UNIVERSE_PASSED", "ENTRY_PASSED", "SELECTION_SCORE_PASSED", "PORTFOLIO_LIMITS_PASSED"],
                                target_weight=target_weight,
                            )
                        )
                        break

        return orders

class FamousStrategyBot(BaseStrategy):
    """3. 유명 투자 전략 봇 (우량 가치·품질 퀀트 고정 전략)"""
    def __init__(self, variant_id: int):
        super().__init__(variant_id, "FAMOUS_STRATEGY", VALUE_QUALITY_STRATEGY["variantName"])
        self.evaluator = StockEvaluator()

    def generate_signals(
        self,
        current_date: str,
        portfolio: Portfolio,
        daily_prices_today: Dict[int, dict],
        securities_map: Dict[int, dict],
        context: Optional[dict] = None
    ) -> List[VirtualOrder]:
        orders = []

        for sec_id, pos in list(portfolio.positions.items()):
            if sec_id in daily_prices_today:
                curr_price = daily_prices_today[sec_id]["closePrice"]
                pos.current_price = curr_price
                return_rate = pos.return_rate

                take_profit_rate = VALUE_QUALITY_STRATEGY["exit"]["take_profit_rate"]
                if return_rate >= take_profit_rate:
                    security_label = _security_label(sec_id, securities_map)
                    orders.append(
                        VirtualOrder(
                            order_id=f"famous_sell_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="SELL",
                            quantity=pos.quantity,
                            requested_price=curr_price,
                            signal_date=current_date,
                            rationale=(f"{security_label} 매도: 평균매입가 {pos.average_buy_price:,.0f}원 대비 "
                                       f"현재가 {curr_price:,.0f}원, 수익률 +{return_rate*100:.1f}%로 "
                                       f"매도 기준 +{take_profit_rate*100:.1f}% 도달")
                        )
                    )

        famous_selection = SelectionRule(
            factor_weights=VALUE_QUALITY_STRATEGY["selection"]["factor_weights"],
            min_passing_score=VALUE_QUALITY_STRATEGY["selection"]["min_passing_score"],
        )
        candidates = self.evaluator.screen_candidates(
            daily_prices_today,
            securities_map,
            famous_selection,
            universe_rule=VALUE_QUALITY_STRATEGY["universe"],
            entry_rule=VALUE_QUALITY_STRATEGY["entry"],
            required_factors=("value", "quality"),
        )
        self.last_screening_audit = {
            "simulationVariantId": self.variant_id,
            "variantType": self.variant_type,
            "screeningDate": current_date,
            **self.evaluator.last_screening_audit,
        }

        for candidate in candidates:
            sec_id = candidate["securityId"]
            if sec_id not in portfolio.positions:
                close_price = candidate["priceInfo"]["closePrice"]
                buy_amount = portfolio.total_equity * VALUE_QUALITY_STRATEGY["portfolio"]["target_weight"]
                buy_qty = int(buy_amount / close_price)
                if buy_qty > 0 and portfolio.cash_balance >= close_price * buy_qty:
                    orders.append(
                        VirtualOrder(
                            order_id=f"famous_buy_{sec_id}_{current_date}",
                            simulation_variant_id=self.variant_id,
                            security_id=sec_id,
                            trade_side="BUY",
                            quantity=float(buy_qty),
                            requested_price=close_price,
                            signal_date=current_date,
                            rationale=_candidate_reason(candidate, current_date, "매수")
                        )
                    )
                    break

        return orders

class RandomBotStrategy(BaseStrategy):
    """4. 원숭이 봇 (몬테카를로 무작위 종목 및 매매 전략)"""
    def __init__(self, variant_id: int, seed: int = 42):
        super().__init__(variant_id, "RANDOM_BOT", "원숭이 봇")
        self.seed = seed
        self.rng = random.Random(seed)

    def generate_signals(
        self,
        current_date: str,
        portfolio: Portfolio,
        daily_prices_today: Dict[int, dict],
        securities_map: Dict[int, dict],
        context: Optional[dict] = None
    ) -> List[VirtualOrder]:
        orders = []
        available_secs = [
            sec_id
            for sec_id, price in daily_prices_today.items()
            if securities_map.get(sec_id, {}).get("marketType") in {"KOSPI", "KOSDAQ"}
            and securities_map.get(sec_id, {}).get("isActive", True)
            and float(price.get("tradingValue", 0.0)) >= 1_000_000_000
            and price.get("marketCap") is not None
            and float(price["marketCap"]) >= 50_000_000_000
        ]

        if available_secs and self.rng.random() < 0.3:
            sellable = [sec_id for sec_id in portfolio.positions if sec_id in daily_prices_today]
            if sellable and self.rng.random() < 0.4:
                sec_id = self.rng.choice(sellable)
                pos = portfolio.positions[sec_id]
                close_price = daily_prices_today[sec_id]["closePrice"]
                orders.append(
                    VirtualOrder(
                        order_id=f"random_sell_{sec_id}_{current_date}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side="SELL",
                        quantity=pos.quantity,
                        requested_price=close_price,
                        signal_date=current_date,
                        rationale="원숭이 무작위 선택 매도",
                        reason_codes=["RANDOM_EXIT"],
                    )
                )
            else:
                buyable = [sec_id for sec_id in available_secs if sec_id not in portfolio.positions]
                if not buyable:
                    return orders
                sec_id = self.rng.choice(buyable)
                close_price = daily_prices_today[sec_id]["closePrice"]
                target_weight = self.rng.uniform(0.10, 0.25)
                buy_amount = min(portfolio.cash_balance, portfolio.total_equity * target_weight)
                buy_qty = int(buy_amount / close_price)
                if buy_qty <= 0:
                    return orders
                orders.append(
                    VirtualOrder(
                        order_id=f"random_buy_{sec_id}_{current_date}",
                        simulation_variant_id=self.variant_id,
                        security_id=sec_id,
                        trade_side="BUY",
                        quantity=float(buy_qty),
                        requested_price=close_price,
                        signal_date=current_date,
                        rationale="원숭이 무작위 선택 매수",
                        reason_codes=["RANDOM_ENTRY"],
                        target_weight=target_weight,
                    )
                )
        return orders
