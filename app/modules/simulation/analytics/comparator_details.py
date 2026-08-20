"""Build the user-facing detail contract for simulation comparators."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


RANDOM_MONTE_CARLO_RUN_COUNT = 500
RANDOM_TRACE_SEED = 42

_FACTOR_LABELS = {
    "value": "가치",
    "growth": "성장",
    "quality": "품질",
    "trend": "추세",
    "disclosure": "공시",
}
_PERIOD_LABELS = {
    "DAILY": "매일",
    "WEEKLY": "매주",
    "MONTHLY": "매월",
    "QUARTERLY": "분기마다",
    "YEARLY": "매년",
}


def _datetime_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _first(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _section(schema: dict, snake_name: str, camel_name: str) -> dict:
    value = _first(schema, snake_name, camel_name, default={})
    return value if isinstance(value, dict) else {}


def _enabled(section: dict) -> bool:
    return _first(section, "enabled", "active", "isEnabled", default=True) is not False


def _percent(value: Any, signed: bool = False) -> str:
    number = float(value) * 100
    prefix = "+" if signed and number > 0 else ""
    rendered = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{prefix}{rendered}%"


def _money(value: Any) -> str:
    number = float(value)
    if number >= 100_000_000:
        return f"{number / 100_000_000:g}억원"
    if number >= 10_000:
        return f"{number / 10_000:g}만원"
    return f"{number:g}원"


def _rule(key: str, label: str, value: str, raw_value: Any, unit: Optional[str] = None) -> dict:
    result = {"key": key, "label": label, "value": value, "rawValue": raw_value}
    if unit is not None:
        result["unit"] = unit
    return result


def personal_rules(rule_schema: Optional[dict]) -> List[dict]:
    """Convert persisted executable rules without inventing missing defaults."""
    schema = rule_schema if isinstance(rule_schema, dict) else {}
    rules: List[dict] = []

    universe = _section(schema, "universe", "universe")
    if _enabled(universe):
        markets = _first(universe, "allowed_markets", "allowedMarkets")
        if isinstance(markets, list):
            rules.append(_rule("universe.allowedMarkets", "투자 시장", "·".join(map(str, markets)), markets))
        market_cap = _first(universe, "min_market_cap", "minMarketCap")
        if market_cap is not None:
            rules.append(_rule("universe.minMarketCap", "최소 시가총액", f"{_money(market_cap)} 이상", market_cap, "KRW"))
        trading_value = _first(universe, "min_daily_trading_value", "minDailyTradingValue")
        if trading_value is not None:
            rules.append(_rule("universe.minDailyTradingValue", "최소 일 거래대금", f"{_money(trading_value)} 이상", trading_value, "KRW"))

    selection = _section(schema, "selection", "selection")
    if _enabled(selection):
        score = _first(selection, "min_passing_score", "minPassingScore", "minimumGrowthScore")
        if score is not None:
            rules.append(_rule("selection.minPassingScore", "종목 선택", f"평가 점수 {float(score):g}점 이상", score, "점"))
        weights = _first(selection, "factor_weights", "factorWeights")
        if isinstance(weights, dict):
            ordered = sorted(weights.items(), key=lambda item: (-float(item[1]), str(item[0])))
            text = " · ".join(f"{_FACTOR_LABELS.get(str(key), str(key))} {_percent(value)}" for key, value in ordered)
            raw_weights = [f"{key}={value}" for key, value in ordered]
            rules.append(_rule("selection.factorWeights", "평가 비중", text, raw_weights, "ratio"))

    entry = _section(schema, "entry", "entry")
    if _enabled(entry):
        max_return = _first(entry, "max_5day_return", "max5dayReturn")
        if max_return is not None:
            rules.append(_rule("entry.max5dayReturn", "단기 급등 제한", f"5거래일 수익률 {_percent(max_return)} 이하", max_return, "ratio"))
        disclosure = _first(entry, "require_positive_disclosure", "requirePositiveDisclosure")
        if disclosure is not None:
            rules.append(_rule("entry.requirePositiveDisclosure", "긍정 공시", "필수" if disclosure else "필수 아님", disclosure))

    additional = _section(schema, "additional_buy", "additionalBuy")
    if _enabled(additional) and _first(additional, "allowed", default=True) is not False:
        count = _first(additional, "max_additional_count", "maxAdditionalCount")
        if count is not None:
            rules.append(_rule("additionalBuy.maxAdditionalCount", "추가 매수", f"최대 {int(count)}회", count, "회"))
        trigger = _first(additional, "trigger_drop_rate", "triggerDropRate")
        if trigger is not None:
            rules.append(_rule("additionalBuy.triggerDropRate", "추가 매수 기준", f"수익률 {_percent(trigger)} 이하", trigger, "ratio"))
        weight = _first(additional, "additional_weight", "additionalWeight")
        if weight is not None:
            rules.append(_rule("additionalBuy.additionalWeight", "추가 매수 비중", _percent(weight), weight, "ratio"))

    portfolio = _section(schema, "portfolio", "portfolio")
    if _enabled(portfolio):
        count = _first(portfolio, "max_position_count", "maxPositionCount")
        if count is not None:
            rules.append(_rule("portfolio.maxPositionCount", "최대 보유 종목", f"최대 {int(count)}개", count, "개"))
        weight = _first(portfolio, "max_single_position_weight", "maxSinglePositionWeight", "maxPositionWeight")
        if weight is not None:
            rules.append(_rule("portfolio.maxPositionWeight", "종목 최대 비중", f"최대 {_percent(weight)}", weight, "ratio"))
        sector = _first(portfolio, "max_sector_weight", "maxSectorWeight")
        if sector is not None:
            rules.append(_rule("portfolio.maxSectorWeight", "업종 최대 비중", f"최대 {_percent(sector)}", sector, "ratio"))

    exit_rule = _section(schema, "exit", "exit")
    if _enabled(exit_rule):
        take_profit = _first(exit_rule, "take_profit_rate", "takeProfitRate")
        if take_profit is not None:
            rules.append(_rule("exit.takeProfitRate", "익절 기준", _percent(take_profit, signed=True), take_profit, "ratio"))
        stop_loss = _first(exit_rule, "stop_loss_rate", "stopLossRate")
        if stop_loss is not None:
            rules.append(_rule("exit.stopLossRate", "손절 기준", _percent(stop_loss), stop_loss, "ratio"))
        days = _first(exit_rule, "max_holding_days", "maxHoldingDays")
        if days is not None:
            rules.append(_rule("exit.maxHoldingDays", "최대 보유 기간", f"최대 {int(days)}일", days, "일"))

    rebalance = _section(schema, "rebalance", "rebalance")
    if _enabled(rebalance):
        period = _first(rebalance, "period")
        if period is not None:
            rules.append(_rule("rebalance.period", "리밸런싱 주기", _PERIOD_LABELS.get(str(period), str(period)), period))
        days = _first(rebalance, "min_holding_days_before_rebalance", "minHoldingDaysBeforeRebalance")
        if days is not None:
            rules.append(_rule("rebalance.minHoldingDays", "리밸런싱 전 최소 보유", f"{int(days)}일", days, "일"))
    return rules


def _confidence_percent(bot: dict) -> Optional[int]:
    metadata = bot.get("ruleCompilation") if isinstance(bot.get("ruleCompilation"), dict) else {}
    schema = bot.get("ruleSchema") if isinstance(bot.get("ruleSchema"), dict) else {}
    audit = _section(schema, "audit", "audit")
    value = _first(metadata, "confidencePercent", "aiConfidence", "confidence")
    if value is None:
        value = _first(audit, "ai_confidence", "aiConfidence")
    if value is None:
        return None
    number = float(value)
    return round(number * 100 if 0 <= number <= 1 else number)


def _personal_traits(rule_schema: dict) -> List[str]:
    selection = _section(rule_schema, "selection", "selection")
    weights = _first(selection, "factor_weights", "factorWeights", default={})
    if not isinstance(weights, dict):
        return []
    ordered = sorted(weights.items(), key=lambda item: (-float(item[1]), str(item[0])))
    return [_FACTOR_LABELS.get(str(key), str(key)) for key, _ in ordered[:3]]


def _principles(items: Optional[Iterable[dict]]) -> List[dict]:
    normalized = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = item.get("principleText") or item.get("text")
        if not text:
            continue
        normalized.append({
            "principleId": item.get("principleSetItemId", item.get("principleId")),
            "text": str(text),
            "source": "USER_CONFIRMED",
            "_order": (item.get("sortOrder", 0), str(item.get("principleSetItemId", ""))),
        })
    normalized.sort(key=lambda item: item.pop("_order"))
    return normalized


def build_personal_comparator(bot: dict, principle_items: Optional[Iterable[dict]], evidence: Optional[dict]) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    schema = bot.get("ruleSchema") if isinstance(bot.get("ruleSchema"), dict) else {}
    version = bot.get("botVersion")
    version_text = str(version) if str(version).startswith("v") else f"v{version}.0"
    confidence = _confidence_percent(bot)
    updated_at = _datetime_text(bot.get("createdAt") or evidence.get("updatedAt") or (bot.get("ruleCompilation") or {}).get("compiledAt"))
    date_text = updated_at[:10].replace("-", ".") if updated_at else None
    version_parts = [part for part in (f"{date_text} 업데이트" if date_text else None, f"신뢰도 {confidence}%" if confidence is not None else None) if part]
    traits = _personal_traits(schema)
    summary = " · ".join(traits)
    summary = f"{summary} 기준을 중심으로 저장된 투자 원칙과 운용 규칙을 적용합니다." if summary else "저장된 투자 원칙과 운용 규칙을 적용합니다."
    trade_count = int(evidence.get("tradeCount") or 0)
    journal_count = int(evidence.get("journalCount") or 0)
    confirmed_count = int(evidence.get("confirmedPrincipleCount") or 0)
    return {
        "variantId": 2,
        "variantType": "PERSONAL_BOT",
        "variantName": f"나의 투자봇 {version_text}",
        "description": "사용자의 확정 원칙과 투자 성향을 바탕으로 생성된 개인 투자봇입니다.",
        "fixed": True,
        "selectable": False,
        "className": "PERSONAL",
        "level": f"LV.{version}" if isinstance(version, int) else version_text.upper(),
        "traits": traits,
        "strategyLabel": "PERSONAL STRATEGY",
        "versionLine": " · ".join(version_parts) or version_text,
        "summary": summary,
        "principles": _principles(principle_items),
        "rules": personal_rules(schema),
        "dataEvidence": {
            "title": "나의 투자 데이터로 만들어졌어요",
            "summary": f"거래 {trade_count}건 · 투자 일지 {journal_count}건 · 확정 원칙 {confirmed_count}개",
            "source": "COMPILED_BOT",
            "updatedAt": updated_at,
            "tradeCount": trade_count,
            "journalCount": journal_count,
            "confirmedPrincipleCount": confirmed_count,
        },
        "personalBotId": bot.get("personalBotId"),
        "botVersion": version_text,
        "confidencePercent": confidence,
        "profileSource": {
            "source": "MYSQL",
            "analysisRunId": bot.get("analysisRunId"),
            "analysisVersion": bot.get("analysisVersion"),
        },
    }


def build_comparators(bot: dict, principle_items: Optional[Iterable[dict]], evidence: Optional[dict]) -> List[dict]:
    evidence = evidence if isinstance(evidence, dict) else {}
    trade_count = int(evidence.get("tradeCount") or 0)
    journal_count = int(evidence.get("journalCount") or 0)
    actual_updated = _datetime_text(evidence.get("actualUpdatedAt"))
    security_count = int(evidence.get("analyzedSecurityCount") or 0)
    system_updated = _datetime_text(evidence.get("systemUpdatedAt"))
    return [
        {
            "variantId": 1, "variantType": "ACTUAL_USER", "variantName": "실제 나",
            "description": "사용자의 과거 실제 계좌 매수·매도 거래를 재현합니다.",
            "fixed": True, "selectable": False, "className": "PLAYER 01", "level": "",
            "traits": ["실제 거래", "계좌 기록"], "strategyLabel": "ACTUAL INVESTOR",
            "versionLine": "실제 계좌 거래 기준",
            "summary": "사용자가 실제로 실행한 매수·매도와 보유 내역을 같은 시장 조건에서 재현합니다.",
            "principles": [],
            "rules": [_rule("execution", "거래 방식", "실제 체결 내역 재현", "DATABASE_ACTUAL_FILL")],
            "dataEvidence": {"title": "실제 투자 기록을 사용해요", "summary": f"실제 거래 {trade_count}건 · 투자 일지 {journal_count}건", "source": "MYSQL", "updatedAt": actual_updated, "tradeCount": trade_count, "journalCount": journal_count},
            "personalBotId": None, "botVersion": None, "confidencePercent": None, "profileSource": None,
        },
        build_personal_comparator(bot, principle_items, evidence),
        {
            "variantId": 3, "variantType": "FAMOUS_STRATEGY", "variantName": "우량 가치·품질 퀀트 봇",
            "description": "재무·가격 데이터를 근거로 가치와 품질을 평가하는 비교 전략봇입니다.",
            "fixed": False, "selectable": True, "className": "LEGEND", "level": "VALUE · QUALITY",
            "traits": ["가치", "품질", "장기"], "strategyLabel": "LEGEND STRATEGY",
            "versionLine": "가치·품질 전략 · 시장 데이터 기반",
            "summary": "가치와 품질 팩터를 각각 40% 반영하고 평가 점수 75점 이상인 종목을 선택합니다.",
            "principles": [
                {"principleId": None, "text": "시가총액 500억원, 일 거래대금 10억원 이상인 KOSPI·KOSDAQ 종목을 대상으로 한다.", "source": "SYSTEM_STRATEGY"},
                {"principleId": None, "text": "가치와 품질을 우선 평가하고 종목당 최대 20%를 매수한다.", "source": "SYSTEM_STRATEGY"},
                {"principleId": None, "text": "보유 수익률이 15% 이상이면 매도한다.", "source": "SYSTEM_STRATEGY"},
            ],
            "rules": [
                _rule("universe.allowedMarkets", "투자 시장", "KOSPI·KOSDAQ", ["KOSPI", "KOSDAQ"]),
                _rule("universe.minMarketCap", "최소 시가총액", "500억원 이상", 50_000_000_000, "KRW"),
                _rule("universe.minDailyTradingValue", "최소 일 거래대금", "10억원 이상", 1_000_000_000, "KRW"),
                _rule("selection.factorWeights", "평가 비중", "가치 40% · 품질 40% · 성장 20%", ["value=0.4", "quality=0.4", "growth=0.2"], "ratio"),
                _rule("selection.minPassingScore", "최소 평가 점수", "75점 이상", 75, "점"),
                _rule("entry.max5dayReturn", "단기 급등 제한", "5거래일 수익률 15% 이하", 0.15, "ratio"),
                _rule("portfolio.targetWeight", "종목 목표 비중", "최대 20%", 0.2, "ratio"),
                _rule("exit.takeProfitRate", "매도 기준", "+15%", 0.15, "ratio"),
            ],
            "dataEvidence": {"title": "시장·재무 데이터를 사용해요", "summary": f"분석 가능 종목 {security_count}개 · 가치·품질 팩터 적용", "source": "SYSTEM_CONFIG", "updatedAt": system_updated, "analyzedSecurityCount": security_count, "strategyCount": 1},
            "personalBotId": None, "botVersion": "v1.0", "confidencePercent": None, "profileSource": None,
        },
        {
            "variantId": 4, "variantType": "RANDOM_BOT", "variantName": "원숭이 봇",
            "description": "무작위 종목과 매매 시점으로 전략 성과의 우연성을 비교합니다.",
            "fixed": False, "selectable": True, "className": "WILD CARD", "level": "RANDOM",
            "traits": ["랜덤", "대조군"], "strategyLabel": "RANDOM STRATEGY",
            "versionLine": f"몬테카를로 {RANDOM_MONTE_CARLO_RUN_COUNT}회 · 고정 시드 추적",
            "summary": "동일한 투자 가능 종목군에서 무작위 선택 결과와 다른 전략의 성과를 비교합니다.",
            "principles": [
                {"principleId": None, "text": "투자 가능 종목 안에서 매수·매도 대상을 무작위로 선택한다.", "source": "RANDOM_POLICY"},
                {"principleId": None, "text": "다른 참가자와 동일한 기간과 초기자금을 사용한다.", "source": "RANDOM_POLICY"},
            ],
            "rules": [
                _rule("runCount", "반복 실행", f"{RANDOM_MONTE_CARLO_RUN_COUNT}회", RANDOM_MONTE_CARLO_RUN_COUNT, "회"),
                _rule("traceSeed", "화면 재현 시드", str(RANDOM_TRACE_SEED), RANDOM_TRACE_SEED),
                _rule("universe", "투자 대상", "KOSPI·KOSDAQ 투자 가능 종목", ["KOSPI", "KOSDAQ"]),
                _rule("signal.attemptProbability", "일별 매매 시도 확률", "30%", 0.3, "ratio"),
                _rule("signal.sellProbability", "보유 시 매도 선택 확률", "40%", 0.4, "ratio"),
                _rule("portfolio.targetWeightRange", "무작위 목표 비중", "10~25%", ["0.1", "0.25"], "ratio"),
            ],
            "dataEvidence": {"title": "무작위 비교 실험 데이터예요", "summary": f"몬테카를로 {RANDOM_MONTE_CARLO_RUN_COUNT}회 · 동일 기간·동일 초기자금", "source": "SYSTEM_CONFIG", "updatedAt": None, "monteCarloRunCount": RANDOM_MONTE_CARLO_RUN_COUNT},
            "personalBotId": None, "botVersion": "v1.0", "confidencePercent": None, "profileSource": None,
        },
    ]
