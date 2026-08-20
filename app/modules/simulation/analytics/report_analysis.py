"""Deterministic post-simulation judgments used by the user-facing report."""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from typing import Dict, List, Optional

from app.modules.simulation.engine.evaluator import StockEvaluator
from app.modules.simulation.rules.rule_schema import SelectionRule
from app.modules.simulation.rules.strengthen_spec import build_strengthen_proposal
from app.modules.simulation.engine.strategy_catalog import (
    VALUE_QUALITY_REFERENCE_PRINCIPLES,
    VALUE_QUALITY_STRATEGY,
)


ACTUAL_VARIANT_IDS = {1, 1001}

# 리포트 형태의 식별자. 버전 사다리가 아니라 "이 형태인지"만 구분합니다.
REPORT_IDENTITY = "DETERMINISTIC"

# A principle needs this many assessed trades before "no problem found" is an
# honest conclusion. Below it the verdict says the evidence is still thin
# instead of clearing the principle.
RELIABLE_SAMPLE_MINIMUM = 5
# Post-trade return averages are hidden below this many samples.
OUTCOME_SAMPLE_MINIMUM = 3
STRENGTHEN_VIOLATION_RATE = 40.0

BUY_RULE_SECTIONS = ("universe", "selection", "entry", "portfolio", "additional_buy")
SELL_RULE_SECTIONS = ("exit", "rebalance")
SECTION_LABELS = {
    "universe": "투자 대상 종목군",
    "selection": "종목 평가",
    "entry": "신규 매수 진입",
    "additional_buy": "추가 매수",
    "portfolio": "비중·위험 관리",
    "exit": "매도·익절·손절",
    "rebalance": "리밸런싱",
}

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


def _principle_feedback(judgment: str, principle: Optional[dict], bot_action: str) -> str:
    title = (principle or {}).get("title") or "개인 원칙봇의 판단"
    if judgment == "FOLLOWED":
        return f"'{title}' 원칙에 맞게 행동했어요. 정한 원칙을 제대로 지켰습니다."
    if judgment == "VIOLATED":
        return f"'{title}' 원칙대로라면 이 시점에는 {bot_action} 판단을 먼저 따랐어야 해요."
    if judgment == "INSUFFICIENT_DATA":
        return "원칙 준수 여부를 판단할 데이터가 부족합니다."
    return "이 거래에 직접 연결할 수 있는 명시적인 사용자 원칙이 없습니다."


