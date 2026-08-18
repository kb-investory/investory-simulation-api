"""Deterministic post-simulation judgments used by the user-facing report."""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from typing import Dict, List, Optional


ACTUAL_VARIANT_IDS = {1, 1001}
DEFAULT_RATIONALE_MARKERS = (
    "과거 실제 매매 내역 재현",
    "database actual fill",
)

PATTERN_CONFIG = {
    "FOMO_BUY": {
        "tag": "FOMO_BUY",
        "label": "추격 매수",
        "principleType": "ENTRY_DISCIPLINE",
        "title": "급등 후 추격매수 제한",
        "description": "최근 5거래일 동안 10% 이상 상승한 종목은 신규 매수를 보류합니다.",
        "ruleJson": {"entry": {"max_5day_return": 0.10}},
        "targetRule": "entry.max_5day_return",
        "proposedValue": 0.10,
        "allowedMinimum": 0.05,
        "allowedMaximum": 0.20,
        "strengthDirection": "DECREASE",
        "actionCategory": "ENTRY_DISCIPLINE",
        "actionTitle": "추격매수 사전 확인",
        "action": "주문 전에 최근 5거래일 수익률이 10% 미만인지 확인합니다.",
    },
    "DELAYED_STOP_LOSS": {
        "tag": "DELAYED_STOP_LOSS",
        "label": "손절 지연",
        "principleType": "LOSS_CONTROL",
        "title": "손실 제한 기준 준수",
        "description": "평균 매수가 대비 10% 하락하면 추가 판단 없이 손실 제한 규칙을 적용합니다.",
        "ruleJson": {"exit": {"stop_loss_rate": -0.10}},
        "targetRule": "exit.stop_loss_rate",
        "proposedValue": -0.10,
        "allowedMinimum": -0.20,
        "allowedMaximum": -0.03,
        "strengthDirection": "INCREASE",
        "actionCategory": "LOSS_CONTROL",
        "actionTitle": "손실 제한 알림",
        "action": "평균 매수가 대비 손실률이 10%에 도달하면 즉시 재검토합니다.",
    },
    "CONCENTRATED_BUY": {
        "tag": "CONCENTRATED_BUY",
        "label": "집중 매수",
        "principleType": "POSITION_SIZING",
        "title": "종목당 비중 상한 설정",
        "description": "한 종목의 매수 비중을 전체 자산의 20% 이내로 제한합니다.",
        "ruleJson": {"portfolio": {"max_single_position_weight": 0.20}},
        "targetRule": "portfolio.max_single_position_weight",
        "proposedValue": 0.20,
        "allowedMinimum": 0.05,
        "allowedMaximum": 0.40,
        "strengthDirection": "DECREASE",
        "actionCategory": "POSITION_SIZING",
        "actionTitle": "주문 전 비중 확인",
        "action": "매수 후 예상 종목 비중이 전체 자산의 20%를 넘는지 확인합니다.",
    },
}

RATIONALE_TYPE_MAP = {
    "FUNDAMENTAL_ANALYSIS": ("FUNDAMENTAL", 80),
    "PRICE_TREND": ("TECHNICAL", 65),
    "EVENT_REACTION": ("EVENT", 60),
    "INTUITION_SOCIAL_SIGNAL": ("INTUITION_SOCIAL", 35),
}


def _variant_id(trade: dict) -> int:
    return int(
        trade.get("variantId")
        or trade.get("simulationVariantId")
        or trade.get("simulation_variant_id")
        or 0
    )


def _date_distance(left: str, right: str) -> int:
    """Return a safe calendar-day distance for persisted/applied trade dates."""
    try:
        return abs((date_type.fromisoformat(left[:10]) - date_type.fromisoformat(right[:10])).days)
    except (TypeError, ValueError):
        return 999_999


def _future_price_outcome(
    trade: dict,
    prices_by_security: Dict[int, List[tuple[str, float]]],
    trading_days: int,
) -> dict:
    security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
    series = prices_by_security.get(security_id, [])
    start_index = next((index for index, item in enumerate(series) if item[0] >= _trade_date(trade)), None)
    if start_index is None or start_index + trading_days >= len(series) or series[start_index][1] <= 0:
        return {
            "measurementTradingDays": trading_days,
            "baseDate": series[start_index][0] if start_index is not None else None,
            "evaluationDate": None,
            "basePrice": series[start_index][1] if start_index is not None else None,
            "evaluationPrice": None,
            "returnPercent": None,
        }
    base_date, base_price = series[start_index]
    evaluation_date, evaluation_price = series[start_index + trading_days]
    return {
        "measurementTradingDays": trading_days,
        "baseDate": base_date,
        "evaluationDate": evaluation_date,
        "basePrice": base_price,
        "evaluationPrice": evaluation_price,
        "returnPercent": round((evaluation_price - base_price) / base_price * 100, 2),
    }


