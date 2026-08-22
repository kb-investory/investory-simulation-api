"""
================================================================================
[Investory Engine Module] report_generator.py
================================================================================
■ 전체 기능 설명:
  - 백테스트 실행 데이터(실제 사용자 매매, 원칙 봇 매매, 일별 성과)를 종합 분석하여
    1위가 누구냐에 따라 갈리는 결과(outcome)와 그 갈래가 근거로 드는 대표 거래
    (decisionReviews / evidenceReviews), 기존 원칙 평가(principleEvaluations)를
    산출하는 모듈입니다.
================================================================================
"""

import logging
from datetime import date
from typing import List, Optional

from app.modules.simulation.llm_client import call_openai_chat_json
from app.modules.simulation.prompts import SYSTEM_REPORT_PROMPT, build_user_report_prompt
from app.modules.simulation.analytics.report_analysis import (
    REPORT_IDENTITY,
    DeterministicReportAnalyzer,
    _is_verifiable,
)
from app.modules.simulation.analytics.evidence_verification import EvidenceJudgmentAgent, EvidenceSearchAgent
from app.config import settings

logger = logging.getLogger(__name__)

class SimulationReportGenerator:
    """시뮬레이션 백테스트 및 실제 매매 내역 기반 리포트 생성기"""

    REPORT_VERSION = REPORT_IDENTITY

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.REPORT_MODEL or "gpt-4o-mini"
        self.search_model = settings.EVIDENCE_SEARCH_MODEL
        self.judgment_model = settings.EVIDENCE_JUDGMENT_MODEL

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
            proposal_items = self._proposals(report)
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
        self._enrich_thesis_outcomes(report)
        return report

    @staticmethod
    def _proposals(report: dict) -> List[dict]:
        """Every strengthening proposal the report carries, in one place."""
        return [
            item["suggestion"]
            for item in report.get("principleEvaluations", [])
            if isinstance(item.get("suggestion"), dict)
        ]

    def _enrich_thesis_outcomes(self, report: dict) -> None:
        """Verify whether a user's stated investment thesis later materialized.

        Only trades that recorded a reason can be checked, so only those receive
        a thesisOutcome. A trade without one is left without the field entirely
        rather than carrying an empty verdict that reads like a failed check --
        the verification UI can then select on its presence alone.

        This runs for the selected key trades only and is best-effort: an
        unavailable key or search never prevents report delivery.
        """
        metadata = report.setdefault("generationMetadata", {})
        reviews = [
            review for review in report.get("keyTradeReviews", [])[:3]
            if _is_verifiable(review)
        ]
        metadata["thesisVerificationTargetCount"] = len(reviews)
        metadata["thesisVerificationCompletedCount"] = 0
        if not reviews:
            metadata["thesisVerificationStatus"] = "NOT_APPLICABLE"
            metadata["thesisVerificationSource"] = "NONE"
            return
        for review in reviews:
            review["thesisOutcome"] = self._unconfirmed_thesis_outcome(
                "WEB_SEARCH_NOT_RUN",
                "웹 검색 검증을 아직 실행하지 못했습니다.",
            )
        self._sync_evidence_verification(report)
        if not self.api_key or self.api_key.startswith("your_") or len(self.api_key) <= 10:
            metadata["thesisVerificationStatus"] = "NOT_CONFIGURED"
            metadata["thesisVerificationSource"] = "NONE"
            return
        completed = 0
        for review in reviews:
            try:
                outcome = self._call_web_thesis_verifier(review)
                if outcome:
                    review["thesisOutcome"] = outcome
                    completed += 1
            except Exception as error:
                logger.warning("Thesis verification failed for trade %s (%s)", review.get("tradeId"), type(error).__name__)
        metadata["thesisVerificationCompletedCount"] = completed
        metadata["thesisVerificationStatus"] = (
            "COMPLETED" if completed == len(reviews) else "PARTIAL" if completed else "FAILED"
        )
        metadata["thesisVerificationSource"] = "OPENAI_WEB_SEARCH" if completed else "NONE"
        if completed:
            self._sync_evidence_verification(report)

    @staticmethod
    def _sync_evidence_verification(report: dict) -> None:
        verdict_map = {
            "REALIZED": "CONFIRMED",
            "PARTIALLY_REALIZED": "PARTIAL",
            "NOT_REALIZED": "CONTRADICTED",
            "UNCONFIRMED": "UNCONFIRMED",
        }
        outcomes_by_trade = {
            item.get("tradeId"): item.get("thesisOutcome", {})
            for item in report.get("keyTradeReviews", [])
        }
        for evidence in report.get("evidenceReviews", []):
            outcome = outcomes_by_trade.get(evidence.get("tradeId"))
            if not outcome:
                continue
            evidence["webVerdict"] = verdict_map.get(outcome.get("verdict"), "UNCONFIRMED")
            evidence["webVerification"] = outcome

    @staticmethod
    def _unconfirmed_thesis_outcome(status: str, summary: str) -> dict:
        return {
            "verdict": "UNCONFIRMED",
            "verdictLabel": "확인 불가",
            "summary": summary,
            "checkedUntil": date.today().isoformat(),
            "claimResults": [],
            "sourceCount": 0,
            "verificationStatus": status,
        }

    def _call_web_thesis_verifier(self, review: dict) -> Optional[dict]:
        rationale = str(review.get("decisionReason") or "").strip()
        if not rationale or rationale == "사용자가 입력한 매매 근거 없음":
            return self._unconfirmed_thesis_outcome("NO_RECORDED_RATIONALE", "기록된 매매 근거가 없어 검증할 수 없습니다.")
        checked_until = date.today().isoformat()
        # Retrieval runs on the fast tier; the verdict runs on the reasoning
        # tier, which needs both the longer wall clock and the token budget.
        search_agent = EvidenceSearchAgent(
            self.api_key,
            self.search_model,
            settings.LLM_TIMEOUT,
        )
        judgment_agent = EvidenceJudgmentAgent(
            self.api_key,
            self.judgment_model,
            settings.REASONING_LLM_TIMEOUT,
            settings.REASONING_MAX_OUTPUT_TOKENS,
        )
        dossier = search_agent.search(review, checked_until)
        return judgment_agent.judge(review, dossier, checked_until)

    def _call_llm_for_narratives(self, report: dict) -> Optional[dict]:
        """Ask OpenAI only for prose; all classifications and numbers are immutable."""
        narrative_input = {
            "reportVersion": report.get("reportVersion"),
            "decisionReviews": report.get("decisionReviews", []),
            "evidenceReviews": report.get("evidenceReviews", []),
            "principleEvaluationSummary": report.get("principleEvaluationSummary", {}),
            "principleEvaluations": report.get("principleEvaluations", []),
        }
        prompt = build_user_report_prompt(narrative_input)

        openai_key = self.api_key
        if openai_key and not openai_key.startswith("your_") and len(openai_key) > 10:
            try:
                parsed = call_openai_chat_json(
                    api_key=openai_key,
                    model=self.model,
                    system_prompt=SYSTEM_REPORT_PROMPT,
                    user_prompt=prompt,
                    response_format={"type": "json_object"},
                    timeout=settings.LLM_TIMEOUT,
                )
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
        for item in report.get("decisionReviews", []):
            text = decision_text.get(str(item.get("tradeId")))
            if text:
                item["narrative"] = text
                merged += 1

        evidence_text = {
            str(item.get("tradeId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("evidenceNarratives", [])
            if isinstance(item, dict)
        }
        for item in report.get("evidenceReviews", []):
            text = evidence_text.get(str(item.get("tradeId")))
            if text:
                item["narrative"] = text
                merged += 1

        evaluation_text = {
            str(item.get("evaluationId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("principleEvaluationNarratives", [])
            if isinstance(item, dict)
        }
        for item in report.get("principleEvaluations", []):
            text = evaluation_text.get(str(item.get("evaluationId")))
            if text:
                item["narrative"] = text
                merged += 1

        recommendation_text = {
            str(item.get("recommendationId")): cls._safe_text(item.get("explanation"))
            for item in narratives.get("recommendationNarratives", [])
            if isinstance(item, dict)
        }
        for item in cls._proposals(report):
            text = recommendation_text.get(str(item.get("recommendationId")))
            if text:
                item["narrative"] = text
                merged += 1

        proposal_text = {
            str(item.get("opportunityId")): item
            for item in narratives.get("principleProposals", [])
            if isinstance(item, dict)
        }
        for item in cls._proposals(report):
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
