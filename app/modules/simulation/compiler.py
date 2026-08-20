"""LLM-only compiler for persisted investor profiles and investment principles."""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import settings
from app.modules.simulation.prompts import SYSTEM_COMPILER_PROMPT, build_user_compiler_prompt
from app.modules.simulation.rule_schema import (
    InvestmentBotStrategySchema,
    executable_rule_paths,
    numeric_rule_paths,
)


class RuleCompilationError(RuntimeError):
    """Raised when a strategy could not be authored and validated by the LLM."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


RULE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["universe", "selection", "entry", "additional_buy", "portfolio", "exit", "rebalance", "audit"],
    "properties": {
        "universe": {
            "type": "object", "additionalProperties": False,
            "required": ["allowed_markets", "min_market_cap", "min_daily_trading_value", "exclude_halted", "exclude_administrative"],
            "properties": {
                "allowed_markets": {"type": "array", "items": {"type": "string"}},
                "min_market_cap": {"type": "number"},
                "min_daily_trading_value": {"type": "number"},
                "exclude_halted": {"type": "boolean"},
                "exclude_administrative": {"type": "boolean"},
            },
        },
        "selection": {
            "type": "object", "additionalProperties": False,
            "required": ["factor_weights", "min_passing_score"],
            "properties": {
                "factor_weights": {
                    "type": "object", "additionalProperties": False,
                    "required": ["value", "growth", "quality", "trend", "disclosure"],
                    "properties": {name: {"type": "number"} for name in ["value", "growth", "quality", "trend", "disclosure"]},
                },
                "min_passing_score": {"type": "number"},
            },
        },
        "entry": {
            "type": "object", "additionalProperties": False,
            "required": ["max_5day_return", "moving_average_condition", "require_positive_disclosure"],
            "properties": {
                "max_5day_return": {"type": "number"},
                "moving_average_condition": {"type": "string"},
                "require_positive_disclosure": {"type": "boolean"},
            },
        },
        "additional_buy": {
            "type": "object", "additionalProperties": False,
            "required": ["allowed", "max_additional_count", "trigger_drop_rate", "additional_weight"],
            "properties": {
                "allowed": {"type": "boolean"},
                "max_additional_count": {"type": "integer"},
                "trigger_drop_rate": {"type": "number"},
                "additional_weight": {"type": "number"},
            },
        },
        "portfolio": {
            "type": "object", "additionalProperties": False,
            "required": ["max_position_count", "max_single_position_weight", "max_sector_weight"],
            "properties": {
                "max_position_count": {"type": "integer"},
                "max_single_position_weight": {"type": "number"},
                "max_sector_weight": {"type": "number"},
            },
        },
        "exit": {
            "type": "object", "additionalProperties": False,
            "required": ["take_profit_rate", "stop_loss_rate", "max_holding_days", "sell_on_negative_disclosure"],
            "properties": {
                "take_profit_rate": {"type": "number"},
                "stop_loss_rate": {"type": "number"},
                "max_holding_days": {"type": "integer"},
                "sell_on_negative_disclosure": {"type": "boolean"},
            },
        },
        "rebalance": {
            "type": "object", "additionalProperties": False,
            "required": ["period", "min_holding_days_before_rebalance"],
            "properties": {
                "period": {"type": "string"},
                "min_holding_days_before_rebalance": {"type": "integer"},
            },
        },
        "audit": {
            "type": "object", "additionalProperties": False,
            "required": [
                "ai_confidence",
                "interpreted_principles",
                "needs_user_confirmation",
                "principle_conflicts",
            ],
            "properties": {
                "ai_confidence": {"type": "number"},
                "interpreted_principles": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "user_natural_text",
                            "ai_mapped_rules",
                            "stated_rules",
                            "status",
                            "unmappable_reason",
                        ],
                        "properties": {
                            "user_natural_text": {"type": "string"},
                            # One sentence often carries several conditions.
                            # Forcing it onto a single rule left the rest of the
                            # sentence unenforced.
                            "ai_mapped_rules": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            # 사용자가 문장에 기준을 직접 쓴 규칙. 문장에서 숫자를
                            # 읽어낸 것은 추정이 아니라 파싱이므로, 그 값으로는
                            # 확인 없이 평가하고 강화안도 제안할 수 있습니다.
                            "stated_rules": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "status": {"type": "string"},
                            # Empty when the principle did map. When it did not,
                            # this is the only thing standing between the user
                            # and a principle parked in REVIEW forever.
                            "unmappable_reason": {"type": "string"},
                        },
                    },
                },
                "needs_user_confirmation": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["field", "reason"],
                        "properties": {"field": {"type": "string"}, "reason": {"type": "string"}},
                    },
                },
                # Pairs that fight each other. Rule paths alone cannot find these
                # -- "average down" and "cut at -10%" live in different sections
                # and only collide once you read what they mean.
                "principle_conflicts": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": [
                            "first_principle_text",
                            "second_principle_text",
                            "conflict_type",
                            "reason",
                        ],
                        "properties": {
                            "first_principle_text": {"type": "string"},
                            "second_principle_text": {"type": "string"},
                            "conflict_type": {
                                "type": "string",
                                "enum": ["CONTRADICTION", "OVERLAP", "AMBIGUOUS_PRIORITY"],
                            },
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


class AIRuleCompiler:
    """Generate an executable rule schema using OpenAI; no local fallback exists."""

    COMPILER_VERSION = "RULE_COMPILER_V1"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.COMPILER_MODEL or "gpt-4o-mini"
        self.last_compilation_metadata: Dict[str, object] = {}

    def analyze_trade_history(self, actual_trades: List[dict]) -> dict:
        buys = [trade for trade in actual_trades if trade.get("tradeSide") == "BUY"]
        sells = [trade for trade in actual_trades if trade.get("tradeSide") == "SELL"]
        notionals = [float(trade.get("quantity", 0)) * float(trade.get("unitPrice", 0)) for trade in actual_trades]
        dates = sorted(str(trade.get("tradedAt", ""))[:10] for trade in actual_trades if trade.get("tradedAt"))
        return {
            "tradeCount": len(actual_trades),
            "buyCount": len(buys),
            "sellCount": len(sells),
            "securityCount": len({trade.get("securityId") for trade in actual_trades if trade.get("securityId") is not None}),
            "averageTradeNotional": round(sum(notionals) / len(notionals), 2) if notionals else 0.0,
            "totalTransactionCost": round(sum(float(trade.get("transactionCostAmount", 0)) for trade in actual_trades), 2),
            "periodStart": dates[0] if dates else None,
            "periodEnd": dates[-1] if dates else None,
        }

    def build_input_fingerprint(
        self,
        principles: List[str],
        profile: Dict[str, object],
        actual_trades: Optional[List[dict]] = None,
    ) -> str:
        """Hash every semantic input that can change the compiled rule schema."""
        canonical_input = {
            "compilerVersion": self.COMPILER_VERSION,
            "model": self.model,
            "systemPrompt": SYSTEM_COMPILER_PROMPT,
            "outputSchema": RULE_OUTPUT_SCHEMA,
            "principles": [str(principle).strip() for principle in principles],
            "profile": profile,
            "tradeStats": self.analyze_trade_history(actual_trades or []),
        }
        payload = json.dumps(
            canonical_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def compile(
        self,
        principles: List[str],
        profile: Dict[str, object],
        actual_trades: Optional[List[dict]] = None,
    ) -> InvestmentBotStrategySchema:
        openai_key = os.getenv("OPENAI_API_KEY") or self.api_key
        if not openai_key or openai_key.startswith("your_") or len(openai_key) <= 10:
            raise RuleCompilationError(
                "LLM_CONFIGURATION_REQUIRED",
                "투자봇 규칙 생성에는 유효한 OPENAI_API_KEY가 필요합니다.",
            )
        if not principles:
            raise RuleCompilationError("PRINCIPLES_REQUIRED", "LLM에 전달할 활성 투자 원칙이 없습니다.")
        if len(profile.get("axes", {})) != 6:
            raise RuleCompilationError("INVESTOR_PROFILE_INCOMPLETE", "LLM에 전달할 6축 투자 성향이 완전하지 않습니다.")

        trade_stats = self.analyze_trade_history(actual_trades or [])
        prompt = build_user_compiler_prompt(principles, profile, trade_stats)
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_COMPILER_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "investment_bot_strategy",
                        "strict": True,
                        "schema": RULE_OUTPUT_SCHEMA,
                    },
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {openai_key}"},
        )
        try:
            # Rule compilation runs on the reasoning tier behind an async job,
            # so it waits on the longer budget rather than the interactive one.
            with urlopen(request, timeout=settings.REASONING_LLM_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            data = json.loads(content)
            self._validate_generated_data(data)
            self._normalize_mapped_rules(data)
            self._drop_unanchored_conflicts(data, principles)
            schema = InvestmentBotStrategySchema.from_dict(data)
        except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuleCompilationError(
                "LLM_RULE_COMPILATION_FAILED",
                f"OpenAI 규칙 생성 또는 응답 검증에 실패했습니다: {type(error).__name__}",
            ) from error

        self.last_compilation_metadata = {
            "source": "OPENAI",
            "model": self.model,
            "compilerVersion": self.COMPILER_VERSION,
            "profileAnalysisRunId": profile.get("analysisRunId"),
            "compiledAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "fallbackUsed": False,
        }
        return schema

    @staticmethod
    def _normalize_mapped_rules(data: dict) -> None:
        """Demote any mapping that is not a real rule path.

        The model sometimes answers with a description instead of the dotted
        path. Left alone it stays CONFIRMED, and every later trade check quietly
        reports "unsupported rule path" while the screen claims the principle is
        executable. Force those to REVIEW_REQUIRED so the failure is visible.
        """
        audit = data.get("audit")
        if not isinstance(audit, dict):
            return
        valid_paths = set(executable_rule_paths())
        for item in audit.get("interpreted_principles") or []:
            if not isinstance(item, dict):
                continue
            proposed = item.get("ai_mapped_rules")
            if isinstance(proposed, str):
                proposed = [proposed]
            elif not isinstance(proposed, list):
                proposed = []
            if item.get("ai_mapped_rule"):
                proposed = [item["ai_mapped_rule"], *proposed]

            kept, dropped = [], False
            for candidate in proposed:
                path = str(candidate or "").strip()
                if not path:
                    continue
                if path in valid_paths:
                    if path not in kept:
                        kept.append(path)
                else:
                    dropped = True

            item["ai_mapped_rules"] = kept
            item["ai_mapped_rule"] = kept[0] if kept else ""
            item["stated_rules"] = AIRuleCompiler._verified_stated_rules(item, kept)
            if kept:
                continue
            item["status"] = "REVIEW_REQUIRED"
            if not str(item.get("unmappable_reason") or "").strip():
                item["unmappable_reason"] = (
                    "AI가 지정한 실행 규칙이 유효하지 않아 직접 검토가 필요합니다."
                    if dropped
                    else "이 원칙에 해당하는 실행 규칙을 찾지 못해 직접 검토가 필요합니다."
                )

    @staticmethod
    def _verified_stated_rules(item: dict, mapped: List[str]) -> List[str]:
        """Keep only the rules whose value the user really did write down.

        A value marked as stated skips the confirmation gate and can drive a
        violation verdict, so the claim is checked rather than trusted: it must
        be a rule this principle actually maps onto, and for a numeric rule the
        sentence has to contain a number. Boolean and enum rules are exempt
        because "물타기는 하지 않는다" states a value without any digits.
        """
        claimed = item.get("stated_rules")
        if isinstance(claimed, str):
            claimed = [claimed]
        elif not isinstance(claimed, list):
            claimed = []
        text = str(item.get("user_natural_text") or "")
        has_number = any(character.isdigit() for character in text)
        numeric = set(numeric_rule_paths())
        mapped_set = set(mapped)

        verified = []
        for candidate in claimed:
            path = str(candidate or "").strip()
            if path not in mapped_set or path in verified:
                continue
            if path in numeric and not has_number:
                continue
            verified.append(path)
        return verified

    @staticmethod
    def _drop_unanchored_conflicts(data: dict, principles: List[str]) -> None:
        """Keep only conflicts between two principles the user actually wrote.

        A conflict naming text the user never wrote, or pairing a principle with
        itself, is the model composing rather than observing. Those are dropped
        instead of being shown to the user as a finding about their own rules.
        """
        audit = data.get("audit")
        if not isinstance(audit, dict):
            return
        known = {str(principle).strip() for principle in principles}
        kept = []
        for item in audit.get("principle_conflicts") or []:
            if not isinstance(item, dict):
                continue
            first = str(item.get("first_principle_text") or "").strip()
            second = str(item.get("second_principle_text") or "").strip()
            if first in known and second in known and first != second:
                kept.append({
                    "first_principle_text": first,
                    "second_principle_text": second,
                    "conflict_type": str(item.get("conflict_type") or "CONTRADICTION"),
                    "reason": str(item.get("reason") or ""),
                })
        audit["principle_conflicts"] = kept

    @staticmethod
    def _validate_generated_data(data: dict) -> None:
        expected_sections = {"universe", "selection", "entry", "additional_buy", "portfolio", "exit", "rebalance", "audit"}
        if set(data) != expected_sections:
            raise ValueError("LLM rule schema sections do not match the required contract")
        weights = data["selection"]["factor_weights"]
        if set(weights) != {"value", "growth", "quality", "trend", "disclosure"}:
            raise ValueError("LLM factor weights do not match the required contract")
        if any(float(value) < 0 or float(value) > 1 for value in weights.values()):
            raise ValueError("LLM factor weights must be between zero and one")
        if abs(sum(float(value) for value in weights.values()) - 1.0) > 0.01:
            raise ValueError("LLM factor weights must sum to one")
        if float(data["exit"]["stop_loss_rate"]) > 0 or float(data["exit"]["take_profit_rate"]) < 0:
            raise ValueError("LLM exit rates have invalid signs")