def _directional_outcome(action: str, return_percent: Optional[float]) -> str:
    if return_percent is None:
        return "INSUFFICIENT_DATA"
    if return_percent == 0:
        return "NEUTRAL"
    favorable = (
        action in {"BUY", "ADD"} and return_percent > 0
    ) or (
        action in {"SELL", "REDUCE"} and return_percent < 0
    )
    return "FAVORABLE" if favorable else "UNFAVORABLE"


def _review_case(principle_judgment: str, market_outcome: str) -> str:
    cases = {
        ("FOLLOWED", "FAVORABLE"): "GOOD_PROCESS_GOOD_OUTCOME",
        ("FOLLOWED", "UNFAVORABLE"): "GOOD_PROCESS_BAD_OUTCOME",
        ("VIOLATED", "FAVORABLE"): "BAD_PROCESS_LUCKY_OUTCOME",
        ("VIOLATED", "UNFAVORABLE"): "BAD_PROCESS_BAD_OUTCOME",
    }
    return cases.get((principle_judgment, market_outcome), "UNASSESSED")


def _trade_date(trade: dict) -> str:
    return str(
        trade.get("appliedTradingDate")
        or trade.get("applied_trading_date")
        or trade.get("tradedAt")
        or trade.get("traded_at")
        or ""
    )[:10]


def _trade_id(trade: dict):
    return trade.get("tradeId") or trade.get("simulatedTradeId") or trade.get("simulated_trade_id")


def _participant_return(participants: List[dict], variant_type: str, variant_id: int) -> float:
    summary = next(
        (
            item for item in participants
            if item.get("variantType") == variant_type
            or int(item.get("variantId") or item.get("simulationVariantId") or 0) == variant_id
        ),
        {},
    )
    value = summary.get("cumulativeReturnPercent")
    if value is None:
        value = float(summary.get("cumulative_return", 0.0) or 0.0) * 100
    return round(float(value or 0.0), 2)


def _learning_narrative(actual_return: float, principle_return: float, primary_text: str) -> str:
    difference = round(principle_return - actual_return, 2)
    if difference > 0:
        comparison = f"원칙봇 수익률이 실제 투자보다 {difference:.2f}%p 높았습니다."
    elif difference < 0:
        comparison = f"실제 투자 수익률이 원칙봇보다 {abs(difference):.2f}%p 높았습니다."
    else:
        comparison = "실제 투자와 원칙봇의 수익률이 같았습니다."
    return (
        f"실제 투자 수익률은 {actual_return:.2f}%, "
        f"원칙봇 수익률은 {principle_return:.2f}%로 {comparison} "
        f"{primary_text}"
    )


def _clean_rationale(trade: dict) -> str:
    value = str(
        trade.get("rationaleText")
        or trade.get("decisionReason")
        or trade.get("decision_reason")
        or ""
    ).strip()
    lowered = value.lower()
    if not value or any(marker in lowered for marker in DEFAULT_RATIONALE_MARKERS):
        return ""
    return value


def _classify_basis(rationale: str) -> tuple[str, int]:
    if not rationale:
        return "UNKNOWN", 10
    lowered = rationale.lower()
    categories = (
        ("FUNDAMENTAL", ("실적", "매출", "영업이익", "per", "pbr", "roe", "현금흐름", "부채", "재무"), 80),
        ("TECHNICAL", ("이평", "이동평균", "차트", "거래량", "지지", "저항", "추세"), 65),
        ("NEWS", ("뉴스", "공시", "기사", "수주", "계약", "루머"), 60),
        ("EMOTION", ("불안", "두려", "공포", "급해서", "조급", "느낌", "감정"), 35),
    )
    for basis_type, keywords, score in categories:
        if any(keyword in lowered for keyword in keywords):
            return basis_type, min(100, score + (5 if any(char.isdigit() for char in rationale) else 0))
    return "OTHER", 45