def _rule_paths(rule_json: dict, prefix: str = "") -> List[str]:
    paths = []
    for key, value in (rule_json or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            paths.extend(_rule_paths(value, path))
        else:
            paths.append(path)
    return paths


def _mapped_rule_paths(matched: Optional[dict], item_paths: List[str]) -> List[str]:
    """Every rule path one principle maps onto, in the compiler's order.

    A single sentence often carries several conditions, and forcing it onto one
    rule left the rest of the sentence unenforced.
    """
    matched = matched or {}
    paths = matched.get("ai_mapped_rules") or matched.get("mappedRules") or []
    if isinstance(paths, str):
        paths = [paths]
    ordered = [str(path).strip() for path in paths if str(path).strip()]
    primary = str(matched.get("ai_mapped_rule") or matched.get("mappedRule") or "").strip()
    if primary and primary not in ordered:
        ordered.insert(0, primary)
    if not ordered and len(item_paths) == 1:
        ordered = [item_paths[0]]
    seen = set()
    return [path for path in ordered if not (path in seen or seen.add(path))]


def _stated_rule_paths(matched: Optional[dict]) -> set:
    """규칙 중 사용자가 원문에 기준을 직접 밝힌 것들."""
    paths = (matched or {}).get("stated_rules") or (matched or {}).get("statedRules") or []
    if isinstance(paths, str):
        paths = [paths]
    return {str(path).strip() for path in paths if str(path).strip()}


def _resolve_current_value(
    target_rule: str,
    item_rule: dict,
    rule_schema: dict,
    confirmations: dict,
    stated_rules: Optional[set] = None,
) -> tuple[object, str]:
    """Find the threshold to judge against, and say where it came from.

    A number the compiler invented is not the user's standard. Judging a trade
    against it and calling the result a violation grades the user on a bar they
    never set, so the source travels with the value and gates what may be done
    with it.
    """
    if not target_rule:
        return None, "NOT_APPLICABLE"
    if target_rule in confirmations:
        return confirmations[target_rule], "USER_CONFIRMED"
    from_principle = _nested_value(item_rule, target_rule) if isinstance(item_rule, dict) else None
    if from_principle is not None:
        return from_principle, "PRINCIPLE_RULE_JSON"
    from_schema = _nested_value(rule_schema, target_rule)
    if from_schema is not None:
        # The compiler read this number out of the user's own sentence. Parsing
        # what someone wrote is not the same as inventing it, so it does not
        # need to be confirmed back to them.
        if stated_rules and target_rule in stated_rules:
            return from_schema, "USER_STATED"
        return from_schema, "AI_INFERRED"
    return None, "MISSING"


def _principle_catalog(analytics: dict, rule_schema: dict) -> List[dict]:
    """Return every active principle, including ones with no applicable trades."""
    audit = rule_schema.get("audit") if isinstance(rule_schema, dict) else {}
    interpreted = audit.get("interpreted_principles", []) if isinstance(audit, dict) else []
    principle_items = analytics.get("principleItems") or []
    confirmations = {
        str(item.get("targetRule") or item.get("target_rule") or ""): item.get("confirmedValue")
        if "confirmedValue" in item else item.get("confirmed_value")
        for item in (analytics.get("ruleConfirmations") or [])
        if isinstance(item, dict) and (item.get("targetRule") or item.get("target_rule"))
    }
    catalog = []

    if principle_items:
        for index, item in enumerate(principle_items, 1):
            text = str(item.get("principleText") or item.get("principle_text") or "").strip()
            item_rule = item.get("ruleJson") or item.get("rule_json") or {}
            item_paths = _rule_paths(item_rule) if isinstance(item_rule, dict) else []
            matched = next(
                (
                    candidate for candidate in interpreted
                    if isinstance(candidate, dict)
                    and str(candidate.get("user_natural_text") or candidate.get("userNaturalText") or "").strip() == text
                ),
                None,
            )
            if not matched and item_paths:
                matched = next(
                    (
                        candidate for candidate in interpreted
                        if isinstance(candidate, dict)
                        and str(candidate.get("ai_mapped_rule") or candidate.get("mappedRule") or "") in item_paths
                    ),
                    None,
                )
            target_rules = _mapped_rule_paths(matched, item_paths)
            target_rule = target_rules[0] if target_rules else ""
            stated = _stated_rule_paths(matched)
            current_value, value_source = _resolve_current_value(
                target_rule, item_rule, rule_schema, confirmations, stated
            )
            rules = []
            for path in target_rules:
                value, source = _resolve_current_value(
                    path, item_rule, rule_schema, confirmations, stated
                )
                rules.append({"targetRule": path, "currentValue": value, "valueSource": source})
            catalog.append({
                "principleSetItemId": item.get("principleSetItemId") or item.get("principle_set_item_id"),
                "principleText": text,
                "targetRule": target_rule or None,
                "targetRules": target_rules,
                "rules": rules,
                "status": str((matched or {}).get("status") or ("CONFIRMED" if target_rule else "REVIEW_REQUIRED")),
                "currentValue": current_value,
                "valueSource": value_source,
                "currentRuleJson": item_rule if isinstance(item_rule, dict) else {},
                "sortOrder": item.get("sortOrder") or item.get("sort_order") or index,
            })
        return catalog

    for index, item in enumerate(interpreted, 1):
        if not isinstance(item, dict):
            continue
        target_rules = _mapped_rule_paths(item, [])
        target_rule = target_rules[0] if target_rules else ""
        stated = _stated_rule_paths(item)
        current_value, value_source = _resolve_current_value(
            target_rule, {}, rule_schema, confirmations, stated
        )
        rules = []
        for path in target_rules:
            value, source = _resolve_current_value(path, {}, rule_schema, confirmations, stated)
            rules.append({"targetRule": path, "currentValue": value, "valueSource": source})
        catalog.append({
            "principleSetItemId": item.get("principle_set_item_id") or item.get("principleSetItemId"),
            "principleText": str(item.get("user_natural_text") or item.get("userNaturalText") or "").strip(),
            "targetRule": target_rule or None,
            "targetRules": target_rules,
            "rules": rules,
            "status": str(item.get("status") or "CONFIRMED"),
            "currentValue": current_value,
            "valueSource": value_source,
            "currentRuleJson": _rule_json(target_rule, current_value) if target_rule and current_value is not None else {},
            "sortOrder": index,
        })
    return catalog


def _directional_return(review: dict, period: str) -> Optional[float]:
    value = review.get("marketOutcome", {}).get(period)
    if value is None:
        return None
    numeric = float(value)
    return round(-numeric if review.get("action") in {"SELL", "REDUCE"} else numeric, 2)


def _average(values: List[float], minimum_count: int = 1) -> Optional[float]:
    """Average only when the sample is large enough to be worth showing."""
    if len(values) < max(1, minimum_count):
        return None
    return round(sum(values) / len(values), 2)


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> Optional[float]:
    """Return the 95% lower bound of a rate so small samples cannot look certain."""
    if total <= 0:
        return None
    observed = successes / total
    denominator = 1 + z ** 2 / total
    centre = observed + z ** 2 / (2 * total)
    margin = z * ((observed * (1 - observed) / total + z ** 2 / (4 * total ** 2)) ** 0.5)
    return round(max(0.0, (centre - margin) / denominator) * 100, 1)


def _evidence_strength(applicable_count: int, violation_lower_bound: Optional[float]) -> str:
    if applicable_count >= RELIABLE_SAMPLE_MINIMUM * 2 and (violation_lower_bound or 0) >= STRENGTHEN_VIOLATION_RATE:
        return "STRONG"
    if applicable_count >= RELIABLE_SAMPLE_MINIMUM:
        return "MODERATE"
    return "PRELIMINARY"


def _build_principle_evaluations(
    analytics: dict,
    rule_schema: dict,
    decision_reviews: List[dict],
) -> tuple[List[dict], List[dict]]:
    evaluations = []
    reinforcements = []
    catalog = _principle_catalog(analytics, rule_schema)

    for index, principle in enumerate(catalog, 1):
        target_rule = principle.get("targetRule")
        principle_text = principle.get("principleText") or "투자 원칙"
        principle_id = principle.get("principleSetItemId")
        related = []
        for review in decision_reviews:
            for match in review.get("principleMatches", []):
                same_principle = (
                    principle_id is not None
                    and match.get("principleSetItemId") == principle_id
                ) or (
                    principle_id is None
                    and target_rule
                    and match.get("targetRule") == target_rule
                    and match.get("principleText") == principle_text
                )
                if same_principle:
                    related.append((review, match))
                    break

        followed = [review for review, match in related if match.get("judgment") == "FOLLOWED"]
        violated = [review for review, match in related if match.get("judgment") == "VIOLATED"]
        value_source = str(principle.get("valueSource") or "MISSING")
        # With several rules behind one principle, tighten the one that actually
        # broke rather than whichever happened to be listed first.
        violation_counts: Dict[str, int] = defaultdict(int)
        for _, match in related:
            for rule_result in match.get("ruleResults") or []:
                if rule_result.get("judgment") == "VIOLATED" and rule_result.get("targetRule"):
                    violation_counts[str(rule_result["targetRule"])] += 1
        suggestion_rule = (
            max(violation_counts.items(), key=lambda item: (item[1], item[0]))[0]
            if violation_counts
            else target_rule
        )
        suggestion_value = next(
            (
                rule.get("currentValue")
                for rule in principle.get("rules") or []
                if rule.get("targetRule") == suggestion_rule
            ),
            principle.get("currentValue"),
        )
        suggestion_value_source = next(
            (
                str(rule.get("valueSource") or "MISSING")
                for rule in principle.get("rules") or []
                if rule.get("targetRule") == suggestion_rule
            ),
            value_source,
        )
        # The measured value on each followed trade is the band the user proved
        # they can stay inside. A proposal derived from it beats a constant.
        followed_actual_values = [
            rule_result["evidence"]["actualValue"]
            for _, match in related
            for rule_result in (match.get("ruleResults") or [])
            if rule_result.get("targetRule") == suggestion_rule
            and rule_result.get("judgment") == "FOLLOWED"
            and isinstance(rule_result.get("evidence"), dict)
            and isinstance(rule_result["evidence"].get("actualValue"), (int, float))
            and not isinstance(rule_result["evidence"].get("actualValue"), bool)
        ]
        applicable_count = len(followed) + len(violated)
        violation_rate = round(len(violated) / applicable_count * 100, 1) if applicable_count else None
        followed_5d = [value for item in followed if (value := _directional_return(item, "return5dPercent")) is not None]
        followed_20d = [value for item in followed if (value := _directional_return(item, "return20dPercent")) is not None]
        violated_5d = [value for item in violated if (value := _directional_return(item, "return5dPercent")) is not None]
        violated_20d = [value for item in violated if (value := _directional_return(item, "return20dPercent")) is not None]
        violation_lower_bound = _wilson_lower_bound(len(violated), applicable_count)
        evidence_strength = _evidence_strength(applicable_count, violation_lower_bound)
        status = str(principle.get("status") or "REVIEW_REQUIRED")

        if status != "CONFIRMED" or not target_rule:
            verdict = "REVIEW"
            reason = "실행 규칙으로 명확하게 해석되지 않아 원칙 문구와 기준을 직접 검토해야 합니다."
        elif applicable_count < 2:
            verdict = "INSUFFICIENT_DATA"
            reason = f"평가 가능한 거래가 {applicable_count}건이라 원칙을 변경하기에는 데이터가 부족합니다."
        elif len(violated) >= 2 and (violation_rate or 0) >= STRENGTHEN_VIOLATION_RATE:
            if suggestion_value_source == "AI_INFERRED":
                # The repeated violation is real, but it was measured against a
                # number the compiler invented because the sentence had none.
                # Ask the user what the bar is before proposing to tighten it.
                verdict = "CONFIRM_THRESHOLD"
                reason = (
                    f"적용 거래 {applicable_count}건 중 {len(violated)}건이 기준을 벗어났습니다. "
                    "다만 이 기준값은 원칙 문구에 수치가 없어 AI가 추정한 값입니다. "
                    "기준을 확정해 주시면 다음 회차부터 강화안을 제안합니다."
                )
            else:
                verdict = "STRENGTHEN"
                reason = f"적용 거래 {applicable_count}건 중 {len(violated)}건에서 원칙을 지키지 않아 실행 기준 강화가 필요합니다."
        elif (
            len(followed_5d) >= OUTCOME_SAMPLE_MINIMUM
            and len(followed_20d) >= OUTCOME_SAMPLE_MINIMUM
            and (_average(followed_5d) or 0) < 0
            and (_average(followed_20d) or 0) < 0
        ):
            verdict = "REVISE"
            reason = "원칙을 지킨 거래에서도 불리한 5거래일·20거래일 결과가 반복되어 기준을 다시 검토할 필요가 있습니다."
        elif applicable_count < RELIABLE_SAMPLE_MINIMUM:
            # Never clear a principle on two or three trades. Say the evidence is
            # still thin so the next simulation has a reason to exist.
            verdict = "EARLY_SIGNAL"
            shortfall = RELIABLE_SAMPLE_MINIMUM - applicable_count
            observed = (
                f"{len(violated)}건에서 원칙을 지키지 않았습니다"
                if violated
                else "아직 위반이 확인되지 않았습니다"
            )
            reason = (
                f"적용 거래 {applicable_count}건 중 {observed}. "
                f"판단을 확정하기에는 표본이 적어 {shortfall}건이 더 쌓이면 평가합니다."
            )
        else:
            verdict = "KEEP"
            reason = f"평가 가능한 거래 {applicable_count}건에서 현재 원칙을 변경할 만큼 반복적인 문제가 확인되지 않았습니다."

        evaluation_id = f"PE_{principle_id or index}_{str(target_rule or 'REVIEW').replace('.', '_')}"
        suggestion = None
        if verdict == "STRENGTHEN":
            proposal = build_strengthen_proposal(
                suggestion_rule,
                suggestion_value,
                followed_actual_values,
            )
            if proposal:
                proposed_value = proposal["proposedValue"]
                # A rule whose value cannot move still gets a proposal, but it
                # must not claim to change the rule JSON.
                rule_json = (
                    {}
                    if proposal["changeType"] == "ENFORCEMENT_REINFORCEMENT"
                    else _rule_json(suggestion_rule, proposed_value)
                )
                suggestion = {
                    "recommendationId": 4000 + index,
                    "evaluationId": evaluation_id,
                    "opportunityId": f"PRINCIPLE:{principle_id or index}:{suggestion_rule}",
                    "proposalType": "REINFORCEMENT",
                    "principleSetItemId": principle_id,
                    "principleType": proposal["principleType"],
                    "title": proposal["title"],
                    "description": proposal["description"],
                    "sourcePrincipleText": principle_text,
                    "targetRule": suggestion_rule,
                    "currentValue": proposal["currentValue"],
                    "proposedValue": proposed_value,
                    "allowedMinimum": proposal["allowedMinimum"],
                    "allowedMaximum": proposal["allowedMaximum"],
                    "strengthDirection": proposal["strengthDirection"],
                    "changeType": proposal["changeType"],
                    "valueBasis": proposal["valueBasis"],
                    "ruleJson": rule_json,
                    "evidence": {
                        "applicableCount": applicable_count,
                        "followedCount": len(followed),
                        "violatedCount": len(violated),
                        "followedValueSampleCount": len(followed_actual_values),
                        "tradeIds": [item.get("tradeId") for item in violated[:10]],
                    },
                    "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
                    "proposalSource": "DETERMINISTIC_FALLBACK",
                }
                reinforcements.append(suggestion)

        evaluations.append({
            "evaluationId": evaluation_id,
            "principleSetItemId": principle_id,
            "principleText": principle_text,
            "targetRule": target_rule,
            "targetRules": principle.get("targetRules") or ([target_rule] if target_rule else []),
            "currentValue": principle.get("currentValue"),
            "valueSource": value_source,
            "verdict": verdict,
            "evaluationReason": reason,
            "statistics": {
                "applicableCount": applicable_count,
                "followedCount": len(followed),
                "violatedCount": len(violated),
                "violationRatePercent": violation_rate,
                "violationRateLowerBoundPercent": violation_lower_bound,
                "reliableSampleMinimum": RELIABLE_SAMPLE_MINIMUM,
                "sampleShortfall": max(0, RELIABLE_SAMPLE_MINIMUM - applicable_count),
                "evidenceStrength": evidence_strength,
                "unassessedCount": sum(
                    match.get("judgment") in {"NOT_APPLICABLE", "INSUFFICIENT_DATA"}
                    for _, match in related
                ),
            },
            "outcomes": {
                "calculationBasis": "DIRECTION_ADJUSTED_POST_TRADE_RETURN",
                "minimumSampleCount": OUTCOME_SAMPLE_MINIMUM,
                "followed5dAveragePercent": _average(followed_5d, OUTCOME_SAMPLE_MINIMUM),
                "followed20dAveragePercent": _average(followed_20d, OUTCOME_SAMPLE_MINIMUM),
                "violated5dAveragePercent": _average(violated_5d, OUTCOME_SAMPLE_MINIMUM),
                "violated20dAveragePercent": _average(violated_20d, OUTCOME_SAMPLE_MINIMUM),
                "sampleCounts": {
                    "followed5d": len(followed_5d),
                    "followed20d": len(followed_20d),
                    "violated5d": len(violated_5d),
                    "violated20d": len(violated_20d),
                },
            },
            "evidenceTradeIds": [
                review.get("tradeId")
                for review, match in related
                if match.get("judgment") in {"FOLLOWED", "VIOLATED"}
            ][:20],
            "suggestion": suggestion,
            # Replaced by a real replay when the run has the price data loaded.
            # Kept here so rebuilt reports keep the same shape.
            "counterfactual": {
                "supported": False,
                "reasonCode": "NOT_COMPUTED",
                "reason": "이 리포트에서는 대안 시나리오를 계산하지 않았습니다.",
                "method": None,
            },
            "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
        })

    return evaluations, reinforcements


PERSONAL_BOT_VARIANT_ID = 2
# 매수는 오른 만큼, 매도는 내린 만큼이 잘한 것입니다. 관망은 0점입니다.
ACTION_DIRECTION = {"BUY": 1.0, "ADD": 1.0, "SELL": -1.0, "REDUCE": -1.0, "HOLD": 0.0}


def _action_score(actions: List[str], return_percent: Optional[float]) -> Optional[float]:
    """Score one side's action by the move that followed it."""
    if return_percent is None:
        return None
    direction = next(
        (ACTION_DIRECTION[action] for action in actions if action in ACTION_DIRECTION),
        0.0,
    )
    return round(direction * float(return_percent), 2)


def _build_divergence_review(
    analytics: dict,
    simulated_trades: List[dict],
    decision_reviews: List[dict],
    outcome_evidence: dict,
) -> dict:
    """Where the user and their own principle bot acted differently, and how each fared.

    This does not claim the moments below add up to the performance gap: they are
    the points where the two split, scored one at a time. Attributing the whole
    difference to them would be arithmetic the data does not support.
    """
    securities = {
        int(item.get("securityId") or item.get("security_id") or 0): item
        for item in analytics.get("securitySnapshots") or []
    }
    bot_reason_by_key: Dict[tuple, str] = {}
    for trade in simulated_trades:
        if _variant_id(trade) != PERSONAL_BOT_VARIANT_ID:
            continue
        key = (_trade_date(trade), int(trade.get("securityId") or trade.get("security_id") or 0))
        reason = str(trade.get("decisionReason") or trade.get("rationaleText") or "").strip()
        if reason:
            bot_reason_by_key.setdefault(key, reason)

    violations_by_key: Dict[tuple, List[dict]] = defaultdict(list)
    for review in decision_reviews:
        key = (str(review.get("tradedAt") or "")[:10], int(review.get("securityId") or 0))
        for match in review.get("principleMatches", []):
            if match.get("judgment") == "VIOLATED":
                violations_by_key[key].append({
                    "principleSetItemId": match.get("principleSetItemId"),
                    "principleText": match.get("principleText"),
                    "targetRule": match.get("targetRule"),
                    "reason": match.get("reason"),
                })

    moments = []
    for moment in analytics.get("divergenceMoments") or []:
        date = str(moment.get("date") or "")[:10]
        security_id = int(moment.get("securityId") or 0)
        key = (date, security_id)
        user_actions = list(moment.get("actualUserActions") or ["HOLD"])
        bot_actions = list(moment.get("personalBotActions") or ["HOLD"])
        return_percent = moment.get("subsequent5TradingDayReturnPercent")
        user_score = _action_score(user_actions, return_percent)
        bot_score = _action_score(bot_actions, return_percent)
        if user_score is None or bot_score is None:
            better = "UNKNOWN"
        elif user_score > bot_score:
            better = "ACTUAL_USER"
        elif bot_score > user_score:
            better = "PERSONAL_BOT"
        else:
            better = "TIED"
        moments.append({
            "date": date,
            "securityId": security_id,
            "securityName": securities.get(security_id, {}).get("securityName") or f"종목 {security_id}",
            "userActions": user_actions,
            "botActions": bot_actions,
            "botReason": bot_reason_by_key.get(key),
            "return5dPercent": return_percent,
            "userScore": user_score,
            "botScore": bot_score,
            "betterSide": better,
            "violatedPrinciples": violations_by_key.get(key, []),
        })

    counts = defaultdict(int)
    for item in moments:
        counts[item["betterSide"]] += 1
    return {
        "gapPercentPoint": outcome_evidence.get("principleBotGapPercentPoint"),
        "momentCount": len(moments),
        "botBetterCount": counts["PERSONAL_BOT"],
        "userBetterCount": counts["ACTUAL_USER"],
        "tiedCount": counts["TIED"],
        "undeterminedCount": counts["UNKNOWN"],
        "moments": moments,
        "measurementPeriod": "5_TRADING_DAYS_AFTER_DIVERGENCE",
        "attributionNote": (
            "각 시점의 결과를 따로 채점한 값입니다. 이 시점들이 전체 수익률 차이를 "
            "모두 설명한다는 뜻은 아닙니다."
        ),
        "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
    }


def _rank_participants(participant_summary: List[dict], simulated_trades: List[dict]) -> List[dict]:
    """Order the run's participants by return, best first."""
    trade_counts: Dict[int, int] = defaultdict(int)
    for trade in simulated_trades:
        trade_counts[_variant_id(trade)] += 1
    ranked = sorted(
        (
            {
                "variantId": int(item.get("variantId") or 0),
                "variantType": str(item.get("variantType") or ""),
                "variantName": item.get("variantName"),
                "cumulativeReturnPercent": round(float(item.get("cumulativeReturnPercent") or 0.0), 2),
                "mddPercent": round(float(item.get("mddPercent") or 0.0), 2),
                "tradeCount": trade_counts.get(int(item.get("variantId") or 0), 0),
            }
            for item in participant_summary
        ),
        key=lambda item: item["cumulativeReturnPercent"],
        reverse=True,
    )
    for position, item in enumerate(ranked, 1):
        item["rank"] = position
    return ranked


def _outcome_branch(ranked: List[dict], review_summary: dict) -> str:
    """Pick the one story this run has to tell.

    Rank alone decides which participant the report talks about, but it never
    decides whether the user is praised: a first place reached while breaking
    the user's own rules is luck, and saying otherwise would undo the whole
    point of judging process separately from outcome.
    """
    by_type = {item["variantType"]: item for item in ranked}
    personal = by_type.get("PERSONAL_BOT")
    if not ranked or (personal is not None and personal["tradeCount"] == 0):
        # A bot that never traded has a 0% line, not a comparable result.
        return "INCONCLUSIVE"

    winner = ranked[0]["variantType"]
    if winner == "RANDOM_BOT":
        return "MARKET_LUCK"
    if winner == "ACTUAL_USER":
        if review_summary.get("violatedCount", 0) >= 2:
            return "USER_AHEAD_LUCKY"
        return "USER_AHEAD_DISCIPLINED"
    if winner == "PERSONAL_BOT":
        return "BOT_AHEAD"
    if winner == "FAMOUS_STRATEGY":
        return "REFERENCE_AHEAD"
    # A winner this function does not recognise must not fall through into a
    # story about some other participant. Say the comparison is unusable.
    return "INCONCLUSIVE"


OUTCOME_COPY = {
    "INCONCLUSIVE": (
        "이번 회차는 비교할 수 없습니다",
        "원칙봇이 한 건도 매매하지 않아 순위를 견줄 수 없습니다. 원칙과 종목 데이터를 확인해 주세요.",
        "COVERAGE",
    ),
    "MARKET_LUCK": (
        "이 기간엔 아무렇게나 사도 벌었습니다",
        "무작위 매매가 1위였습니다. 이번 회차 수익률로는 원칙의 좋고 나쁨을 판단하지 마세요.",
        "PERFORMANCE_CONTEXT",
    ),
    "USER_AHEAD_DISCIPLINED": (
        "결과도 좋았고, 원칙도 지켰습니다",
        "직접 한 매매가 1위였고 원칙을 어긴 거래도 반복되지 않았습니다. 지금 방식을 유지할 근거가 있습니다.",
        "PRINCIPLE_EVALUATIONS",
    ),
    "USER_AHEAD_LUCKY": (
        "1위였지만, 원칙을 지켜서 얻은 결과는 아닙니다",
        "직접 한 매매가 1위였습니다. 다만 스스로 정한 기준을 반복해서 넘겼기 때문에 같은 방식이 반복되면 결과는 달라질 수 있습니다.",
        "PRINCIPLE_EVALUATIONS",
    ),
    "BOT_AHEAD": (
        "원칙을 그대로 지킨 쪽이 앞섰습니다",
        "당신의 원칙만 따라 매매한 결과가 실제 매매보다 좋았습니다. 어디서 갈라졌는지 거래 단위로 확인해 보세요.",
        "DIVERGENCE",
    ),
    "REFERENCE_AHEAD": (
        "비교 전략이 앞선 기간입니다",
        "이 전략은 당신 원칙에 없는 기준을 사용합니다. 수익률이 아니라 그 기준이 무엇인지를 보세요.",
        "REFERENCE_PRINCIPLES",
    ),
}


def _build_outcome(
    participant_summary: List[dict],
    simulated_trades: List[dict],
    review_summary: dict,
    coverage: dict,
) -> dict:
    """The report's spine: one ranked answer, and where to look next."""
    ranked = _rank_participants(participant_summary, simulated_trades)
    branch = _outcome_branch(ranked, review_summary)
    headline, detail, focus = OUTCOME_COPY[branch]
    actual = next((item for item in ranked if item["variantType"] == "ACTUAL_USER"), None)
    personal = next((item for item in ranked if item["variantType"] == "PERSONAL_BOT"), None)
    gap = (
        round(personal["cumulativeReturnPercent"] - actual["cumulativeReturnPercent"], 2)
        if actual and personal
        else None
    )
    return {
        "branch": branch,
        "headline": headline,
        "detail": detail,
        "focusSection": focus,
        "winnerVariantType": ranked[0]["variantType"] if ranked else None,
        "ranking": ranked,
        "evidence": {
            "actualReturnPercent": actual["cumulativeReturnPercent"] if actual else None,
            "principleBotReturnPercent": personal["cumulativeReturnPercent"] if personal else None,
            "principleBotGapPercentPoint": gap,
            "assessedTradeCount": review_summary.get("assessedTradeCount", 0),
            "violatedTradeCount": review_summary.get("violatedCount", 0),
            "uncoveredTradeCount": coverage.get("uncoveredTradeCount", 0),
            "totalTradeCount": review_summary.get("totalTradeCount", 0),
        },
        "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
    }


def _build_principle_set_diagnostics(
    catalog: List[dict],
    decision_reviews: List[dict],
    rule_schema: Optional[dict] = None,
) -> dict:
    """Judge the principle set as a whole, not one principle at a time.

    Per-principle verdicts can all read "fine" while the set has no sell rule at
    all, or two principles quietly bound to the same rule path.
    """
    audit = (rule_schema or {}).get("audit") if isinstance(rule_schema, dict) else {}
    audit = audit if isinstance(audit, dict) else {}
    interpreted_principles = audit.get("interpreted_principles") or audit.get("interpretedPrinciples") or []
    principle_conflicts = audit.get("principle_conflicts") or audit.get("principleConflicts") or []

    covered_trade_ids = set()
    applicable_by_section: Dict[str, int] = defaultdict(int)
    for review in decision_reviews:
        for match in review.get("principleMatches", []):
            if match.get("applicability") != "APPLICABLE":
                continue
            covered_trade_ids.add(review.get("tradeId"))
            section = str(match.get("targetRule") or "").split(".", 1)[0]
            if section:
                applicable_by_section[section] += 1

    uncovered = [
        review for review in decision_reviews
        if review.get("tradeId") not in covered_trade_ids
    ]
    total_trade_count = len(decision_reviews)

    declared_sections = {
        str(principle.get("targetRule") or "").split(".", 1)[0]
        for principle in catalog
        if principle.get("targetRule")
    }
    buy_trade_count = sum(review.get("action") in {"BUY", "ADD"} for review in decision_reviews)
    sell_trade_count = sum(review.get("action") in {"SELL", "REDUCE"} for review in decision_reviews)
    missing_sections = []
    if buy_trade_count and not declared_sections.intersection(BUY_RULE_SECTIONS):
        missing_sections.append({
            "sectionGroup": "BUY",
            "sections": list(BUY_RULE_SECTIONS),
            "relatedTradeCount": buy_trade_count,
            "message": f"매수 거래 {buy_trade_count}건을 설명할 원칙이 없습니다.",
        })
    if sell_trade_count and not declared_sections.intersection(SELL_RULE_SECTIONS):
        missing_sections.append({
            "sectionGroup": "SELL",
            "sections": list(SELL_RULE_SECTIONS),
            "relatedTradeCount": sell_trade_count,
            "message": f"매도 거래 {sell_trade_count}건을 설명할 원칙이 없습니다.",
        })

    by_rule: Dict[str, List[dict]] = defaultdict(list)
    for principle in catalog:
        if principle.get("targetRule"):
            by_rule[str(principle["targetRule"])].append(principle)
    duplicates = [
        {
            "targetRule": target_rule,
            "principleSetItemIds": [item.get("principleSetItemId") for item in items],
            "principleTexts": [item.get("principleText") for item in items],
            "message": (
                f"{len(items)}개 원칙이 같은 실행 규칙({target_rule})에 연결되어 "
                "통계가 중복 집계됩니다."
            ),
        }
        for target_rule, items in sorted(by_rule.items())
        if len(items) > 1
    ]

    # A principle that cannot become a rule used to sit in REVIEW with no
    # explanation, run after run. The compiler now says why, so carry it here.
    reason_by_text = {
        str(item.get("user_natural_text") or item.get("userNaturalText") or "").strip():
            str(item.get("unmappable_reason") or item.get("unmappableReason") or "")
        for item in interpreted_principles
        if isinstance(item, dict)
    }
    unmapped = [
        {
            "principleSetItemId": principle.get("principleSetItemId"),
            "principleText": principle.get("principleText"),
            "reason": reason_by_text.get(str(principle.get("principleText") or "").strip())
            or "실행 규칙으로 해석되지 않아 원칙 문구와 기준을 직접 검토해야 합니다.",
        }
        for principle in catalog
        if not principle.get("targetRule") or str(principle.get("status")) != "CONFIRMED"
    ]

    # Semantic clashes the rule paths cannot see: two principles in different
    # sections that tell the user to do opposite things in the same moment.
    known_texts = {str(principle.get("principleText") or "").strip() for principle in catalog}
    conflicts = [
        {
            "firstPrincipleText": str(item.get("first_principle_text") or item.get("firstPrincipleText") or "").strip(),
            "secondPrincipleText": str(item.get("second_principle_text") or item.get("secondPrincipleText") or "").strip(),
            "conflictType": str(item.get("conflict_type") or item.get("conflictType") or "CONTRADICTION"),
            "reason": str(item.get("reason") or ""),
            "judgmentSource": "LLM_PRINCIPLE_REVIEW",
        }
        for item in principle_conflicts
        if isinstance(item, dict)
    ]
    conflicts = [
        item for item in conflicts
        if item["firstPrincipleText"] in known_texts
        and item["secondPrincipleText"] in known_texts
    ]

    return {
        "principleCount": len(catalog),
        "conflicts": conflicts,
        "coverage": {
            "totalTradeCount": total_trade_count,
            "coveredTradeCount": len(covered_trade_ids),
            "uncoveredTradeCount": len(uncovered),
            "uncoveredTradeRatePercent": (
                round(len(uncovered) / total_trade_count * 100, 1) if total_trade_count else None
            ),
            "uncoveredTradeIds": [review.get("tradeId") for review in uncovered][:20],
            "applicableCountBySection": {
                section: applicable_by_section.get(section, 0)
                for section in SECTION_LABELS
            },
            "sectionLabels": SECTION_LABELS,
        },
        "missingSections": missing_sections,
        "duplicateRules": duplicates,
        "unmappedPrinciples": unmapped,
        "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
    }


def _build_performance_context(
    analytics: dict,
    participant_summary: List[dict],
) -> dict:
    """Surface the comparison numbers the backtest already produced.

    The simulation computes a benchmark set and a random-bot distribution, but
    the report used to drop both, leaving "why did I lose" unanswered.
    """
    actual_return = _participant_return(participant_summary, "ACTUAL_USER", 1)
    personal_return = _participant_return(participant_summary, "PERSONAL_BOT", 2)

    distribution = analytics.get("randomDistribution") or {}
    values = distribution.get("distributionPercent") or []
    actual_percentile = (
        round(sum(value <= actual_return for value in values) / len(values) * 100, 1)
        if values
        else None
    )
    luck_check = None
    if actual_percentile is not None:
        # 무작위 매매의 몇 퍼센트가 이 기간에 돈을 벌었는지가, 그 기간이
        # 누구에게나 관대했는지를 가장 직접적으로 말해 줍니다.
        profitable_percent = round(sum(value > 0 for value in values) / len(values) * 100, 1)
        luck_check = {
            "runCount": distribution.get("runCount"),
            "actualUserPercentile": actual_percentile,
            "personalBotPercentile": distribution.get("personalBotPercentile"),
            "medianReturnPercent": distribution.get("medianReturnPercent"),
            "lowerQuartileReturnPercent": distribution.get("lowerQuartileReturnPercent"),
            "upperQuartileReturnPercent": distribution.get("upperQuartileReturnPercent"),
            "minimumReturnPercent": distribution.get("minimumReturnPercent"),
            "maximumReturnPercent": distribution.get("maximumReturnPercent"),
            "profitableRunPercent": profitable_percent,
            "summary": (
                f"무작위 매매 {distribution.get('runCount')}회 분포에서 "
                f"실제 투자 수익률은 상위 {round(100 - actual_percentile, 1)}% 수준입니다."
            ),
            "periodSummary": (
                f"무작위로 사고팔았을 때도 {profitable_percent:g}%가 수익을 냈습니다."
                if profitable_percent >= 50
                else f"무작위로 사고팔았을 때 수익을 낸 경우는 {profitable_percent:g}%였습니다."
            ),
            "disclaimer": "무작위 분포 비교는 실력 검증이 아니라 결과의 우연성 참고 지표입니다.",
        }

    benchmarks = []
    for item in analytics.get("benchmarks") or []:
        benchmark_return = item.get("returnPercent")
        if benchmark_return is None:
            continue
        benchmarks.append({
            "benchmark": item.get("benchmark"),
            "returnPercent": benchmark_return,
            "method": item.get("method"),
            "actualExcessPercentPoint": round(actual_return - float(benchmark_return), 2),
            "personalBotExcessPercentPoint": round(personal_return - float(benchmark_return), 2),
        })

    contributions = [
        item for item in (analytics.get("securityContributions") or [])
        if int(item.get("variantId") or 0) in ACTUAL_VARIANT_IDS
    ]
    total_absolute = sum(abs(float(item.get("contributionAmount") or 0.0)) for item in contributions)
    top_contributors = [
        {
            "securityId": item.get("securityId"),
            "securityName": item.get("securityName"),
            "contributionAmount": item.get("contributionAmount"),
            "sharePercent": (
                round(abs(float(item.get("contributionAmount") or 0.0)) / total_absolute * 100, 1)
                if total_absolute
                else None
            ),
        }
        for item in contributions[:5]
    ]

    return {
        "actualReturnPercent": actual_return,
        "principleReturnPercent": personal_return,
        "luckCheck": luck_check,
        "benchmarks": benchmarks,
        "topSecurityContributions": top_contributors,
        "calculationSource": "DETERMINISTIC_ANALYTICS",
    }


def _principle_match_result(
    principle: dict,
    trade: dict,
    applicability: str,
    judgment: str,
    expected_action: Optional[str],
    reason: str,
    evidence: Optional[dict] = None,
) -> dict:
    return {
        "principleSetItemId": principle.get("principleSetItemId"),
        "principleText": principle.get("principleText") or "투자 원칙",
        "targetRule": principle.get("targetRule"),
        "applicability": applicability,
        "judgment": judgment,
        "expectedAction": expected_action,
        "actualAction": str(trade.get("tradeSide") or "HOLD"),
        "reason": reason,
        "evidence": evidence or {},
        "matchingMethod": "RULE_PREDICATE_AT_TRADE_TIME",
    }


def _principle_match_context(analytics: dict, actual_trades: List[dict]) -> dict:
    prices = {
        (
            str(item.get("priceDate") or item.get("price_date") or "")[:10],
            int(item.get("securityId") or item.get("security_id") or 0),
        ): item
        for item in analytics.get("dailyPrices") or []
    }
    securities = {
        int(item.get("securityId") or item.get("security_id") or 0): item
        for item in analytics.get("securitySnapshots") or []
    }
    performance = {}
    for item in analytics.get("dailyPerformance") or []:
        variant_id = int(item.get("variantId") or item.get("simulationVariantId") or 0)
        if variant_id not in ACTUAL_VARIANT_IDS:
            continue
        snapshot_date = str(item.get("performanceDate") or item.get("snapshotDate") or "")[:10]
        performance[(snapshot_date, 1)] = item
    positions_by_date: Dict[tuple[str, int], List[dict]] = defaultdict(list)
    positions_by_security: Dict[int, List[dict]] = defaultdict(list)
    for item in analytics.get("positionSnapshots") or []:
        if int(item.get("simulationVariantId") or item.get("variantId") or 0) not in ACTUAL_VARIANT_IDS:
            continue
        snapshot_date = str(item.get("snapshotDate") or item.get("performanceDate") or "")[:10]
        security_id = int(item.get("securityId") or 0)
        positions_by_date[(snapshot_date, 1)].append(item)
        positions_by_security[security_id].append(item)
    for items in positions_by_security.values():
        items.sort(key=lambda item: str(item.get("snapshotDate") or ""))
    ordered_trades = sorted(actual_trades, key=lambda item: (_trade_date(item), str(_trade_id(item))))
    return {
        "prices": prices,
        "securities": securities,
        "performance": performance,
        "positionsByDate": positions_by_date,
        "positionsBySecurity": positions_by_security,
        "actualTrades": ordered_trades,
    }


def _previous_position(context: dict, trade: dict) -> Optional[dict]:
    trade_date = _trade_date(trade)
    security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
    candidates = [
        item for item in context["positionsBySecurity"].get(security_id, [])
        if str(item.get("snapshotDate") or "")[:10] < trade_date
    ]
    return candidates[-1] if candidates else None


def _portfolio_evidence(context: dict, trade: dict) -> tuple[Optional[float], List[dict]]:
    trade_date = _trade_date(trade)
    performance = context["performance"].get((trade_date, 1), {})
    total_equity = performance.get("portfolioValue") or performance.get("totalEquity")
    positions = context["positionsByDate"].get((trade_date, 1), [])
    return (float(total_equity) if total_equity else None), positions


def _numeric_rule_match(
    principle: dict,
    trade: dict,
    actual_value,
    rule_value,
    operator: str,
    actual_label: str,
) -> dict:
    if actual_value is None or rule_value is None:
        return _principle_match_result(
            principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None,
            f"{actual_label} 데이터가 없어 원칙 준수 여부를 계산할 수 없습니다.",
            {"actualValue": actual_value, "ruleValue": rule_value, "operator": operator},
        )
    actual_number = float(actual_value)
    rule_number = float(rule_value)
    passed = actual_number <= rule_number if operator == "<=" else actual_number >= rule_number
    action = str(trade.get("tradeSide") or "HOLD")
    expected = action if passed else "HOLD"
    return _principle_match_result(
        principle,
        trade,
        "APPLICABLE",
        "FOLLOWED" if passed else "VIOLATED",
        expected,
        (
            f"{actual_label} {actual_number:g}이(가) 원칙 기준 {operator} {rule_number:g}을 충족했습니다."
            if passed
            else f"{actual_label} {actual_number:g}이(가) 원칙 기준 {operator} {rule_number:g}을 벗어났습니다."
        ),
        {"actualValue": actual_number, "ruleValue": rule_number, "operator": operator},
    )


def _combine_rule_results(principle: dict, trade: dict, results: List[dict]) -> dict:
    """Fold one principle's several rule checks into a single judgment.

    A sentence like "only buy liquid names that have not spiked" is two rules,
    and a person reading it means both. Breaking the failure of either one is
    breaking the principle, so any violation carries; a principle counts as
    followed only when something applied and nothing broke.
    """
    judgments = [item["judgment"] for item in results]
    if "VIOLATED" in judgments:
        combined = "VIOLATED"
    elif "FOLLOWED" in judgments:
        combined = "FOLLOWED"
    elif judgments and all(item == "NOT_APPLICABLE" for item in judgments):
        combined = "NOT_APPLICABLE"
    else:
        combined = "INSUFFICIENT_DATA"

    representative = next(
        (item for item in results if item["judgment"] == combined),
        results[0] if results else {},
    )
    rule_results = [
        {
            "targetRule": item.get("targetRule"),
            "applicability": item.get("applicability"),
            "judgment": item.get("judgment"),
            "reason": item.get("reason"),
            "evidence": item.get("evidence") or {},
        }
        for item in results
    ]
    reason = str(representative.get("reason") or "")
    if len(results) > 1 and representative.get("targetRule"):
        reason = f"[{representative['targetRule']}] {reason}"
    return {
        "principleSetItemId": principle.get("principleSetItemId"),
        "principleText": principle.get("principleText") or "투자 원칙",
        "targetRule": representative.get("targetRule") or principle.get("targetRule"),
        "targetRules": [item.get("targetRule") for item in results],
        "applicability": "APPLICABLE" if combined in {"FOLLOWED", "VIOLATED"} else combined,
        "judgment": combined,
        "expectedAction": representative.get("expectedAction"),
        "actualAction": str(trade.get("tradeSide") or "HOLD"),
        "reason": reason,
        "evidence": representative.get("evidence") or {},
        "ruleResults": rule_results,
        "matchingMethod": "RULE_PREDICATE_AT_TRADE_TIME",
    }


def _evaluate_principle_matches(
    principle: dict,
    trade: dict,
    context: dict,
    rule_schema: dict,
    evidence_codes: List[str],
) -> dict:
    """Judge every rule this principle maps onto, then combine them."""
    rules = principle.get("rules") or []
    if not rules:
        return _evaluate_principle_for_trade(principle, trade, context, rule_schema, evidence_codes)
    results = []
    for rule in rules:
        single = {
            **principle,
            "targetRule": rule.get("targetRule"),
            "currentValue": rule.get("currentValue"),
            "status": rule.get("status", principle.get("status")),
        }
        result = _evaluate_principle_for_trade(single, trade, context, rule_schema, evidence_codes)
        result["targetRule"] = rule.get("targetRule")
        results.append(result)
    if len(results) == 1:
        results[0]["targetRules"] = [results[0].get("targetRule")]
        results[0]["ruleResults"] = [{
            "targetRule": results[0].get("targetRule"),
            "applicability": results[0].get("applicability"),
            "judgment": results[0].get("judgment"),
            "reason": results[0].get("reason"),
            "evidence": results[0].get("evidence") or {},
        }]
        return results[0]
    return _combine_rule_results(principle, trade, results)


def _evaluate_principle_for_trade(
    principle: dict,
    trade: dict,
    context: dict,
    rule_schema: dict,
    evidence_codes: List[str],
) -> dict:
    action = str(trade.get("tradeSide") or "HOLD")
    target_rule = str(principle.get("targetRule") or "")
    status = str(principle.get("status") or "REVIEW_REQUIRED")
    section = target_rule.split(".", 1)[0] if target_rule else ""
    buy_sections = {"universe", "selection", "entry", "portfolio", "additional_buy"}
    sell_sections = {"exit", "rebalance"}

    if status != "CONFIRMED" or not target_rule:
        return _principle_match_result(
            principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None,
            "원칙이 실행 가능한 Rule 경로로 확정되지 않았습니다.",
        )
    if section in buy_sections and action not in {"BUY", "ADD"}:
        return _principle_match_result(
            principle, trade, "NOT_APPLICABLE", "NOT_APPLICABLE", None,
            "매수·추가매수에 적용되는 원칙이라 이 거래에는 적용되지 않습니다.",
        )
    if section in sell_sections and action not in {"SELL", "REDUCE"}:
        return _principle_match_result(
            principle, trade, "NOT_APPLICABLE", "NOT_APPLICABLE", None,
            "매도·비중축소에 적용되는 원칙이라 이 거래에는 적용되지 않습니다.",
        )

    trade_date = _trade_date(trade)
    security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
    price = context["prices"].get((trade_date, security_id))
    security = context["securities"].get(security_id, {})
    rule_value = principle.get("currentValue")
    if rule_value is None:
        rule_value = _nested_value(rule_schema, target_rule)

    if section in {"universe", "selection", "entry"} and not price:
        return _principle_match_result(
            principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None,
            "거래일의 종목 가격·시장 데이터가 없어 원칙을 평가할 수 없습니다.",
        )

    if target_rule == "universe.allowed_markets":
        market = security.get("marketType")
        if market is None:
            return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "종목 시장 구분 데이터가 없습니다.")
        allowed = list(rule_value or [])
        passed = market in allowed
        return _principle_match_result(
            principle, trade, "APPLICABLE", "FOLLOWED" if passed else "VIOLATED", action if passed else "HOLD",
            f"종목 시장 {market}이(가) 허용 시장에 {'포함됩니다' if passed else '포함되지 않습니다'}.",
            {"actualValue": market, "ruleValue": allowed, "operator": "IN"},
        )
    if target_rule == "universe.min_market_cap":
        return _numeric_rule_match(principle, trade, price.get("marketCap", security.get("marketCap")), rule_value, ">=", "시가총액")
    if target_rule == "universe.min_daily_trading_value":
        return _numeric_rule_match(principle, trade, price.get("tradingValue"), rule_value, ">=", "일 거래대금")
    if target_rule in {"universe.exclude_halted", "universe.exclude_administrative"}:
        actual = (
            bool(price.get("isHalted") or security.get("isHalted"))
            if target_rule.endswith("exclude_halted")
            else bool(security.get("isAdministrative"))
        )
        passed = not actual if bool(rule_value) else True
        return _principle_match_result(
            principle, trade, "APPLICABLE", "FOLLOWED" if passed else "VIOLATED", action if passed else "HOLD",
            "제외 대상 종목이 아닙니다." if passed else "원칙에서 제외한 종목에 해당합니다.",
            {"actualValue": actual, "ruleValue": rule_value, "operator": "EXCLUDE_IF_TRUE"},
        )
    if target_rule == "entry.max_5day_return":
        return _numeric_rule_match(principle, trade, price.get("day5Return"), rule_value, "<=", "최근 5거래일 수익률")
    if target_rule == "entry.moving_average_condition":
        condition = str(rule_value or "NONE").upper()
        close = price.get("closePrice")
        ma5 = price.get("movingAverage5")
        ma20 = price.get("movingAverage20")
        if condition == "NONE":
            passed = True
        elif close is None or (condition in {"ABOVE_MA5", "MA5_ABOVE_MA20"} and ma5 is None) or (condition in {"ABOVE_MA20", "MA5_ABOVE_MA20"} and ma20 is None):
            return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "이동평균 데이터가 부족합니다.")
        elif condition == "ABOVE_MA5":
            passed = float(close) > float(ma5)
        elif condition == "ABOVE_MA20":
            passed = float(close) > float(ma20)
        elif condition == "MA5_ABOVE_MA20":
            passed = float(ma5) > float(ma20)
        else:
            return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "지원하지 않는 이동평균 조건입니다.")
        return _principle_match_result(
            principle, trade, "APPLICABLE", "FOLLOWED" if passed else "VIOLATED", action if passed else "HOLD",
            "이동평균 진입 조건을 충족했습니다." if passed else "이동평균 진입 조건을 충족하지 못했습니다.",
            {"closePrice": close, "movingAverage5": ma5, "movingAverage20": ma20, "ruleValue": condition},
        )
    if target_rule == "entry.require_positive_disclosure":
        return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "거래 시점의 공시 방향 스냅샷이 없어 이 원칙을 평가할 수 없습니다.")
    if target_rule.startswith("selection."):
        selection = rule_schema.get("selection") or {}
        evaluator = StockEvaluator()
        scores = evaluator.calculate_factor_scores(security_id, price, security)
        selection_rule = SelectionRule(
            factor_weights=dict(selection.get("factor_weights") or {}),
            min_passing_score=float(selection.get("min_passing_score", 0.0)),
        )
        fit_score = evaluator.calculate_personal_fit_score(scores, selection_rule)
        available = [value for name, value in scores.items() if value is not None and selection_rule.factor_weights.get(name, 0) > 0]
        if not available:
            return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "종목 선택 팩터 데이터가 부족합니다.")
        result = _numeric_rule_match(principle, trade, fit_score, selection_rule.min_passing_score, ">=", "종목 평가 점수")
        result["evidence"].update({
            "factorScores": scores,
            "factorWeights": selection_rule.factor_weights,
        })
        return result
    if target_rule.startswith("portfolio."):
        total_equity, positions = _portfolio_evidence(context, trade)
        if total_equity is None or total_equity <= 0:
            return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "거래일 포트폴리오 평가금액이 없습니다.")
        if target_rule == "portfolio.max_position_count":
            return _numeric_rule_match(principle, trade, len(positions), rule_value, "<=", "보유 종목 수")
        if target_rule == "portfolio.max_single_position_weight":
            position = next((item for item in positions if int(item.get("securityId") or 0) == security_id), None)
            market_value = float((position or {}).get("marketValue") or 0.0)
            if not position:
                market_value = float(trade.get("quantity") or 0) * float(trade.get("unitPrice") or 0)
            return _numeric_rule_match(principle, trade, market_value / total_equity, rule_value, "<=", "거래 후 종목 비중")
        if target_rule == "portfolio.max_sector_weight":
            sector = security.get("sectorName")
            if not sector:
                return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "종목 업종 데이터가 없습니다.")
            sector_value = sum(
                float(item.get("marketValue") or 0.0)
                for item in positions
                if context["securities"].get(int(item.get("securityId") or 0), {}).get("sectorName") == sector
            )
            return _numeric_rule_match(principle, trade, sector_value / total_equity, rule_value, "<=", "거래 후 업종 비중")
    if target_rule.startswith("additional_buy."):
        if action != "ADD":
            return _principle_match_result(principle, trade, "NOT_APPLICABLE", "NOT_APPLICABLE", None, "추가매수 거래가 아니라 적용되지 않습니다.")
        if target_rule == "additional_buy.allowed":
            passed = bool(rule_value)
            return _principle_match_result(principle, trade, "APPLICABLE", "FOLLOWED" if passed else "VIOLATED", action if passed else "HOLD", "추가매수가 허용됩니다." if passed else "원칙상 추가매수가 허용되지 않습니다.", {"actualValue": True, "ruleValue": rule_value})
        if target_rule == "additional_buy.max_additional_count":
            count = sum(
                item is trade or (
                    int(item.get("securityId") or item.get("security_id") or 0) == security_id
                    and str(item.get("tradeSide") or "") == "ADD"
                    and (_trade_date(item), str(_trade_id(item))) <= (trade_date, str(_trade_id(trade)))
                )
                for item in context["actualTrades"]
            )
            return _numeric_rule_match(principle, trade, count, rule_value, "<=", "누적 추가매수 횟수")
        if target_rule == "additional_buy.trigger_drop_rate":
            previous = _previous_position(context, trade)
            actual_return = float(previous.get("returnPercent")) / 100 if previous and previous.get("returnPercent") is not None else None
            return _numeric_rule_match(principle, trade, actual_return, rule_value, "<=", "추가매수 전 보유 수익률")
        if target_rule == "additional_buy.additional_weight":
            total_equity, _ = _portfolio_evidence(context, trade)
            weight = (float(trade.get("quantity") or 0) * float(trade.get("unitPrice") or 0) / total_equity) if total_equity else None
            return _numeric_rule_match(principle, trade, weight, rule_value, "<=", "추가매수 비중")
    if target_rule.startswith("exit."):
        previous = _previous_position(context, trade)
        average_price = float(previous.get("averagePrice") or 0.0) if previous else 0.0
        execution_price = float(trade.get("unitPrice") or trade.get("unit_price") or 0.0)
        actual_return = (
            (execution_price - average_price) / average_price
            if average_price > 0 and execution_price > 0
            else float(previous.get("returnPercent")) / 100
            if previous and previous.get("returnPercent") is not None
            else None
        )
        if target_rule == "exit.stop_loss_rate" and "DELAYED_STOP_LOSS" in evidence_codes:
            return _principle_match_result(
                principle, trade, "APPLICABLE", "VIOLATED", "SELL",
                "손실 제한 기준 도달 후 매도가 지연된 패턴이 확인됐습니다.",
                {"actualValue": actual_return, "ruleValue": rule_value, "patternCode": "DELAYED_STOP_LOSS"},
            )
        if target_rule in {"exit.take_profit_rate", "exit.stop_loss_rate"}:
            if actual_return is None or rule_value is None:
                return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "매도 전 보유 수익률 데이터가 없습니다.")
            triggered = actual_return >= float(rule_value) if target_rule.endswith("take_profit_rate") else actual_return <= float(rule_value)
            if not triggered:
                return _principle_match_result(
                    principle, trade, "NOT_APPLICABLE", "NOT_APPLICABLE", None,
                    "이 매도 시점에는 해당 수익률 기준이 발동하지 않았습니다.",
                    {"actualValue": actual_return, "ruleValue": rule_value},
                )
            return _principle_match_result(
                principle, trade, "APPLICABLE", "FOLLOWED", "SELL",
                "매도 시점에 원칙의 수익률 기준이 발동했고 실제로 매도했습니다.",
                {"actualValue": actual_return, "ruleValue": rule_value},
            )
        if target_rule == "exit.max_holding_days":
            prior_buys = [
                item for item in context["actualTrades"]
                if int(item.get("securityId") or item.get("security_id") or 0) == security_id
                and str(item.get("tradeSide") or "") in {"BUY", "ADD"}
                and _trade_date(item) <= trade_date
            ]
            if not prior_buys:
                return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "시뮬레이션 이전 취득일을 확인할 수 없습니다.")
            holding_days = (date_type.fromisoformat(trade_date) - date_type.fromisoformat(_trade_date(prior_buys[0]))).days
            if holding_days < int(rule_value):
                return _principle_match_result(principle, trade, "NOT_APPLICABLE", "NOT_APPLICABLE", None, "최대 보유 기간이 아직 도래하지 않았습니다.", {"actualValue": holding_days, "ruleValue": rule_value})
            return _principle_match_result(principle, trade, "APPLICABLE", "FOLLOWED", "SELL", "최대 보유 기간 도달 후 매도했습니다.", {"actualValue": holding_days, "ruleValue": rule_value})
        return _principle_match_result(principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None, "거래 시점 공시 또는 리밸런싱 정보가 없어 이 매도 원칙을 평가할 수 없습니다.")

    return _principle_match_result(
        principle, trade, "INSUFFICIENT_DATA", "INSUFFICIENT_DATA", None,
        "현재 리포트 분석기가 지원하지 않는 Rule 경로입니다.",
    )


