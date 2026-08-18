"""
================================================================================
[API Endpoint Helpers] simulation_helpers.py
================================================================================
■ 역할:
  - simulation 엔드포인트에서 사용되는 DTO 스키마(RuleCompileRequest, SimulationRunRequest),
    데이터 정규화 유틸리티(normalize_daily_snapshot, normalize_trade),
    인메모리 캐시(SIMULATION_RUN_CACHE)를 분리 관리합니다.
================================================================================
"""

from dataclasses import asdict
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, AliasChoices

# [전역 인메모리 세션 캐시]
SIMULATION_RUN_CACHE: Dict[int, dict] = {}


# [DTO 스키마 정의]
class RuleCompileRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    principles: Optional[List[str]] = None
    profile: Optional[Dict[str, float]] = None
    actual_trades: Optional[List[dict]] = Field(default=None, validation_alias=AliasChoices("actual_trades", "actualTrades"))
    account_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("account_id", "accountId"))

class SimulationRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    simulation_run_id: Optional[int] = Field(default=1, validation_alias=AliasChoices("simulation_run_id", "simulationRunId"))
    period_start: Optional[str] = Field(default="2026-05-12", validation_alias=AliasChoices("period_start", "periodStart"))
    period_end: Optional[str] = Field(default="2026-08-09", validation_alias=AliasChoices("period_end", "periodEnd"))
    # 하위 호환 입력이며 실제 실행 자금은 시작일 직전 보유 스냅샷에서 계산합니다.
    initial_capital: Optional[float] = Field(default=None, validation_alias=AliasChoices("initial_capital", "initialCapital"))
    # None lets the endpoint resolve the currently configured connected account.
    account_id: Optional[int] = Field(default=None, validation_alias=AliasChoices("account_id", "accountId"))
    principles: Optional[List[str]] = None
    profile: Optional[Dict[str, float]] = None
    participant_types: Optional[List[str]] = Field(default=None, validation_alias=AliasChoices("participant_types", "participantTypes"))
    personal_bot_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("personal_bot_id", "personalBotId"))

RuleCompileRequest.model_rebuild()
SimulationRunRequest.model_rebuild()


def normalize_daily_snapshot(snap) -> dict:
    """
    일별 성과 스냅샷 데이터의 필드명을 스네이크케이스/카멜케이스 및 레거시 스펙과의 호환성을 보장하도록 정규화합니다.
    """
    if hasattr(snap, '__dataclass_fields__'):
        snap = asdict(snap)
    elif not isinstance(snap, dict):
        snap = vars(snap) if hasattr(snap, '__dict__') else {}

    vid = snap.get("variantId") or snap.get("simulationVariantId") or snap.get("simulation_variant_id") or 1
    p_date = snap.get("snapshotDate") or snap.get("performanceDate") or snap.get("performance_date") or ""
    cash = snap.get("cashBalance") or snap.get("cash_balance") or snap.get("cash") or 0.0
    holdings = snap.get("holdingsMarketValue") or snap.get("holdings_market_value") or 0.0
    port_val = snap.get("portfolioValue") or snap.get("portfolio_value") or snap.get("totalEquity") or 0.0
    daily_ret = snap.get("dailyReturn") if "dailyReturn" in snap else snap.get("daily_return", 0.0)
    cum_ret = snap.get("cumulativeReturn") if "cumulativeReturn" in snap else snap.get("cumulative_return", 0.0)

    # Internal rate fields are always decimal ratios (0.01 == 1%).  Explicit
    # *Percent fields are already presentation values; otherwise convert here.
    cum_pct = snap.get("cumulativeReturnPercent")
    if cum_pct is None:
        cum_pct = float(cum_ret) * 100 if isinstance(cum_ret, (int, float)) else 0.0

    mdd_rate = snap.get("drawdownRate") if "drawdownRate" in snap else snap.get("drawdown_rate", 0.0)
    mdd_pct = snap.get("mddPercent")
    if mdd_pct is None:
        mdd_pct = float(mdd_rate) * 100 if isinstance(mdd_rate, (int, float)) else 0.0

    return {
        "variantId": vid,
        "simulationVariantId": vid,
        "simulation_variant_id": vid,

        "performanceDate": p_date,
        "performance_date": p_date,
        "snapshotDate": p_date,

        "cash": cash,
        "cashBalance": cash,
        "cash_balance": cash,

        "holdingsMarketValue": holdings,
        "holdings_market_value": holdings,

        "portfolioValue": port_val,
        "portfolio_value": port_val,
        "totalEquity": port_val,

        "dailyReturn": daily_ret,
        "daily_return": daily_ret,

        "cumulativeReturn": cum_ret,
        "cumulative_return": cum_ret,
        "cumulativeReturnPercent": round(float(cum_pct), 2),

        "drawdownRate": mdd_rate,
        "drawdown_rate": mdd_rate,
        "mddPercent": round(float(mdd_pct), 2)
        ,"netCashFlow": float(snap.get("netCashFlow", snap.get("net_cash_flow", 0.0)) or 0.0)
    }