def _basis_classification(trade: dict, rationale: str) -> tuple[str, int, str, str]:
    database_type = str(
        trade.get("rationaleLabelType")
        or trade.get("rationale_label_type")
        or "UNCLASSIFIED"
    )
    mapped = RATIONALE_TYPE_MAP.get(database_type)
    if mapped:
        return mapped[0], mapped[1], "DATABASE", database_type
    basis_type, score = _classify_basis(rationale)
    source = "DETERMINISTIC_KEYWORD_FALLBACK" if rationale else "NOT_CLASSIFIED"
    return basis_type, score, source, database_type


def _verifiability(rationale: str, basis_type: str) -> str:
    if not rationale:
        return "UNVERIFIABLE"
    if basis_type in {"INTUITION_SOCIAL", "EMOTION"}:
        return "AMBIGUOUS"
    if basis_type in {"FUNDAMENTAL", "TECHNICAL", "EVENT", "NEWS"}:
        return "VERIFIABLE"
    return "AMBIGUOUS"


def _confidence_label(score: int) -> str:
    if score <= 30:
        return "매우 부족"
    if score <= 50:
        return "근거 부족"
    if score <= 70:
        return "보통"
    return "이성적 근거"


def _outcome_text(action: str, subsequent_return: Optional[float]) -> str:
    if subsequent_return is None:
        return "후속 5거래일 데이터 부족"
    if action in {"BUY", "ADD"}:
        return f"매수 후 5거래일 수익률 {subsequent_return:+.2f}%"
    if action in {"SELL", "REDUCE"}:
        if subsequent_return > 0:
            return f"매도 후 5거래일 동안 {subsequent_return:+.2f}% 상승"
        return f"매도 후 5거래일 동안 {subsequent_return:+.2f}% 변동"
    return f"5거래일 후 수익률 {subsequent_return:+.2f}%"


def _nested_value(data: dict, dotted_path: str):
    current = data
    for key in dotted_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _rule_json(dotted_path: str, value) -> dict:
    section, field = dotted_path.split(".", 1)
    return {section: {field: value}}


def _explicit_principle_match(rule_schema: dict, dotted_path: str) -> Optional[dict]:
    audit = rule_schema.get("audit") if isinstance(rule_schema, dict) else {}
    interpreted = audit.get("interpreted_principles", []) if isinstance(audit, dict) else []
    leaf = dotted_path.split(".")[-1].lower()
    for item in interpreted:
        if not isinstance(item, dict):
            continue
        mapped = str(
            item.get("ai_mapped_rule")
            or item.get("mappedRule")
            or item.get("field")
            or ""
        ).lower()
        if dotted_path.lower() in mapped or leaf in mapped:
            return item
    return None


def _applicable_explicit_principle(
    rule_schema: dict,
    action: str,
    required_sections: Optional[tuple[str, ...]] = None,
) -> Optional[dict]:
    audit = rule_schema.get("audit") if isinstance(rule_schema, dict) else {}
    interpreted = audit.get("interpreted_principles", []) if isinstance(audit, dict) else []
    sections = required_sections or (
        ("entry.", "universe.", "selection.", "portfolio.", "additional_buy.")
        if action in {"BUY", "ADD"}
        else ("exit.", "rebalance.")
    )
    for item in interpreted:
        if not isinstance(item, dict) or str(item.get("status") or "CONFIRMED") != "CONFIRMED":
            continue
        mapped = str(item.get("ai_mapped_rule") or item.get("mappedRule") or "").lower()
        if mapped.startswith(sections):
            return item
    return None


def _principle_payload(
    explicit: Optional[dict],
    config: Optional[dict],
    bot_action: str,
) -> Optional[dict]:
    if explicit:
        mapped_rule = str(
            explicit.get("ai_mapped_rule")
            or explicit.get("mappedRule")
            or explicit.get("field")
            or (config or {}).get("targetRule")
            or ""
        )
        original_text = str(
            explicit.get("user_natural_text")
            or explicit.get("userNaturalText")
            or (config or {}).get("description")
            or ""
        )
        return {
            "principleSetItemId": explicit.get("principle_set_item_id") or explicit.get("principleSetItemId"),
            "title": (config or {}).get("title") or original_text or "사용자 원칙",
            "originalText": original_text,
            "source": "USER_PRINCIPLE",
            "targetRule": mapped_rule or None,
            "expectedAction": bot_action,
        }
    if config:
        return {
            "principleSetItemId": None,
            "title": config["title"],
            "originalText": config["description"],
            "source": "SUGGESTED_PATTERN",
            "targetRule": config["targetRule"],
            "expectedAction": bot_action,
        }
    return None


