"""Single source of truth for the fixed comparison strategy and its references.

The rules below are adapted from criteria Warren Buffett has stated himself, so
each one carries the source it came from. Two limits are worth stating plainly,
because the screen must not overclaim:

* Berkshire's published criteria are for buying whole businesses, not for
  screening listed Korean equities. "Management in place", "a simple business"
  and "an offering price" have no counterpart in this rule schema and are left
  out rather than approximated by something numeric.
* The thresholds here are this service's reading of those criteria against the
  eight rule areas. Buffett publishes no stock screen, and a quarter of
  simulated returns says nothing about whether the approach is sound.

Sources
  Acquisition criteria, incl. "good returns on equity while employing little or
    no debt": https://www.berkshirehathaway.com/1999ar/acq.html
  1987 letter, ROE bar: average above 20% over ten years, never below 15%
  1988 letter, "our favorite holding period is forever" and "we continue to
    concentrate our investments in a very few companies"
    https://www.berkshirehathaway.com/letters/1988.html
"""

BUFFETT_STRATEGY = {
    "variantId": 3,
    "variantType": "FAMOUS_STRATEGY",
    "variantName": "버핏식 우량기업 장기보유 봇",
    "strategyName": "버핏식 우량기업 장기보유 전략",
    "sourceLabel": "버크셔 해서웨이 주주서한에 공개된 기준을 이 서비스의 실행 규칙으로 옮긴 것",
    "universe": {
        "allowed_markets": ["KOSPI", "KOSDAQ"],
        # "Large purchases". Berkshire's floor is stated in pre-tax earnings, not
        # market cap; this is the nearest available screen in this schema.
        "min_market_cap": 1_000_000_000_000,
        "min_daily_trading_value": 5_000_000_000,
        "exclude_halted": True,
        "exclude_administrative": True,
    },
    "selection": {
        # Quality carries the most weight because the stated criteria are about
        # the business: returns on equity earned with little or no debt. Value
        # follows, since a price still has to make sense. Growth stands in for
        # "demonstrated consistent earning power" -- a record, not a forecast.
        # Trend and disclosure are zero: "future projections are of no interest."
        "factor_weights": {
            "value": 0.30,
            "quality": 0.50,
            "growth": 0.20,
            "trend": 0.0,
            "disclosure": 0.0,
        },
        "min_passing_score": 80.0,
    },
    "entry": {
        # Nothing in the criteria endorses buying into a run-up, and the
        # published stance is the opposite of chasing.
        "max_5day_return": 0.05,
        "moving_average_condition": "NONE",
        "require_positive_disclosure": False,
    },
    "portfolio": {
        # "We continue to concentrate our investments in a very few companies."
        "max_position_count": 5,
        "max_single_position_weight": 0.35,
        "max_sector_weight": 0.60,
        "target_weight": 0.25,
    },
    "additional_buy": {
        # A falling price on an intact business is a lower entry, not a signal
        # to leave. The cap keeps that from becoming unlimited averaging down.
        "allowed": True,
        "max_additional_count": 2,
        "trigger_drop_rate": -0.20,
        "additional_weight": 0.05,
    },
    "exit": {
        # "Our favorite holding period is forever." No percentage take-profit
        # and no price-triggered stop appear anywhere in the stated criteria, so
        # both sit far outside the range a normal trade would reach instead of
        # being invented at a plausible-looking number.
        "take_profit_rate": 1.00,
        "stop_loss_rate": -0.50,
        "max_holding_days": 3650,
        "sell_on_negative_disclosure": False,
    },
}

