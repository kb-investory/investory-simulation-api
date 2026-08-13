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
from app.modules.simulation.rule_schema import InvestmentBotStrategySchema


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
            "required": ["ai_confidence", "interpreted_principles", "needs_user_confirmation"],
            "properties": {
                "ai_confidence": {"type": "number"},
                "interpreted_principles": {
                    "type": "array",
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["user_natural_text", "ai_mapped_rule", "status"],
                        "properties": {
                            "user_natural_text": {"type": "string"},
                            "ai_mapped_rule": {"type": "string"},
                            "status": {"type": "string"},
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
            with urlopen(request, timeout=settings.LLM_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            data = json.loads(content)
            self._validate_generated_data(data)
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