def _principle_feedback(judgment: str, principle: Optional[dict], bot_action: str) -> str:
    title = (principle or {}).get("title") or "개인 원칙봇의 판단"
    if judgment == "FOLLOWED":
        return f"'{title}' 원칙에 맞게 행동했어요. 정한 원칙을 제대로 지켰습니다."
    if judgment == "VIOLATED":
        return f"'{title}' 원칙대로라면 이 시점에는 {bot_action} 판단을 먼저 따랐어야 해요."
    if judgment == "DECISION_DIFFERENCE":
        return "명시적인 사용자 원칙 위반은 확인되지 않았지만 개인 원칙봇과 다른 판단을 했어요."
    if judgment == "INSUFFICIENT_DATA":
        return "원칙 준수 여부를 판단할 데이터가 부족합니다."
    return "이 거래에 직접 연결할 수 있는 명시적인 사용자 원칙이 없습니다."


def _fallback_proposed_value(config: dict, current_value):
    proposed = float(config["proposedValue"])
    if not isinstance(current_value, (int, float)):
        return proposed
    current = float(current_value)
    if config["strengthDirection"] == "DECREASE":
        return min(current, proposed)
    return max(current, proposed)


def _trade_snapshot(trade: dict) -> dict:
    """Return the factual execution details needed to replay one decision."""
    quantity = float(trade.get("quantity") or 0.0)
    unit_price = float(trade.get("unitPrice") or trade.get("unit_price") or 0.0)
    return {
        "quantity": quantity,
        "unitPrice": unit_price,
        "notionalAmount": round(quantity * unit_price, 2),
        "transactionCostAmount": round(
            float(trade.get("transactionCostAmount") or trade.get("transaction_cost_amount") or 0.0),
            2,
        ),
    }


def _recommended_action(actual_action: str, bot_action: str, config: Optional[dict]) -> str:
    if config:
        return config["action"]
    if bot_action == "HOLD":
        return "\uc6d0\uce59\ubd07\uc740 \ud574\ub2f9 \uc2dc\uc810\uc5d0 \ub9e4\ub9e4\ud558\uc9c0 \uc54a\uace0 \uad00\ub9dd\ud588\uc2b5\ub2c8\ub2e4."
    if bot_action in {"BUY", "ADD", "SELL", "REDUCE"}:
        return f"\uc6d0\uce59\ubd07\uc758 \ud310\ub2e8({bot_action})\uc744 \uba3c\uc800 \uac80\ud1a0\ud558\uc138\uc694."
    return "\uc801\uc6a9\ud560 \uc6d0\uce59 \ud310\ub2e8 \uc815\ubcf4\uac00 \ubd80\uc871\ud569\ub2c8\ub2e4."


def _principle_review(
    action: str,
    bot_action: str,
    matched_code: Optional[str],
    evidence_codes: List[str],
) -> dict:
    """Describe the rule gap without calling an unmatched decision a violation."""
    config = PATTERN_CONFIG.get(matched_code or "")
    if config:
        return {
            "status": "VIOLATION_PATTERN_DETECTED",
            "violatedPrinciple": config["title"],
            "violationReason": config["description"],
            "recommendedAction": _recommended_action(action, bot_action, config),
            "targetRule": config["targetRule"],
        }
    return {
        "status": "DECISION_DIFFERENCE",
        "violatedPrinciple": None,
        "violationReason": "\uba85\uc2dc\uc801 \uc6d0\uce59 \uc704\ubc18\uc740 \ud655\uc778\ub418\uc9c0 \uc54a\uc558\uc9c0\ub9cc, \uc6d0\uce59\ubd07\uacfc \ub2e4\ub978 \ud310\ub2e8\uc774 \uc788\uc5c8\uc2b5\ub2c8\ub2e4.",
        "recommendedAction": _recommended_action(action, bot_action, None),
        "targetRule": None,
    }


