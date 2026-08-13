"""
================================================================================
[Investory Engine Module] report_generator.py
================================================================================
■ 전체 기능 설명:
  - 백테스트 실행 데이터(실제 사용자 매매, 원칙 봇 매매, 일별 성과)를 종합 분석하여
    감정 복기(decisionReviews), 근거 검증(evidenceReviews), 학습 인사이트(learningInsights),
    추천 원칙(recommendedPrinciples), 개선 행동 조치(improvementActions) 리포트를 산출하는 모듈입니다.
================================================================================
"""

import json
import logging
import urllib.request
from typing import List, Optional

from app.modules.simulation.prompts import SYSTEM_REPORT_PROMPT, build_user_report_prompt
from app.modules.simulation.report_analysis import DeterministicReportAnalyzer
from app.config import settings

logger = logging.getLogger(__name__)

class SimulationReportGenerator:
    """시뮬레이션 백테스트 및 실제 매매 내역 기반 리포트 생성기"""

    REPORT_VERSION = "DETERMINISTIC_V10"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.REPORT_MODEL or "gpt-4o-mini"

    def generate_report(
        self,
        simulation_run_id: int,
        simulated_trades: List[dict],
        participant_summary: List[dict],
        daily_performance: Optional[List[dict]] = None,
        analytics: Optional[dict] = None,
    ) -> dict:
        """Build judgments locally and optionally enrich them with LLM-written prose."""
        report = self.build_deterministic_report(
            simulation_run_id=simulation_run_id,
            simulated_trades=simulated_trades,
            participant_summary=participant_summary,
            daily_performance=daily_performance,
            analytics=analytics,
        )
        return self.enrich_report(report)

    def build_deterministic_report(
        self,
        simulation_run_id: int,
        simulated_trades: List[dict],
        participant_summary: List[dict],
        daily_performance: Optional[List[dict]] = None,
        analytics: Optional[dict] = None,
    ) -> dict:
        """Build the complete numeric report without waiting for an external model."""
        report = DeterministicReportAnalyzer().build(
            simulated_trades,
            participant_summary,
            analytics or {},
        )
        report["generationMetadata"]["narrativeStatus"] = "PENDING"
        return report

    def enrich_report(self, report: dict) -> dict:
        """Add optional model-written prose to an already complete report."""
        try:
            narratives = self._call_llm_for_narratives(report)
            merged_count = self._merge_narratives(report, narratives)
            report["generationMetadata"]["narrativeSource"] = (
                "OPENAI" if merged_count else "TEMPLATE_FALLBACK"
            )
            proposal_items = (
                report.get("principleDiscoveries", [])
                + report.get("principleReinforcements", [])
            )
            report["generationMetadata"]["proposalSource"] = (
                "OPENAI_VALIDATED"
                if any(item.get("proposalSource") == "OPENAI_VALIDATED" for item in proposal_items)
                else "DETERMINISTIC_FALLBACK"
            )
            report["generationMetadata"]["narrativeStatus"] = "COMPLETED"
        except Exception as error:
            logger.warning(
                "Simulation report narrative generation failed; deterministic report retained (%s)",
                type(error).__name__,
            )
            report["generationMetadata"]["narrativeSource"] = "TEMPLATE_FALLBACK"
            report["generationMetadata"]["proposalSource"] = "DETERMINISTIC_FALLBACK"
            report["generationMetadata"]["narrativeStatus"] = "FAILED"
        return report

    def _call_llm_for_narratives(self, report: dict) -> Optional[dict]:
        """Ask OpenAI only for prose; all classifications and numbers are immutable."""
        narrative_input = {
            "reportVersion": report.get("reportVersion"),
            "decisionReviews": report.get("decisionReviews", [])[:20],
            "evidenceReviews": report.get("evidenceReviews", [])[:20],
            "learningInsights": report.get("learningInsights", {}),
            "recommendedPrinciples": report.get("recommendedPrinciples", []),
            "principleDiscoveries": report.get("principleDiscoveries", []),
            "principleReinforcements": report.get("principleReinforcements", []),
            "improvementActions": report.get("improvementActions", []),
        }
        prompt = build_user_report_prompt(narrative_input)

        openai_key = self.api_key
        if openai_key and not openai_key.startswith("your_") and len(openai_key) > 10:
            try:
                payload = json.dumps({
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_REPORT_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }).encode("utf-8")

                req = urllib.request.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {openai_key}"
                    }
                )

                with urllib.request.urlopen(req, timeout=settings.LLM_TIMEOUT) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    content = result["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    return parsed if isinstance(parsed, dict) else None
            except Exception as e:
                raise RuntimeError(
                    f"OpenAI narrative request failed: {type(e).__name__}"
                ) from e

        raise ValueError("유효한 OPENAI_API_KEY가 설정되지 않았습니다.")

    @staticmethod
    def _safe_text(value, maximum_length: int = 600) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        return text[:maximum_length]

    @classmethod
    def _merge_narratives(cls, report: dict, narratives: Optional[dict]) -> int:
        """Merge text-only fields. Numeric judgments and rule JSON are never accepted."""
        if not isinstance(narratives, dict):
            return 0
        merged = 0

        decision_text = {
            str(item.get("tradeId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("decisionNarratives", [])
            if isinstance(item, dict)
        }
        for item in report["decisionReviews"]:
            text = decision_text.get(str(item.get("tradeId")))
            if text:
                item["narrative"] = text
                merged += 1

        evidence_text = {
            str(item.get("tradeId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("evidenceNarratives", [])
            if isinstance(item, dict)
        }
        for item in report["evidenceReviews"]:
            text = evidence_text.get(str(item.get("tradeId")))
            if text:
                item["narrative"] = text
                merged += 1

        recommendation_text = {
            str(item.get("recommendationId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("recommendationNarratives", [])
            if isinstance(item, dict)
        }
        for item in report["recommendedPrinciples"]:
            text = recommendation_text.get(str(item.get("recommendationId")))
            if text:
                item["narrative"] = text
                merged += 1

        improvement_text = {
            str(item.get("category")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("improvementNarratives", [])
            if isinstance(item, dict)
        }
        for item in report["improvementActions"]:
            text = improvement_text.get(str(item.get("category")))
            if text:
                item["narrative"] = text
                merged += 1

        proposal_text = {
            str(item.get("opportunityId")): item
            for item in narratives.get("principleProposals", [])
            if isinstance(item, dict)
        }
        proposal_items = (
            report.get("principleDiscoveries", [])
            + report.get("principleReinforcements", [])
        )
        for item in proposal_items:
            proposal = proposal_text.get(str(item.get("opportunityId")))
            if not proposal:
                continue
            accepted = False
            title = cls._safe_text(proposal.get("title"), 100)
            description = cls._safe_text(proposal.get("description"), 600)
            if title:
                item["title"] = title
                accepted = True
            if description:
                item["description"] = description
                accepted = True

            value = proposal.get("proposedValue")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = round(float(value), 6)
                minimum = float(item["allowedMinimum"])
                maximum = float(item["allowedMaximum"])
                current = item.get("currentValue")
                direction = item.get("strengthDirection")
                strengthens = True
                if item.get("proposalType") == "REINFORCEMENT" and isinstance(current, (int, float)):
                    if direction == "DECREASE" and value > float(current):
                        strengthens = False
                    if direction == "INCREASE" and value < float(current):
                        strengthens = False
                if minimum <= value <= maximum and strengthens:
                    section, field = str(item["targetRule"]).split(".", 1)
                    item["proposedValue"] = value
                    item["ruleJson"] = {section: {field: value}}
                    item["changeType"] = (
                        "THRESHOLD_ADJUSTMENT"
                        if item.get("proposalType") == "REINFORCEMENT"
                        and item.get("currentValue") != value
                        else item["changeType"]
                    )
                    accepted = True
            if accepted:
                item["proposalSource"] = "OPENAI_VALIDATED"
                merged += 1
        return merged