def normalize_trade(t) -> dict:
    """
    가상 체결 거래 데이터의 필드명을 스네이크케이스/카멜케이스 및 프론트/백엔드 데이터 모델에 맞춰 정규화합니다.
    """
    if hasattr(t, '__dataclass_fields__'):
        t = asdict(t)
    elif not isinstance(t, dict):
        t = vars(t) if hasattr(t, '__dict__') else {}

    t_id = t.get("simulatedTradeId") or t.get("simulated_trade_id") or t.get("tradeId") or 1
    vid = t.get("simulationVariantId") or t.get("simulation_variant_id") or t.get("variantId") or 1
    sec_id = t.get("securityId") or t.get("security_id") or 101
    sec_name = t.get("securityName") or t.get("security_name") or f"종목 {sec_id}"
    sec_code = t.get("securityCode") or t.get("security_code") or ""
    side = t.get("tradeSide") or t.get("trade_side") or "BUY"
    traded_at = t.get("tradedAt") or t.get("traded_at") or ""
    qty = t.get("quantity", 0.0)
    unit_p = t.get("unitPrice") or t.get("unit_price") or 0.0
    cost = t.get("transactionCostAmount") or t.get("transaction_cost_amount") or 0.0
    reason = t.get("decisionReason") or t.get("decision_reason") or t.get("rationaleText") or ""
    principle_item_id = t.get("triggeredPrincipleSetItemId") or t.get("triggered_principle_set_item_id")
    execution_policy = t.get("executionPolicy") or t.get("execution_policy") or "NEXT_TRADING_DAY_OPEN"
    original_traded_at = t.get("originalTradedAt") or t.get("original_traded_at")
    applied_trading_date = t.get("appliedTradingDate") or t.get("applied_trading_date")
    rationale_label_type = t.get("rationaleLabelType") or t.get("rationale_label_type") or "UNCLASSIFIED"

    return {
        "simulatedTradeId": t_id,
        "simulated_trade_id": t_id,
        "tradeId": t_id,

        "simulationVariantId": vid,
        "simulation_variant_id": vid,
        "variantId": vid,

        "securityId": sec_id,
        "security_id": sec_id,

        "securityName": sec_name,
        "security_name": sec_name,
        "securityCode": sec_code,
        "security_code": sec_code,

        "tradeSide": side,
        "trade_side": side,

        "tradedAt": traded_at,
        "traded_at": traded_at,

        "quantity": qty,
        "unitPrice": unit_p,
        "unit_price": unit_p,

        "transactionCostAmount": cost,
        "transaction_cost_amount": cost,

        "decisionReason": reason,
        "decision_reason": reason,
        "rationaleText": reason,
        "rationaleLabelType": rationale_label_type,
        "rationale_label_type": rationale_label_type,

        "triggeredPrincipleSetItemId": principle_item_id,
        "triggered_principle_set_item_id": principle_item_id,

        "executionPolicy": execution_policy,
        "execution_policy": execution_policy,
        "originalTradedAt": original_traded_at,
        "original_traded_at": original_traded_at,
        "appliedTradingDate": applied_trading_date,
        "applied_trading_date": applied_trading_date,
    }
