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


def _five_day_return(trade: dict, prices_by_security: Dict[int, List[tuple[str, float]]]) -> Optional[float]:
    security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
    trade_date = _trade_date(trade)
    series = prices_by_security.get(security_id, [])
    start_index = next((index for index, item in enumerate(series) if item[0] >= trade_date), None)
    if start_index is None or start_index + 5 >= len(series) or series[start_index][1] <= 0:
        return None
    return round((series[start_index + 5][1] - series[start_index][1]) / series[start_index][1] * 100, 2)


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
        trades_by_key: Dict[tuple[str, int], List[dict]] = {}
        trades_by_id = {}
        for trade in actual_trades:
            key = (_trade_date(trade), int(trade.get("securityId") or trade.get("security_id") or 0))
            trades_by_key.setdefault(key, []).append(trade)
            trades_by_id[_trade_id(trade)] = trade

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

        decision_reviews = []
        # A single actual fill can be associated with more than one divergence
        # moment (for example, through the three-day fallback match below).
        # The review is trade-centric, so retain only one review per trade.
        reviewed_trade_ids = set()
        for moment in analytics.get("divergenceMoments") or []:
            date = str(moment.get("date") or "")[:10]
            security_id = int(moment.get("securityId") or 0)
            actual_actions = moment.get("actualUserActions") or ["HOLD"]
            bot_actions = moment.get("personalBotActions") or ["HOLD"]
            action = str(actual_actions[0])
            if action not in {"BUY", "ADD", "SELL", "REDUCE"}:
                continue
            trade = next(
                (item for item in trades_by_key.get((date, security_id), []) if item.get("tradeSide") == action),
                None,
            )
            if trade is None:
                same_action_trades = [
                    item for item in actual_trades
                    if int(item.get("securityId") or item.get("security_id") or 0) == security_id
                    and str(item.get("tradeSide") or "") == action
                ]
                nearest_trade = min(
                    same_action_trades,
                    key=lambda item: _date_distance(_trade_date(item), date),
                    default=None,
                )
                if nearest_trade is not None and _date_distance(_trade_date(nearest_trade), date) <= 3:
                    trade = nearest_trade
            if trade is None:
                continue
            trade_id = _trade_id(trade)
            if trade_id in reviewed_trade_ids:
                continue
            codes = list(pattern_by_trade_id.get(trade_id, []))
            codes.extend(pattern_by_security.get(security_id, []))
            subsequent = moment.get("subsequent5TradingDayReturnPercent")
            subsequent = round(float(subsequent), 2) if subsequent is not None else None
            if subsequent is None:
                subsequent = _five_day_return(trade, prices_by_security)
            rationale = _clean_rationale(trade)
            recorded_rationale = rationale or "사용자가 입력한 매매 근거 없음"

            matched_code = next((code for code in PATTERN_CONFIG if code in codes), None)
            if matched_code:
                tag = PATTERN_CONFIG[matched_code]["tag"]
                label = PATTERN_CONFIG[matched_code]["label"]
            elif action in {"SELL", "REDUCE"} and subsequent is not None and subsequent > 0:
                tag, label = "EARLY_SELL", "조기 매도 가능성"
                codes.append("POSITIVE_POST_SELL_RETURN")
            elif action in {"BUY", "ADD"} and subsequent is not None and subsequent < 0:
                tag, label = "MISTIMED_BUY", "매수 시점 재검토"
                codes.append("NEGATIVE_POST_BUY_RETURN")
            else:
                tag, label = "RULE_DIVERGENCE", "원칙봇과 다른 결정"
                codes.append("ACTUAL_BOT_ACTION_DIVERGENCE")

            bot_action = str(bot_actions[0])
            feedback = f"실제 매매 근거: {recorded_rationale}. 결과: {_outcome_text(action, subsequent)}."
            decision_reviews.append({
                "tradeId": trade_id,
                "tradedAt": trade.get("tradedAt") or date,
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
                "principleFeedback": feedback,
                "trade": _trade_snapshot(trade),
                "principleReview": _principle_review(action, bot_action, matched_code, sorted(set(codes))),
                "outcome": {
                    "measurementPeriod": "5_TRADING_DAYS_AFTER_EXECUTION",
                    "priceReturnPercent": subsequent,
                    "summary": _outcome_text(action, subsequent),
                },
                "classificationSource": "DETERMINISTIC_RULE_ENGINE",
                "evidenceCodes": sorted(set(codes)),
            })
            reviewed_trade_ids.add(trade_id)

        for pattern in patterns:
            code = str(pattern.get("patternCode") or "")
            config = PATTERN_CONFIG.get(code)
            if not config:
                continue
            candidate_trades = [
                trades_by_id.get(trade_id)
                for trade_id in pattern.get("evidenceTradeIds") or []
            ]
            if code == "DELAYED_STOP_LOSS":
                for evidence in pattern.get("evidence") or []:
                    security_id = int(evidence.get("securityId") or 0)
                    sells = [
                        trade for trade in actual_trades
                        if int(trade.get("securityId") or trade.get("security_id") or 0) == security_id
                        and trade.get("tradeSide") in {"SELL", "REDUCE"}
                    ]
                    if sells:
                        candidate_trades.append(max(sells, key=_trade_date))
            for trade in candidate_trades:
                if not trade or _trade_id(trade) in reviewed_trade_ids:
                    continue
                trade_id = _trade_id(trade)
                security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
                action = str(trade.get("tradeSide") or "HOLD")
                subsequent = _five_day_return(trade, prices_by_security)
                recorded_rationale = _clean_rationale(trade) or "사용자가 입력한 매매 근거 없음"
                decision_reviews.append({
                    "tradeId": trade_id,
                    "tradedAt": trade.get("tradedAt") or _trade_date(trade),
                    "securityId": security_id,
                    "securityName": trade.get("securityName") or trade.get("security_name") or f"종목 {security_id}",
                    "action": action,
                    "actionSummary": action,
                    "decisionReason": recorded_rationale,
                    "emotionTag": config["tag"],
                    "emotionLabel": config["label"],
                    "subsequentReturnPercent": subsequent,
                    "returnPercent": subsequent,
                    "principleBotAction": "NOT_COMPARED",
                    "principleFeedback": (
                        f"실제 매매 근거: {recorded_rationale}. "
                        f"결과: {_outcome_text(action, subsequent)}."
                    ),
                    "trade": _trade_snapshot(trade),
                    "principleReview": _principle_review(action, "NOT_COMPARED", code, [code]),
                    "outcome": {
                        "measurementPeriod": "5_TRADING_DAYS_AFTER_EXECUTION",
                        "priceReturnPercent": subsequent,
                        "summary": _outcome_text(action, subsequent),
                    },
                    "classificationSource": "DETERMINISTIC_RULE_ENGINE",
                    "evidenceCodes": [code],
                })
                reviewed_trade_ids.add(trade_id)

        decision_reviews.sort(
            key=lambda item: (
                item.get("subsequentReturnPercent") is not None,
                abs(float(item.get("subsequentReturnPercent") or 0.0)),
                str(item.get("tradedAt") or ""),
            ),
            reverse=True,
        )
        decision_reviews = [
            item for item in decision_reviews
            if item.get("subsequentReturnPercent") is not None
        ][:3]

        evidence_reviews = []
        evidence_candidates = [
            (decision, trades_by_id.get(decision.get("tradeId")))
            for decision in decision_reviews
        ]
        selected_trade_ids = {decision.get("tradeId") for decision in decision_reviews}
        supplemental_trades = []
        for trade in actual_trades:
            if _trade_id(trade) in selected_trade_ids:
                continue
            subsequent = _five_day_return(trade, prices_by_security)
            if subsequent is not None:
                supplemental_trades.append((abs(subsequent), subsequent, trade))
        supplemental_trades.sort(key=lambda item: (item[0], _trade_date(item[2])), reverse=True)
        for _, subsequent, trade in supplemental_trades:
            if len(evidence_candidates) >= 3:
                break
            evidence_candidates.append(({"subsequentReturnPercent": subsequent}, trade))
            selected_trade_ids.add(_trade_id(trade))

        for decision, trade in evidence_candidates:
            if not trade:
                continue
            trade_id = _trade_id(trade)
            rationale = _clean_rationale(trade)
            basis_type, score = _classify_basis(rationale)
            action = str(trade.get("tradeSide") or "HOLD")
            security_name = trade.get("securityName") or trade.get("security_name") or "종목"
            subsequent = decision.get("subsequentReturnPercent")
            if subsequent is None:
                subsequent = _five_day_return(trade, prices_by_security)
            evidence_reviews.append({
                "tradeId": trade_id,
                "tradedAt": trade.get("tradedAt") or _trade_date(trade),
                "securityId": int(trade.get("securityId") or trade.get("security_id") or 0),
                "securityName": security_name,
                "action": f"{security_name} {action}",
                "basis": rationale or "사용자가 입력한 매매 근거 없음",
                "basisType": basis_type,
                "result": _outcome_text(action, subsequent),
                "returnPercent": subsequent,
                "confidenceScore": score,
                "confidenceLabel": _confidence_label(score),
                "classificationSource": "DETERMINISTIC_KEYWORD_RULE",
            })

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
            "emotionalTradeCount": sum(item["emotionTag"] != "RULE_DIVERGENCE" for item in decision_reviews),
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

        rule_schema = analytics.get("ruleSchema") or {}
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
            "reportVersion": "DETERMINISTIC_V11",
            "decisionReviews": decision_reviews,
            "keyTradeReviews": decision_reviews,
            "evidenceReviews": evidence_reviews,
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