def _build_reference_review(
    catalog: List[dict],
    references: List[dict],
    participant_summary: List[dict],
) -> dict:
    """What the comparison strategy checks that the user's principles never do.

    The strategy finishing first is what brings the user to this section, but it
    is deliberately not the reason anything is suggested here: one quarter is far
    too short to establish that a strategy is better. The reason is structural --
    it applies a rule in an area the user has left empty -- and the return rides
    along as a labelled reference number only.
    """
    strategy_return = _participant_return(
        participant_summary,
        VALUE_QUALITY_STRATEGY["variantType"],
        VALUE_QUALITY_STRATEGY["variantId"],
    )
    user_sections = {
        str(principle.get("targetRule") or "").split(".", 1)[0]
        for principle in catalog
        if principle.get("targetRule")
    }
    strategy_sections = [
        section for section in VALUE_QUALITY_STRATEGY
        if section in SECTION_LABELS
    ]
    missing = [
        {
            "section": section,
            "sectionLabel": SECTION_LABELS[section],
            "strategyRules": sorted(
                rule for candidate in VALUE_QUALITY_REFERENCE_PRINCIPLES
                for rule in candidate["targetRules"]
                if str(rule).split(".", 1)[0] == section
            ),
        }
        for section in strategy_sections
        if section not in user_sections
    ]
    return {
        "strategyName": VALUE_QUALITY_STRATEGY["strategyName"],
        "botName": VALUE_QUALITY_STRATEGY["variantName"],
        "ruleSource": "SYSTEM_STRATEGY_CONFIG",
        "referenceReturnPercent": strategy_return,
        # The screen must not turn this number into the reason to adopt anything.
        "returnUsedAsEvidence": False,
        "missingSections": missing,
        "missingSectionCount": len(missing),
        "references": references,
        "adoptionMode": "REVIEW_ONLY",
        "disclaimer": (
            "한 기간의 수익률은 이 전략이 더 낫다는 근거가 되지 않습니다. "
            "지금 내 원칙이 다루지 않는 영역이 무엇인지만 확인하세요."
        ),
        "judgmentSource": "SYSTEM_STRATEGY_CONFIG",
    }


def _build_reference_principles(
    analytics: dict,
    rule_schema: dict,
    simulated_trades: List[dict],
    participant_summary: List[dict],
) -> List[dict]:
    """Suggest up to two non-duplicate rules from the configured comparator."""
    famous_summary = next(
        (
            item for item in participant_summary
            if item.get("variantType") == VALUE_QUALITY_STRATEGY["variantType"]
            or int(item.get("variantId") or 0) == VALUE_QUALITY_STRATEGY["variantId"]
        ),
        None,
    )
    if not famous_summary:
        return []

    explicit_paths = set()
    for principle in _principle_catalog(analytics, rule_schema):
        target_rule = str(principle.get("targetRule") or "").lower()
        if target_rule:
            explicit_paths.add(target_rule)
        explicit_paths.update(
            path.lower() for path in _rule_paths(principle.get("currentRuleJson") or {})
        )

    famous_trades = [
        trade for trade in simulated_trades
        if _variant_id(trade) == VALUE_QUALITY_STRATEGY["variantId"]
    ]
    strategy_return = _participant_return(
        participant_summary,
        VALUE_QUALITY_STRATEGY["variantType"],
        VALUE_QUALITY_STRATEGY["variantId"],
    )
    references = []
    for candidate in VALUE_QUALITY_REFERENCE_PRINCIPLES:
        target_rules = [str(path).lower() for path in candidate["targetRules"]]
        if any(path in explicit_paths for path in target_rules):
            continue
        applicable_sides = set(candidate["applicableTradeSides"])
        applied_trades = [
            trade for trade in famous_trades
            if str(trade.get("tradeSide") or "") in applicable_sides
        ]
        references.append({
            "referenceId": candidate["referenceId"],
            "sourceBot": {
                "variantId": VALUE_QUALITY_STRATEGY["variantId"],
                "variantType": VALUE_QUALITY_STRATEGY["variantType"],
                "strategyName": VALUE_QUALITY_STRATEGY["strategyName"],
                "source": "SYSTEM_STRATEGY_CONFIG",
            },
            "recommendationOrigin": {
                "originType": "COMPARATOR_STRATEGY",
                "originLabel": "비교 전략 참고",
                "botName": VALUE_QUALITY_STRATEGY["variantName"],
                "strategyName": VALUE_QUALITY_STRATEGY["strategyName"],
                "ruleSource": "SYSTEM_STRATEGY_CONFIG",
                "ruleSourceLabel": "서비스에 설정된 고정 비교 전략",
                "reason": "현재 사용자 원칙에 같은 실행 규칙이 없어 비교 전략의 기준을 참고용으로 제시했습니다.",
            },
            "title": candidate["title"],
            "description": candidate["description"],
            "targetRules": candidate["targetRules"],
            "ruleJson": candidate["ruleJson"],
            "comparisonEvidence": {
                "userHasSimilarPrinciple": False,
                "botAppliedTradeCount": len(applied_trades),
                "tradeIds": [_trade_id(trade) for trade in applied_trades[:10]],
                "simulationReturnPercent": strategy_return,
                "performanceUsedForSelection": False,
            },
            "adoptionMode": "REVIEW_ONLY",
            "disclaimer": "비교 전략의 시뮬레이션 수익률만으로 이 원칙의 우수성이 입증되는 것은 아닙니다.",
            "judgmentSource": "SYSTEM_STRATEGY_CONFIG",
        })
        if len(references) == 2:
            break
    return references


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
        "status": "NOT_COVERED",
        "violatedPrinciple": None,
        "violationReason": "\uc774 \uac70\ub798\uc5d0 \uc801\uc6a9\ud560 \uc218 \uc788\ub294 \uc6d0\uce59\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.",
        "recommendedAction": None,
        "targetRule": None,
    }


def _principle_review_from_match(selected_match: Optional[dict], fallback: dict) -> dict:
    if not selected_match:
        return fallback
    judgment = selected_match.get("judgment")
    if judgment == "VIOLATED":
        return {
            "status": "RULE_VIOLATED",
            "violatedPrinciple": selected_match.get("principleText"),
            "violationReason": selected_match.get("reason"),
            "recommendedAction": selected_match.get("expectedAction"),
            "targetRule": selected_match.get("targetRule"),
            "evidence": selected_match.get("evidence") or {},
        }
    return {
        "status": "RULE_FOLLOWED",
        "violatedPrinciple": None,
        "violationReason": None,
        "recommendedAction": selected_match.get("expectedAction"),
        "targetRule": selected_match.get("targetRule"),
        "evidence": selected_match.get("evidence") or {},
    }


class DeterministicReportAnalyzer:
    """Create report judgments and evaluate the user's existing principles."""

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
        principle_catalog = _principle_catalog(analytics, rule_schema)
        principle_match_context = _principle_match_context(analytics, actual_trades)

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
            principle_matches = [
                _evaluate_principle_matches(
                    principle_item,
                    trade,
                    principle_match_context,
                    rule_schema,
                    sorted(set(codes)),
                )
                for principle_item in principle_catalog
            ]
            selected_match = next(
                (item for item in principle_matches if item["judgment"] == "VIOLATED"),
                None,
            ) or next(
                (item for item in principle_matches if item["judgment"] == "FOLLOWED"),
                None,
            )
            principle = None
            if selected_match:
                principle = {
                    "principleSetItemId": selected_match.get("principleSetItemId"),
                    "title": selected_match.get("principleText") or "사용자 원칙",
                    "originalText": selected_match.get("principleText") or "",
                    "source": "USER_PRINCIPLE",
                    "targetRule": selected_match.get("targetRule"),
                    "expectedAction": selected_match.get("expectedAction"),
                }
                principle_judgment = selected_match["judgment"]
            elif any(item["judgment"] == "INSUFFICIENT_DATA" for item in principle_matches):
                principle_judgment = "INSUFFICIENT_DATA"
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
            expected_action = (selected_match or {}).get("expectedAction") or bot_action
            feedback = (
                f"{_principle_feedback(principle_judgment, principle, expected_action)} "
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
                "principleMatches": principle_matches,
                "principleFeedback": feedback,
                "trade": _trade_snapshot(trade),
                "principleReview": _principle_review_from_match(
                    selected_match,
                    _principle_review(action, bot_action, matched_code, sorted(set(codes))),
                ),
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

        principle_evaluations, principle_reinforcements = _build_principle_evaluations(
            analytics,
            rule_schema,
            decision_reviews,
        )
        evaluation_counts = {
            verdict: sum(item["verdict"] == verdict for item in principle_evaluations)
            for verdict in ("KEEP", "STRENGTHEN", "REVISE", "REVIEW", "EARLY_SIGNAL",
                            "CONFIRM_THRESHOLD", "INSUFFICIENT_DATA")
        }
        principle_evaluation_summary = {
            "totalCount": len(principle_evaluations),
            "keepCount": evaluation_counts["KEEP"],
            "strengthenCount": evaluation_counts["STRENGTHEN"],
            "reviseCount": evaluation_counts["REVISE"],
            "reviewCount": evaluation_counts["REVIEW"],
            "earlySignalCount": evaluation_counts["EARLY_SIGNAL"],
            "confirmThresholdCount": evaluation_counts["CONFIRM_THRESHOLD"],
            "insufficientDataCount": evaluation_counts["INSUFFICIENT_DATA"],
        }
        principle_set_diagnostics = _build_principle_set_diagnostics(
            principle_catalog,
            decision_reviews,
            rule_schema,
        )
        performance_context = _build_performance_context(analytics, participant_summary)
        reference_principles = _build_reference_principles(
            analytics,
            rule_schema,
            simulated_trades,
            participant_summary,
        )

        outcome = _build_outcome(
            participant_summary,
            simulated_trades,
            principle_review_summary,
            principle_set_diagnostics["coverage"],
        )

        divergence_review = _build_divergence_review(
            analytics,
            simulated_trades,
            decision_reviews,
            outcome["evidence"],
        )

        return {
            "reportVersion": REPORT_IDENTITY,
            "outcome": outcome,
            "divergenceReview": divergence_review,
            "principleReviewSummary": principle_review_summary,
            "decisionReviews": decision_reviews,
            "keyTradeReviews": key_trade_reviews,
            "evidenceReviews": evidence_reviews,
            "securityEvidenceReviews": security_evidence_reviews,
            "learningInsights": learning_insights,
            "principleEvaluationSummary": principle_evaluation_summary,
            "principleEvaluations": principle_evaluations,
            "principleSetDiagnostics": principle_set_diagnostics,
            "performanceContext": performance_context,
            "principleReinforcements": principle_reinforcements,
            "referenceReview": _build_reference_review(
                principle_catalog, reference_principles, participant_summary
            ),
            "generationMetadata": {
                "judgmentSource": "DETERMINISTIC_RULE_ENGINE",
                "narrativeSource": "NOT_REQUESTED",
                "proposalSource": "DETERMINISTIC_FALLBACK",
                "referencePrincipleSource": (
                    "SYSTEM_STRATEGY_CONFIG" if reference_principles else "NOT_AVAILABLE"
                ),
            },
        }