BUFFETT_REFERENCE_PRINCIPLES = (
    {
        "referenceId": "REF_QUALITY_OVER_PRICE",
        "title": "사업의 질을 값보다 먼저 본다",
        "description": (
            "자기자본이익률이 높으면서 빚을 거의 쓰지 않는 기업만 후보로 봅니다. "
            "종목 평가에서 품질 50%, 가치 30%, 실적 지속성 20%를 반영하고 "
            "80점 이상일 때만 검토합니다."
        ),
        "sourceQuote": "businesses earning good returns on equity while employing little or no debt",
        "sourceLabel": "버크셔 해서웨이 인수 기준",
        "sourceUrl": "https://www.berkshirehathaway.com/1999ar/acq.html",
        "targetRules": ["selection.factor_weights", "selection.min_passing_score"],
        "ruleJson": {"selection": BUFFETT_STRATEGY["selection"]},
        "applicableTradeSides": ["BUY", "ADD"],
        "priority": 1,
    },
    {
        "referenceId": "REF_FOREVER_HOLDING",
        "title": "좋은 기업은 오래 들고 간다",
        "description": (
            "수익률이 얼마가 되었다고 팔지 않습니다. 파는 이유는 사업이 달라졌을 때뿐입니다. "
            "최대 보유 기간을 10년으로 두고, 목표 수익률이나 손절선으로 자동 매도하지 않습니다."
        ),
        "sourceQuote": "our favorite holding period is forever",
        "sourceLabel": "1988년 주주서한",
        "sourceUrl": "https://www.berkshirehathaway.com/letters/1988.html",
        "targetRules": ["exit.max_holding_days", "exit.take_profit_rate"],
        "ruleJson": {
            "exit": {
                "max_holding_days": BUFFETT_STRATEGY["exit"]["max_holding_days"],
                "take_profit_rate": BUFFETT_STRATEGY["exit"]["take_profit_rate"],
            }
        },
        "applicableTradeSides": ["SELL", "REDUCE"],
        "priority": 2,
    },
    {
        "referenceId": "REF_CONCENTRATION",
        "title": "확신이 있는 소수에 집중한다",
        "description": (
            "잘 아는 소수 기업에만 의미 있는 비중을 싣습니다. "
            "보유 종목은 5개 이내로 두고, 한 종목에 최대 35%까지 허용합니다."
        ),
        "sourceQuote": (
            "we continue to concentrate our investments in a very few companies "
            "that we try to understand well"
        ),
        "sourceLabel": "1988년 주주서한",
        "sourceUrl": "https://www.berkshirehathaway.com/letters/1988.html",
        "targetRules": ["portfolio.max_position_count", "portfolio.max_single_position_weight"],
        "ruleJson": {
            "portfolio": {
                "max_position_count": BUFFETT_STRATEGY["portfolio"]["max_position_count"],
                "max_single_position_weight": BUFFETT_STRATEGY["portfolio"]["max_single_position_weight"],
            }
        },
        "applicableTradeSides": ["BUY", "ADD"],
        "priority": 3,
    },
    {
        "referenceId": "REF_LARGE_ESTABLISHED",
        "title": "규모가 검증된 기업만 본다",
        "description": (
            "실적이 이미 증명된 큰 기업만 검토합니다. 시가총액 1조원 이상, "
            "일 거래대금 50억원 이상인 종목으로 후보를 좁힙니다."
        ),
        "sourceQuote": "large purchases … demonstrated consistent earning power",
        "sourceLabel": "버크셔 해서웨이 인수 기준",
        "sourceUrl": "https://www.berkshirehathaway.com/1999ar/acq.html",
        "targetRules": ["universe.min_market_cap", "universe.min_daily_trading_value"],
        "ruleJson": {
            "universe": {
                "min_market_cap": BUFFETT_STRATEGY["universe"]["min_market_cap"],
                "min_daily_trading_value": BUFFETT_STRATEGY["universe"]["min_daily_trading_value"],
            }
        },
        "applicableTradeSides": ["BUY", "ADD"],
        "priority": 4,
    },
)

# 이전 이름으로 참조하는 코드가 남아 있어 별칭을 유지합니다.
VALUE_QUALITY_STRATEGY = BUFFETT_STRATEGY
VALUE_QUALITY_REFERENCE_PRINCIPLES = BUFFETT_REFERENCE_PRINCIPLES
