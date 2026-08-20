"""
================================================================================
[Investory Engine Module] rule_schema.py
================================================================================
■ 전체 기능 설명:
  - 글로벌 퀀트 트레이딩 스키마 표준을 준수하는 8대 영역 표준 Rule JSON 데이터 모델 및 유효성 검증기(Validator)를 정의합니다.
================================================================================
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

@dataclass
class UniverseRule:
    """1. 투자 대상 종목군 필터링 규칙"""
    allowed_markets: List[str] = field(default_factory=lambda: ["KOSPI", "KOSDAQ"])
    min_market_cap: float = 50000000000.0
    min_daily_trading_value: float = 1000000000.0
    exclude_halted: bool = True
    exclude_administrative: bool = True

@dataclass
class SelectionRule:
    """2. 종목 평가 및 팩터 가중치 규칙"""
    factor_weights: Dict[str, float] = field(default_factory=lambda: {
        "value": 0.20,
        "growth": 0.30,
        "quality": 0.20,
        "trend": 0.15,
        "disclosure": 0.15
    })
    min_passing_score: float = 70.0

    def validate(self):
        total = sum(self.factor_weights.values())
        if abs(total - 1.0) > 0.01 and total > 0:
            for k in self.factor_weights:
                self.factor_weights[k] /= total

@dataclass
class EntryRule:
    """3. 신규 매수 진입 조건 규칙"""
    max_5day_return: float = 0.15
    moving_average_condition: str = "NONE"
    require_positive_disclosure: bool = False

@dataclass
class AdditionalBuyRule:
    """4. 추가 매수 / 물타기·불타기 규칙"""
    allowed: bool = True
    max_additional_count: int = 2
    trigger_drop_rate: float = -0.05
    additional_weight: float = 0.05

@dataclass
class PortfolioRule:
    """5. 포트폴리오 비중 및 위험 제어 규칙"""
    max_position_count: int = 5
    max_single_position_weight: float = 0.20
    max_sector_weight: float = 0.40

@dataclass
class ExitRule:
    """6. 매도 및 익절/손절 조건 규칙"""
    take_profit_rate: float = 0.20
    stop_loss_rate: float = -0.10
    max_holding_days: int = 90
    sell_on_negative_disclosure: bool = True

@dataclass
class RebalanceRule:
    """7. 포트폴리오 정기 리밸런싱 규칙"""
    period: str = "MONTHLY"
    min_holding_days_before_rebalance: int = 14

@dataclass
class AuditPrincipleItem:
    """8-1. AI 자연어 해석 결과 개별 내역"""
    user_natural_text: str
    # 대표 규칙 경로. 하위 호환을 위해 유지하며 ai_mapped_rules의 첫 항목과 같습니다.
    ai_mapped_rule: str
    status: str = "CONFIRMED"
    # 한 원칙이 여러 조건을 담을 수 있으므로 매핑된 경로 전체를 보관합니다.
    ai_mapped_rules: List[str] = field(default_factory=list)
    # 사용자가 원문에 기준을 직접 밝힌 규칙 경로. ai_mapped_rules 의 부분집합입니다.
    # "손실이 12%에 도달하면 손절" 처럼 문장에 값이 있으면 그 값은 AI 추정이 아니라
    # 사용자가 정한 기준이므로, 확인 없이 평가와 강화에 사용할 수 있습니다.
    stated_rules: List[str] = field(default_factory=list)
    # 실행 규칙으로 만들 수 없을 때 그 사유. 매핑에 성공하면 빈 문자열입니다.
    unmappable_reason: str = ""

@dataclass
class AuditRule:
    """8. AI 파싱 메타데이터 및 사용자 확인 필요 항목"""
    ai_confidence: float = 0.90
    interpreted_principles: List[AuditPrincipleItem] = field(default_factory=list)
    needs_user_confirmation: List[Dict[str, str]] = field(default_factory=list)
    # 서로 충돌하거나 중복되는 원칙 쌍. 규칙 경로만으로는 찾을 수 없습니다.
    principle_conflicts: List[Dict[str, str]] = field(default_factory=list)

@dataclass
class InvestmentBotStrategySchema:
    """전체 8대 영역을 종합 관리하는 메인 투자봇 전략 스키마 클래스"""
    universe: UniverseRule = field(default_factory=UniverseRule)
    selection: SelectionRule = field(default_factory=SelectionRule)
    entry: EntryRule = field(default_factory=EntryRule)
    additional_buy: AdditionalBuyRule = field(default_factory=AdditionalBuyRule)
    portfolio: PortfolioRule = field(default_factory=PortfolioRule)
    exit: ExitRule = field(default_factory=ExitRule)
    rebalance: RebalanceRule = field(default_factory=RebalanceRule)
    audit: AuditRule = field(default_factory=AuditRule)

    def to_dict(self) -> dict:
        self.selection.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InvestmentBotStrategySchema":
        universe = UniverseRule(**data.get("universe", {}))
        
        sel_data = data.get("selection", {})
        selection = SelectionRule(
            factor_weights=sel_data.get("factorWeights", sel_data.get("factor_weights", SelectionRule().factor_weights)),
            min_passing_score=sel_data.get("minPassingScore", sel_data.get("min_passing_score", 70.0))
        )
        
        entry = EntryRule(**{k: v for k, v in data.get("entry", {}).items() if k in EntryRule.__annotations__})
        
        add_data = data.get("additionalBuy", data.get("additional_buy", {}))
        additional_buy = AdditionalBuyRule(**{k: v for k, v in add_data.items() if k in AdditionalBuyRule.__annotations__})
        
        port_data = data.get("portfolio", {})
        portfolio = PortfolioRule(**{k: v for k, v in port_data.items() if k in PortfolioRule.__annotations__})
        
        exit_data = data.get("exit", {})
        exit_rule = ExitRule(**{k: v for k, v in exit_data.items() if k in ExitRule.__annotations__})
        
        reb_data = data.get("rebalance", {})
        rebalance = RebalanceRule(**{k: v for k, v in reb_data.items() if k in RebalanceRule.__annotations__})
        
        aud_data = data.get("audit", {})
        inter_principles = [
            AuditPrincipleItem(**{
                key: value for key, value in item.items()
                if key in AuditPrincipleItem.__annotations__
            })
            for item in aud_data.get("interpretedPrinciples", aud_data.get("interpreted_principles", []))
        ]
        audit = AuditRule(
            ai_confidence=aud_data.get("aiConfidence", aud_data.get("ai_confidence", 0.90)),
            interpreted_principles=inter_principles,
            needs_user_confirmation=aud_data.get("needsUserConfirmation", aud_data.get("needs_user_confirmation", [])),
            principle_conflicts=aud_data.get("principleConflicts", aud_data.get("principle_conflicts", [])),
        )
        
        schema = cls(
            universe=universe,
            selection=selection,
            entry=entry,
            additional_buy=additional_buy,
            portfolio=portfolio,
            exit=exit_rule,
            rebalance=rebalance,
            audit=audit
        )
        schema.selection.validate()
        return schema


def executable_rule_paths() -> List[str]:
    """Every dotted path a principle may be mapped onto.

    ``audit.ai_mapped_rule`` must be one of these exactly. Anything else cannot
    be evaluated, so a principle carrying prose here would be marked CONFIRMED
    and then silently fail every trade check.
    """
    schema = InvestmentBotStrategySchema().to_dict()
    return sorted(
        f"{section}.{field}"
        for section, values in schema.items()
        if section != "audit" and isinstance(values, dict)
        for field in values
    )


def numeric_rule_paths() -> List[str]:
    """수치로 표현되는 규칙 경로.

    사용자가 원문에 숫자를 적었는지 검증할 때 씁니다. 참·거짓이나 열거형 규칙은
    숫자 없이도 문장에서 명시될 수 있으므로("물타기는 하지 않는다") 제외합니다.
    """
    schema = InvestmentBotStrategySchema().to_dict()
    return sorted(
        f"{section}.{field}"
        for section, values in schema.items()
        if section != "audit" and isinstance(values, dict)
        for field, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )
