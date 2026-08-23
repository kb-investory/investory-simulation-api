"""How each executable rule gets tightened, and by how much.

Reinforcement used to exist for three rule paths only, so a principle could be
judged STRENGTHEN and still hand the screen no button. This module covers every
rule path the evaluator can judge, and derives the proposed number from the
user's own trades instead of a constant.

Two things are separated on purpose:

* the *shape* of a tightening (which direction is stricter, what values are
  allowed, how to word it) lives in ``RULE_STRENGTHEN_SPEC``;
* the *value* comes from evidence -- the band the user actually stayed inside on
  the trades they did follow -- and only falls back to a fixed step when there is
  no such evidence.
"""

from __future__ import annotations

from typing import List, Optional

# Rules whose violation is measured as "actual must stay at or below the value".
# Tightening one means lowering it. LOWER-bound rules are the mirror image.
UPPER_BOUND = "UPPER"
LOWER_BOUND = "LOWER"

# Share of the followed trades a proposed threshold should still admit. Setting
# the bar at the extreme of past behaviour would outlaw trades the user got
# right, so the proposal keeps most of them and trims the tail.
FOLLOWED_COVERAGE = 0.75
MINIMUM_FOLLOWED_SAMPLES = 2


def _numeric(
    target_rule: str,
    *,
    bound: str,
    minimum: float,
    maximum: float,
    step: float,
    unit: str,
    principle_type: str,
    title: str,
    label: str,
    integer: bool = False,
) -> dict:
    return {
        "targetRule": target_rule,
        "kind": "NUMERIC",
        "bound": bound,
        "strengthDirection": "DECREASE" if bound == UPPER_BOUND else "INCREASE",
        "allowedMinimum": minimum,
        "allowedMaximum": maximum,
        "step": step,
        "unit": unit,
        "principleType": principle_type,
        "title": title,
        "label": label,
        "integer": integer,
    }


def _boolean(target_rule: str, *, strict_value: bool, principle_type: str, title: str, label: str) -> dict:
    return {
        "targetRule": target_rule,
        "kind": "BOOLEAN",
        "strictValue": strict_value,
        "strengthDirection": "INCREASE" if strict_value else "DECREASE",
        "allowedMinimum": 0,
        "allowedMaximum": 1,
        "principleType": principle_type,
        "title": title,
        "label": label,
    }


def _enum(target_rule: str, *, ladder: List[str], principle_type: str, title: str, label: str) -> dict:
    return {
        "targetRule": target_rule,
        "kind": "ENUM",
        "ladder": ladder,
        "strengthDirection": "INCREASE",
        "allowedMinimum": 0,
        "allowedMaximum": len(ladder) - 1,
        "principleType": principle_type,
        "title": title,
        "label": label,
    }


def _enforcement(target_rule: str, *, principle_type: str, title: str, label: str, action: str) -> dict:
    """A rule with no single number to tighten; only execution can be reinforced."""
    return {
        "targetRule": target_rule,
        "kind": "NOT_TUNABLE",
        "strengthDirection": "NONE",
        "allowedMinimum": 0,
        "allowedMaximum": 0,
        "principleType": principle_type,
        "title": title,
        "label": label,
        "action": action,
    }


RULE_STRENGTHEN_SPEC = {
    # --- universe -------------------------------------------------------
    "universe.min_market_cap": _numeric(
        "universe.min_market_cap", bound=LOWER_BOUND,
        minimum=10_000_000_000.0, maximum=1_000_000_000_000.0, step=0.25, unit="KRW",
        principle_type="UNIVERSE_DISCIPLINE", title="최소 시가총액 기준 강화", label="최소 시가총액",
    ),
    "universe.min_daily_trading_value": _numeric(
        "universe.min_daily_trading_value", bound=LOWER_BOUND,
        minimum=100_000_000.0, maximum=100_000_000_000.0, step=0.25, unit="KRW",
        principle_type="UNIVERSE_DISCIPLINE", title="최소 거래대금 기준 강화", label="최소 일 거래대금",
    ),
    "universe.exclude_halted": _boolean(
        "universe.exclude_halted", strict_value=True,
        principle_type="UNIVERSE_DISCIPLINE", title="거래정지 종목 제외", label="거래정지 종목 제외",
    ),
    "universe.exclude_administrative": _boolean(
        "universe.exclude_administrative", strict_value=True,
        principle_type="UNIVERSE_DISCIPLINE", title="관리종목 제외", label="관리종목 제외",
    ),
    "universe.allowed_markets": _enforcement(
        "universe.allowed_markets",
        principle_type="UNIVERSE_DISCIPLINE", title="투자 시장 범위 확인",
        label="허용 시장",
        action="주문 전에 해당 종목이 원칙에서 허용한 시장에 속하는지 확인합니다.",
    ),
    # --- selection ------------------------------------------------------
    "selection.min_passing_score": _numeric(
        "selection.min_passing_score", bound=LOWER_BOUND,
        minimum=50.0, maximum=95.0, step=0.10, unit="SCORE",
        principle_type="SELECTION_DISCIPLINE", title="종목 평가 통과 점수 강화", label="최소 종목 평가 점수",
    ),
    "selection.factor_weights": _enforcement(
        "selection.factor_weights",
        principle_type="SELECTION_DISCIPLINE", title="종목 평가 기준 재확인",
        label="팩터 가중치",
        action="주문 전에 이 종목이 원칙에서 중요하게 보는 팩터를 충족하는지 확인합니다.",
    ),
    # --- entry ----------------------------------------------------------
    "entry.max_5day_return": _numeric(
        "entry.max_5day_return", bound=UPPER_BOUND,
        minimum=0.05, maximum=0.30, step=0.25, unit="RATE",
        principle_type="ENTRY_DISCIPLINE", title="급등 후 추격매수 제한", label="최근 5거래일 수익률 상한",
    ),
    "entry.require_positive_disclosure": _boolean(
        "entry.require_positive_disclosure", strict_value=True,
        principle_type="ENTRY_DISCIPLINE", title="긍정 공시 확인 후 매수", label="긍정 공시 확인",
    ),
    "entry.moving_average_condition": _enum(
        "entry.moving_average_condition",
        ladder=["NONE", "ABOVE_MA5", "ABOVE_MA20", "MA5_ABOVE_MA20"],
        principle_type="ENTRY_DISCIPLINE", title="이동평균 진입 조건 강화", label="이동평균 진입 조건",
    ),
    # --- additional buy -------------------------------------------------
    "additional_buy.allowed": _boolean(
        "additional_buy.allowed", strict_value=False,
        principle_type="ADDITIONAL_BUY_DISCIPLINE", title="추가매수 금지", label="추가매수 허용",
    ),
    "additional_buy.max_additional_count": _numeric(
        "additional_buy.max_additional_count", bound=UPPER_BOUND,
        minimum=0, maximum=5, step=0.34, unit="COUNT",
        principle_type="ADDITIONAL_BUY_DISCIPLINE", title="추가매수 횟수 제한", label="최대 추가매수 횟수",
        integer=True,
    ),
    "additional_buy.trigger_drop_rate": _numeric(
        "additional_buy.trigger_drop_rate", bound=UPPER_BOUND,
        minimum=-0.30, maximum=-0.02, step=0.25, unit="SIGNED_RATE",
        principle_type="ADDITIONAL_BUY_DISCIPLINE", title="추가매수 발동 하락률 강화", label="추가매수 발동 하락률",
    ),
    "additional_buy.additional_weight": _numeric(
        "additional_buy.additional_weight", bound=UPPER_BOUND,
        minimum=0.01, maximum=0.20, step=0.25, unit="RATE",
        principle_type="ADDITIONAL_BUY_DISCIPLINE", title="추가매수 비중 제한", label="추가매수 비중",
    ),
    # --- portfolio ------------------------------------------------------
    "portfolio.max_position_count": _numeric(
        "portfolio.max_position_count", bound=UPPER_BOUND,
        minimum=1, maximum=20, step=0.20, unit="COUNT",
        principle_type="POSITION_SIZING", title="보유 종목 수 제한", label="최대 보유 종목 수",
        integer=True,
    ),
    "portfolio.max_single_position_weight": _numeric(
        "portfolio.max_single_position_weight", bound=UPPER_BOUND,
        minimum=0.05, maximum=0.40, step=0.20, unit="RATE",
        principle_type="POSITION_SIZING", title="종목당 비중 상한 강화", label="종목당 최대 비중",
    ),
    "portfolio.max_sector_weight": _numeric(
        "portfolio.max_sector_weight", bound=UPPER_BOUND,
        minimum=0.10, maximum=0.70, step=0.20, unit="RATE",
        principle_type="POSITION_SIZING", title="업종 비중 상한 강화", label="업종 최대 비중",
    ),
    # --- exit -----------------------------------------------------------
    "exit.stop_loss_rate": _numeric(
        "exit.stop_loss_rate", bound=LOWER_BOUND,
        minimum=-0.30, maximum=-0.03, step=0.25, unit="SIGNED_RATE",
        principle_type="LOSS_CONTROL", title="손실 제한 기준 강화", label="손절 기준",
    ),
    "exit.take_profit_rate": _numeric(
        "exit.take_profit_rate", bound=UPPER_BOUND,
        minimum=0.05, maximum=0.50, step=0.20, unit="RATE",
        principle_type="PROFIT_TAKING", title="수익 실현 기준 강화", label="익절 기준",
    ),
    "exit.max_holding_days": _numeric(
        "exit.max_holding_days", bound=UPPER_BOUND,
        minimum=5, maximum=365, step=0.20, unit="DAYS",
        principle_type="HOLDING_DISCIPLINE", title="최대 보유 기간 단축", label="최대 보유 기간",
        integer=True,
    ),
    "exit.sell_on_negative_disclosure": _boolean(
        "exit.sell_on_negative_disclosure", strict_value=True,
        principle_type="LOSS_CONTROL", title="악재 공시 시 매도", label="악재 공시 매도",
    ),
    # --- rebalance ------------------------------------------------------
    "rebalance.period": _enforcement(
        "rebalance.period",
        principle_type="REBALANCE_DISCIPLINE", title="정기 점검 주기 준수", label="리밸런싱 주기",
        action="정해진 점검 주기마다 보유 비중을 실제로 다시 확인합니다.",
    ),
    "rebalance.min_holding_days_before_rebalance": _enforcement(
        "rebalance.min_holding_days_before_rebalance",
        principle_type="REBALANCE_DISCIPLINE", title="최소 보유 기간 준수", label="리밸런싱 전 최소 보유일",
        action="정기 점검 시 최소 보유 기간을 채운 종목만 조정합니다.",
    ),
}


def format_value(value, unit: str) -> str:
    """Render a rule value the way a person would read it."""
    if value is None:
        return "미설정"
    if unit == "BOOLEAN" or isinstance(value, bool):
        return "예" if value else "아니오"
    if unit == "ENUM":
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit in {"RATE", "SIGNED_RATE"}:
        return f"{number * 100:g}%"
    if unit == "KRW":
        if abs(number) >= 1_0000_0000_0000:
            return f"{number / 1_0000_0000_0000:g}조원"
        if abs(number) >= 1_0000_0000:
            return f"{number / 1_0000_0000:g}억원"
        if abs(number) >= 1_0000:
            return f"{number / 1_0000:g}만원"
        return f"{number:,.0f}원"
    if unit == "COUNT":
        return f"{number:g}개"
    if unit == "DAYS":
        return f"{number:g}일"
    if unit == "SCORE":
        return f"{number:g}점"
    return f"{number:g}"


def _percentile(sorted_values: List[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _clamp(value: float, spec: dict) -> float:
    return max(float(spec["allowedMinimum"]), min(float(spec["allowedMaximum"]), value))


def _is_stricter_or_equal(candidate: float, current: float, bound: str) -> bool:
    return candidate <= current if bound == UPPER_BOUND else candidate >= current


def _numeric_proposal(spec: dict, current_value, followed_values: List[float]) -> tuple[Optional[float], str]:
    """Propose a threshold from the band the user actually respected."""
    bound = spec["bound"]
    usable = sorted(float(value) for value in followed_values if value is not None)

    basis = "FIXED_STEP_FROM_CURRENT"
    candidate = None
    if len(usable) >= MINIMUM_FOLLOWED_SAMPLES:
        probability = FOLLOWED_COVERAGE if bound == UPPER_BOUND else 1.0 - FOLLOWED_COVERAGE
        candidate = _percentile(usable, probability)
        basis = "FOLLOWED_TRADE_DISTRIBUTION"

    if current_value is None:
        if candidate is None:
            return None, "NO_BASIS"
        return round(_clamp(candidate, spec), 6), basis

    current = float(current_value)
    if candidate is None or not _is_stricter_or_equal(candidate, current, bound):
        # No usable evidence, or the evidence would loosen the rule. Take a
        # bounded step in the stricter direction instead of inventing a number.
        magnitude = abs(current) * float(spec["step"])
        if magnitude == 0:
            magnitude = float(spec["step"])
        candidate = current - magnitude if bound == UPPER_BOUND else current + magnitude
        basis = "FIXED_STEP_FROM_CURRENT"

    proposed = _clamp(candidate, spec)
    if spec.get("integer"):
        proposed = float(int(proposed)) if bound == UPPER_BOUND else float(-(-proposed // 1))
        proposed = _clamp(proposed, spec)
    if not _is_stricter_or_equal(proposed, current, bound):
        return None, "ALREADY_AT_LIMIT"
    return round(proposed, 6), basis


# 마지막 글자의 받침에 따라 조사가 달라집니다. 값에는 숫자와 %가 섞여 오므로
# 읽는 소리를 기준으로 판단합니다. "15%"는 "십오 퍼센트", "5개"는 "오 개"입니다.
_DIGIT_FINAL = {"0": True, "1": True, "3": True, "6": True, "7": True, "8": True,
                "2": False, "4": False, "5": False, "9": False}


def _final_consonant(text: str):
    """(받침 있음, 받침이 ㄹ인가). 판단할 글자가 없으면 None."""
    for ch in reversed(str(text).strip()):
        if "가" <= ch <= "힣":
            code = (ord(ch) - 0xAC00) % 28
            return code != 0, code == 8
        if ch.isdigit():
            return _DIGIT_FINAL[ch], ch in ("1", "7", "8")
        if ch == "%":            # 퍼센트
            return False, False
        if ch.isalpha():
            return True, False
    return None


def _josa(word: str, with_final: str, without_final: str) -> str:
    state = _final_consonant(word)
    if state is None:
        return with_final
    return with_final if state[0] else without_final


def eul(word: str) -> str:
    """을 / 를"""
    return f"{word}{_josa(word, '을', '를')}"


def eun(word: str) -> str:
    """은 / 는"""
    return f"{word}{_josa(word, '은', '는')}"


def euro(word: str) -> str:
    """으로 / 로 — 받침이 ㄹ이면 '로'를 씁니다."""
    state = _final_consonant(word)
    if state is None or not state[0] or state[1]:
        return f"{word}로"
    return f"{word}으로"


def build_strengthen_proposal(
    target_rule: str,
    current_value,
    followed_values: Optional[List[float]] = None,
) -> Optional[dict]:
    """Return the tightening for one rule, or None when the rule is unknown.

    Every known rule returns something. When no number can move, the result is
    an enforcement reinforcement so the screen still has an action to offer
    instead of a verdict with no remedy.
    """
    spec = RULE_STRENGTHEN_SPEC.get(target_rule)
    if not spec:
        return None

    kind = spec["kind"]
    common = {
        "targetRule": target_rule,
        "principleType": spec["principleType"],
        "title": spec["title"],
        "label": spec["label"],
        "strengthDirection": spec["strengthDirection"],
        "allowedMinimum": spec["allowedMinimum"],
        "allowedMaximum": spec["allowedMaximum"],
        "currentValue": current_value,
    }

    if kind == "NUMERIC":
        proposed, basis = _numeric_proposal(spec, current_value, followed_values or [])
        unit = spec["unit"]
        if proposed is None or (current_value is not None and float(proposed) == float(current_value)):
            return {
                **common,
                "changeType": "ENFORCEMENT_REINFORCEMENT",
                "proposedValue": current_value,
                "valueBasis": basis,
                "description": (
                    f"{eun(spec['label'])} 이미 {euro(format_value(current_value, unit))} "
                    "더 조일 곳이 없습니다. 주문 전에 지키는지만 확인하세요."
                ),
            }
        # 화면에 한 줄로 들어가야 해서 짧게 씁니다. 다만 이 수치가 어디서
        # 나왔는지는 남깁니다 -- 표본이 적어 임의로 조인 값을 근거 있는 값처럼
        # 보이게 하면 안 됩니다.
        evidence_sentence = (
            "지킨 거래들이 머문 구간 기준입니다."
            if basis == "FOLLOWED_TRADE_DISTRIBUTION"
            else "표본이 적어 한 단계만 조였습니다."
        )
        return {
            **common,
            "changeType": "THRESHOLD_ADJUSTMENT",
            "proposedValue": proposed,
            "valueBasis": basis,
            "description": (
                f"{eul(spec['label'])} {format_value(current_value, unit)} → "
                f"{euro(format_value(proposed, unit))} 조입니다. {evidence_sentence}"
            ),
        }

    if kind == "BOOLEAN":
        strict_value = bool(spec["strictValue"])
        if bool(current_value) == strict_value:
            return {
                **common,
                "changeType": "ENFORCEMENT_REINFORCEMENT",
                "proposedValue": current_value,
                "valueBasis": "ALREADY_AT_LIMIT",
                "description": f"{eun(spec['label'])} 이미 켜져 있습니다. 주문 전에 확인하세요.",
            }
        return {
            **common,
            "changeType": "SWITCH_TO_STRICT",
            "proposedValue": strict_value,
            "valueBasis": "RULE_DEFINITION",
            "description": (
                f"{eul(spec['label'])} '{format_value(current_value, 'BOOLEAN')}' → "
                f"{euro(chr(39) + format_value(strict_value, 'BOOLEAN') + chr(39))} 바꿉니다."
            ),
        }

    if kind == "ENUM":
        ladder = list(spec["ladder"])
        current_text = str(current_value or ladder[0])
        index = ladder.index(current_text) if current_text in ladder else 0
        if index >= len(ladder) - 1:
            return {
                **common,
                "changeType": "ENFORCEMENT_REINFORCEMENT",
                "proposedValue": current_value,
                "valueBasis": "ALREADY_AT_LIMIT",
                "description": f"{eun(spec['label'])} 이미 가장 엄격한 단계입니다. 주문 전에 확인하세요.",
            }
        proposed = ladder[index + 1]
        return {
            **common,
            "changeType": "CONDITION_TIGHTENED",
            "proposedValue": proposed,
            "valueBasis": "RULE_LADDER",
            "description": (
                f"{eul(spec['label'])} '{current_text}' → "
                f"{euro(chr(39) + proposed + chr(39))} 한 단계 올립니다."
            ),
        }

    return {
        **common,
        "changeType": "ENFORCEMENT_REINFORCEMENT",
        "proposedValue": current_value,
        "valueBasis": "NOT_TUNABLE",
        "description": spec["action"],
    }
