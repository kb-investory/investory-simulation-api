"""Single source of truth for fixed comparison-strategy rules and references."""

VALUE_QUALITY_STRATEGY = {
    "variantId": 3,
    "variantType": "FAMOUS_STRATEGY",
    "variantName": "우량 가치·품질 퀀트 봇",
    "strategyName": "우량 가치·품질 퀀트 전략",
    "universe": {
        "allowed_markets": ["KOSPI", "KOSDAQ"],
        "min_market_cap": 50_000_000_000,
        "min_daily_trading_value": 1_000_000_000,
        "exclude_halted": True,
        "exclude_administrative": True,
    },
    "selection": {
        "factor_weights": {
            "value": 0.40,
            "quality": 0.40,
            "growth": 0.20,
            "trend": 0.0,
            "disclosure": 0.0,
        },
        "min_passing_score": 75.0,
    },
    "entry": {
        "max_5day_return": 0.15,
        "moving_average_condition": "NONE",
        "require_positive_disclosure": False,
    },
    "portfolio": {"target_weight": 0.20},
    "exit": {"take_profit_rate": 0.15},
}


VALUE_QUALITY_REFERENCE_PRINCIPLES = (
    {
        "referenceId": "REF_VALUE_QUALITY_SELECTION",
        "title": "가치와 품질을 함께 확인하기",
        "description": "가치와 품질을 각각 40% 반영하고 종목 평가 점수가 75점 이상일 때만 진입 후보로 검토합니다.",
        "targetRules": ["selection.factor_weights", "selection.min_passing_score"],
        "ruleJson": {"selection": VALUE_QUALITY_STRATEGY["selection"]},
        "applicableTradeSides": ["BUY", "ADD"],
        "priority": 1,
    },
    {
        "referenceId": "REF_LIQUID_UNIVERSE",
        "title": "거래 가능한 규모와 유동성 확인하기",
        "description": "시가총액 500억원 이상, 일 거래대금 10억원 이상인 종목만 검토합니다.",
        "targetRules": ["universe.min_market_cap", "universe.min_daily_trading_value"],
        "ruleJson": {
            "universe": {
                "min_market_cap": VALUE_QUALITY_STRATEGY["universe"]["min_market_cap"],
                "min_daily_trading_value": VALUE_QUALITY_STRATEGY["universe"]["min_daily_trading_value"],
            }
        },
        "applicableTradeSides": ["BUY", "ADD"],
        "priority": 2,
    },
    {
        "referenceId": "REF_TAKE_PROFIT_15",
        "title": "수익 실현 기준을 수치로 정하기",
        "description": "평균 매수가 대비 수익률이 15% 이상이면 보유 수량을 매도합니다.",
        "targetRules": ["exit.take_profit_rate"],
        "ruleJson": {"exit": VALUE_QUALITY_STRATEGY["exit"]},
        "applicableTradeSides": ["SELL", "REDUCE"],
        "priority": 3,
    },
)