class DeterministicReportAnalyzer:
    """Create all report judgments and rule recommendations without an LLM."""

    def build(
        self,
        simulated_trades: List[dict],
        participant_summary: List[dict],
        analytics: Optional[dict] = None,
    ) -> dict:
        analytics = analytics or {}
        actual_trades = [trade for trade in simulated_trades if _variant_id(trade) in ACTUAL_VARIANT_IDS]
        prices_by_security: Dict[int, List[tuple[str, float]]] = defaultdict(list)
        for price in analytics.get("dailyPrices") or []:
            security_id = int(price.get("securityId") or price.get("security_id") or 0)
            price_date = str(price.get("priceDate") or price.get("price_date") or "")[:10]
            close_price = float(price.get("closePrice") or price.get("close_price") or 0.0)
            if security_id and price_date and close_price > 0:
                prices_by_security[security_id].append((price_date, close_price))
        for series in prices_by_security.values():
            series.sort(key=lambda item: item[0])
        pattern_by_trade_id: Dict[object, List[str]] = {}
        pattern_by_security: Dict[int, List[str]] = {}
        patterns = analytics.get("behaviorPatterns") or []
        for pattern in patterns:
            code = str(pattern.get("patternCode") or "")
            for trade_id in pattern.get("evidenceTradeIds") or []:
                pattern_by_trade_id.setdefault(trade_id, []).append(code)
            for evidence in pattern.get("evidence") or []:
                security_id = int(evidence.get("securityId") or 0)
                pattern_by_security.setdefault(security_id, []).append(code)

        personal_actions_by_key: Dict[tuple[str, int], List[str]] = defaultdict(list)
        for trade in simulated_trades:
            if _variant_id(trade) != 2:
                continue
            key = (_trade_date(trade), int(trade.get("securityId") or trade.get("security_id") or 0))
            personal_actions_by_key[key].append(str(trade.get("tradeSide") or "HOLD"))
        moment_by_key = {
            (str(item.get("date") or "")[:10], int(item.get("securityId") or 0)): item
            for item in analytics.get("divergenceMoments") or []
        }
        compliance_by_trade = {
            item.get("tradeId"): item
            for item in (analytics.get("actualPrincipleCompliance") or {}).get("violations", [])
        }
        rule_schema = analytics.get("ruleSchema") or {}

        decision_reviews = []
        for trade in actual_trades:
            trade_id = _trade_id(trade)
            security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
            trade_date = _trade_date(trade)
            action = str(trade.get("tradeSide") or "HOLD")
            key = (trade_date, security_id)
            moment = moment_by_key.get(key, {})
            if not moment:
                nearby_moments = [
                    item for (moment_date, moment_security_id), item in moment_by_key.items()
                    if moment_security_id == security_id
                    and action in (item.get("actualUserActions") or [])
                    and _date_distance(moment_date, trade_date) <= 3
                ]
                moment = min(
                    nearby_moments,
                    key=lambda item: _date_distance(str(item.get("date") or "")[:10], trade_date),
                    default={},
                )
            bot_actions = personal_actions_by_key.get(key) or moment.get("personalBotActions") or ["HOLD"]
            bot_action = str(bot_actions[0])
            codes = list(pattern_by_trade_id.get(trade_id, []))
            codes.extend(pattern_by_security.get(security_id, []))
            codes.extend((compliance_by_trade.get(trade_id) or {}).get("reasonCodes") or [])
            if action != bot_action:
                codes.append("ACTUAL_BOT_ACTION_DIVERGENCE")

            outcome_5d = _future_price_outcome(trade, prices_by_security, 5)
            outcome_20d = _future_price_outcome(trade, prices_by_security, 20)
            subsequent = moment.get("subsequent5TradingDayReturnPercent")
            if subsequent is not None:
                subsequent = round(float(subsequent), 2)
                outcome_5d["returnPercent"] = subsequent
            else:
                subsequent = outcome_5d["returnPercent"]

            matched_code = next((code for code in PATTERN_CONFIG if code in codes), None)
            config = PATTERN_CONFIG.get(matched_code or "")
            compliance_codes = set((compliance_by_trade.get(trade_id) or {}).get("reasonCodes") or [])
            if config:
                explicit = _explicit_principle_match(rule_schema, config["targetRule"])
            elif action == bot_action:
                explicit = _applicable_explicit_principle(rule_schema, action)
            elif "ENTRY_RULE_VIOLATED" in compliance_codes:
                explicit = _applicable_explicit_principle(rule_schema, action, ("entry.",))
            elif "UNIVERSE_RULE_VIOLATED" in compliance_codes:
                explicit = _applicable_explicit_principle(rule_schema, action, ("universe.",))
            else:
                explicit = None
            principle = _principle_payload(explicit, config, bot_action)
            if explicit:
                principle_judgment = "FOLLOWED" if action == bot_action else "VIOLATED"
            elif action != bot_action:
                principle_judgment = "DECISION_DIFFERENCE"
            else:
                principle_judgment = "NOT_APPLICABLE"

            if config:
                tag, label = config["tag"], config["label"]
            elif principle_judgment == "FOLLOWED":
                tag, label = "PRINCIPLE_FOLLOWED", "원칙 준수"
            elif action in {"SELL", "REDUCE"} and subsequent is not None and subsequent > 0:
                tag, label = "EARLY_SELL", "조기 매도 가능성"
                codes.append("POSITIVE_POST_SELL_RETURN")
            elif action in {"BUY", "ADD"} and subsequent is not None and subsequent < 0:
                tag, label = "MISTIMED_BUY", "매수 시점 재검토"
                codes.append("NEGATIVE_POST_BUY_RETURN")
            else:
                tag, label = "RULE_DIVERGENCE", "원칙봇과 다른 결정"

            market_5d = _directional_outcome(action, outcome_5d["returnPercent"])
            market_20d = _directional_outcome(action, outcome_20d["returnPercent"])
            recorded_rationale = _clean_rationale(trade) or "사용자가 입력한 매매 근거 없음"
            feedback = (
                f"{_principle_feedback(principle_judgment, principle, bot_action)} "
                f"실제 매매 근거: {recorded_rationale}. 결과: {_outcome_text(action, subsequent)}."
            )
            decision_reviews.append({
                "tradeId": trade_id,
                "tradedAt": trade.get("tradedAt") or trade_date,
                "securityId": security_id,
                "securityName": trade.get("securityName") or trade.get("security_name") or f"종목 {security_id}",
                "action": action,
                "actionSummary": action,
                "decisionReason": recorded_rationale,
                "emotionTag": tag,
                "emotionLabel": label,
                "subsequentReturnPercent": subsequent,
                "returnPercent": subsequent,
                "principleBotAction": bot_action,
                "principleJudgment": principle_judgment,
                "matchedPrinciple": principle,
                "principleFeedback": feedback,
                "trade": _trade_snapshot(trade),
                "principleReview": _principle_review(action, bot_action, matched_code, sorted(set(codes))),
                "marketOutcome": {
                    "return5dPercent": outcome_5d["returnPercent"],
                    "return20dPercent": outcome_20d["returnPercent"],
                    "outcome5d": market_5d,
                    "outcome20d": market_20d,
                    "fiveTradingDays": outcome_5d,
                    "twentyTradingDays": outcome_20d,
                    "calculationSource": "SECURITY_DAILY_PRICES",
                },
                "reviewCase": _review_case(principle_judgment, market_5d),
                "outcome": {
                    "measurementPeriod": "5_TRADING_DAYS_AFTER_EXECUTION",
                    "priceReturnPercent": subsequent,
                    "summary": _outcome_text(action, subsequent),
                },
                "classificationSource": "DETERMINISTIC_RULE_ENGINE",
                "evidenceCodes": sorted(set(codes)),
            })

        decision_reviews.sort(key=lambda item: str(item.get("tradedAt") or ""), reverse=True)
        key_trade_reviews = sorted(
            decision_reviews,
            key=lambda item: (
                item.get("decisionReason") != "사용자가 입력한 매매 근거 없음",
                item.get("subsequentReturnPercent") is not None,
                abs(float(item.get("subsequentReturnPercent") or 0.0)),
                str(item.get("tradedAt") or ""),
            ),
            reverse=True,
        )[:3]
        key_trade_ids = {item.get("tradeId") for item in key_trade_reviews}
        decision_by_trade_id = {item.get("tradeId"): item for item in decision_reviews}

        evidence_reviews = []
        for trade in actual_trades:
            trade_id = _trade_id(trade)
            decision = decision_by_trade_id.get(trade_id, {})
            rationale = _clean_rationale(trade)
            basis_type, score, basis_source, database_type = _basis_classification(trade, rationale)
            action = str(trade.get("tradeSide") or "HOLD")
            security_name = trade.get("securityName") or trade.get("security_name") or "종목"
            evidence_reviews.append({
                "tradeId": trade_id,
                "tradedAt": trade.get("tradedAt") or _trade_date(trade),
                "securityId": int(trade.get("securityId") or trade.get("security_id") or 0),
                "securityName": security_name,
                "action": f"{security_name} {action}",
                "tradeAction": action,
                "basis": rationale or "사용자가 입력한 매매 근거 없음",
                "basisType": basis_type,
                "databaseBasisType": database_type,
                "basisTypeSource": basis_source,
                "verifiability": _verifiability(rationale, basis_type),
                "webVerdict": (
                    "PENDING" if rationale and trade_id in key_trade_ids
                    else "NOT_SELECTED" if rationale
                    else "UNCONFIRMED"
                ),
                "result": _outcome_text(action, decision.get("subsequentReturnPercent")),
                "returnPercent": decision.get("subsequentReturnPercent"),
                "marketOutcome": decision.get("marketOutcome", {}),
                "confidenceScore": score,
                "confidenceLabel": _confidence_label(score),
                "classificationSource": basis_source,
            })
        evidence_reviews.sort(key=lambda item: str(item.get("tradedAt") or ""), reverse=True)

        security_evidence_reviews = []
        actual_security_ids = sorted({int(item.get("securityId") or item.get("security_id") or 0) for item in actual_trades})
        for security_id in actual_security_ids:
            security_trades = [item for item in evidence_reviews if item["securityId"] == security_id]
            price_series = [
                {"date": price_date, "closePrice": close_price}
                for price_date, close_price in prices_by_security.get(security_id, [])
            ]
            annotations = []
            for review in [item for item in decision_reviews if item["securityId"] == security_id]:
                annotations.append({
                    "date": str(review.get("tradedAt") or "")[:10],
                    "type": review["action"],
                    "tradeId": review["tradeId"],
                    "label": "매수" if review["action"] in {"BUY", "ADD"} else "매도",
                })
                for period_key, label in (("fiveTradingDays", "5거래일 평가"), ("twentyTradingDays", "20거래일 평가")):
                    point = review.get("marketOutcome", {}).get(period_key, {})
                    if point.get("evaluationDate"):
                        annotations.append({
                            "date": point["evaluationDate"],
                            "type": "OUTCOME_CHECKPOINT",
                            "tradeId": review["tradeId"],
                            "label": label,
                        })
            security_evidence_reviews.append({
                "securityId": security_id,
                "securityName": security_trades[0]["securityName"] if security_trades else f"종목 {security_id}",
                "evidenceReviews": security_trades,
                "priceSeries": price_series,
                "chartAnnotations": sorted(annotations, key=lambda item: (item["date"], str(item.get("tradeId") or ""))),
            })

        principle_review_summary = {
            "followedCount": sum(item["principleJudgment"] == "FOLLOWED" for item in decision_reviews),
            "violatedCount": sum(item["principleJudgment"] == "VIOLATED" for item in decision_reviews),
            "decisionDifferenceCount": sum(item["principleJudgment"] == "DECISION_DIFFERENCE" for item in decision_reviews),
            "unassessedCount": sum(item["principleJudgment"] in {"NOT_APPLICABLE", "INSUFFICIENT_DATA"} for item in decision_reviews),
            "assessedTradeCount": sum(item["principleJudgment"] in {"FOLLOWED", "VIOLATED"} for item in decision_reviews),
            "totalTradeCount": len(decision_reviews),
        }

        actual_return = _participant_return(participant_summary, "ACTUAL_USER", 1)
        principle_return = _participant_return(participant_summary, "PERSONAL_BOT", 2)
        underperformed = sum(
            1 for item in decision_reviews
            if item["subsequentReturnPercent"] is not None
            and (
                (item["action"] in {"BUY", "ADD"} and item["subsequentReturnPercent"] < 0)
                or (item["action"] in {"SELL", "REDUCE"} and item["subsequentReturnPercent"] > 0)
            )
        )
        primary_pattern = max(patterns, key=lambda item: int(item.get("count") or 0), default=None)
        primary_text = (
            str(primary_pattern.get("description") or primary_pattern.get("label"))
            if primary_pattern
            else "반복적으로 확인된 행동 패턴이 없습니다."
        )
        learning_insights = {
            "primaryMistakePattern": primary_text,
            "emotionalTradeCount": sum(item["emotionTag"] in PATTERN_CONFIG for item in decision_reviews),
            "underperformedTradeCount": underperformed,
            "actualReturnPercent": actual_return,
            "principleReturnPercent": principle_return,
            "returnImprovementPercentPoint": round(principle_return - actual_return, 2),
            "calculationSource": "DETERMINISTIC_ANALYTICS",
            "narrative": _learning_narrative(actual_return, principle_return, primary_text),
            "narrativeSource": "DETERMINISTIC_TEMPLATE",
        }

        recommendation_codes = []
        patterns_by_code = {}
        for pattern in patterns:
            code = str(pattern.get("patternCode") or "")
            patterns_by_code[code] = pattern
            if code in PATTERN_CONFIG and code not in recommendation_codes:
                recommendation_codes.append(code)
        missing_rationale = any(
            _classify_basis(_clean_rationale(trade))[0] == "UNKNOWN"
            for trade in actual_trades
        )

        principle_discoveries = []
        principle_reinforcements = []
        improvement_actions = []
        for index, code in enumerate(recommendation_codes[:4], 1):
            config = PATTERN_CONFIG[code]
            recommendation_id = 2000 + index
            target_rule = config["targetRule"]
            current_value = _nested_value(rule_schema, target_rule)
            explicit_match = _explicit_principle_match(rule_schema, target_rule)
            is_reinforcement = explicit_match is not None
            proposed_value = _fallback_proposed_value(config, current_value)
            pattern = patterns_by_code.get(code, {})
            proposal = {
                "recommendationId": recommendation_id,
                "opportunityId": f"{code}:{target_rule}",
                "recommendationCode": code,
                "proposalType": "REINFORCEMENT" if is_reinforcement else "DISCOVERY",
                "principleType": config["principleType"],
                "title": config["title"],
                "description": config["description"],
                "targetRule": target_rule,
                "currentValue": current_value if is_reinforcement else None,
                "sourcePrincipleText": (
                    explicit_match.get("user_natural_text")
                    or explicit_match.get("userNaturalText")
                    or None
                    if explicit_match
                    else None
                ),
                "proposedValue": proposed_value,
                "allowedMinimum": config["allowedMinimum"],
                "allowedMaximum": config["allowedMaximum"],
                "strengthDirection": config["strengthDirection"],
                "changeType": (
                    "THRESHOLD_ADJUSTMENT"
                    if is_reinforcement and current_value != proposed_value
                    else "ENFORCEMENT_REINFORCEMENT"
                    if is_reinforcement
                    else "NEW_RULE"
                ),
                "ruleJson": _rule_json(target_rule, proposed_value),
                "evidence": {
                    "patternCode": code,
                    "count": int(pattern.get("count") or 0),
                    "tradeIds": (pattern.get("evidenceTradeIds") or [])[:10],
                    "details": (pattern.get("evidence") or [])[:10],
                },
                "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
                "proposalSource": "DETERMINISTIC_FALLBACK",
            }
            if is_reinforcement:
                principle_reinforcements.append(proposal)
            else:
                principle_discoveries.append(proposal)
            improvement_actions.append({
                "category": config["actionCategory"],
                "title": config["actionTitle"],
                "action": config["action"],
                "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
            })

        if missing_rationale:
            improvement_actions.append({
                "category": "EVIDENCE_DISCIPLINE",
                "title": "주문 전 근거 기록",
                "action": "매수·매도 주문 전에 판단 근거를 한 문장 이상 기록합니다.",
                "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
            })

        return {
            "reportVersion": "DETERMINISTIC_V12",
            "principleReviewSummary": principle_review_summary,
            "decisionReviews": decision_reviews,
            "keyTradeReviews": key_trade_reviews,
            "evidenceReviews": evidence_reviews,
            "securityEvidenceReviews": security_evidence_reviews,
            "learningInsights": learning_insights,
            "principleDiscoveries": principle_discoveries,
            "principleReinforcements": principle_reinforcements,
            "recommendedPrinciples": principle_discoveries + principle_reinforcements,
            "improvementActions": improvement_actions,
            "generationMetadata": {
                "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
                "narrativeSource": "NOT_REQUESTED",
                "proposalSource": "DETERMINISTIC_FALLBACK",
            },
        }
