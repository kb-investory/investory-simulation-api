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
from datetime import date
from typing import List, Optional

from app.modules.simulation.prompts import SYSTEM_REPORT_PROMPT, build_user_report_prompt
from app.modules.simulation.report_analysis import DeterministicReportAnalyzer
from app.modules.simulation.evidence_verification import EvidenceJudgmentAgent, EvidenceSearchAgent
from app.config import settings

logger = logging.getLogger(__name__)

class SimulationReportGenerator:
    """시뮬레이션 백테스트 및 실제 매매 내역 기반 리포트 생성기"""

    REPORT_VERSION = "DETERMINISTIC_V12"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.REPORT_MODEL or "gpt-4o-mini"
        self.thesis_model = settings.THESIS_VERIFICATION_MODEL or "gpt-5.4-nano"

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
        self._enrich_thesis_outcomes(report)
        return report

    def _enrich_thesis_outcomes(self, report: dict) -> None:
        """Verify whether a user's stated investment thesis later materialized.

        This intentionally runs only for the three selected key trades and is
        best-effort: an unavailable key/search never prevents report delivery.
        """
        reviews = report.get("keyTradeReviews", [])[:3]
        if not reviews:
            return
        for review in reviews:
            review["thesisOutcome"] = self._unconfirmed_thesis_outcome(
                "WEB_SEARCH_NOT_RUN",
                "웹 검색 검증을 아직 실행하지 못했습니다.",
            )
        self._sync_evidence_verification(report)
        if not self.api_key or self.api_key.startswith("your_") or len(self.api_key) <= 10:
            report["generationMetadata"]["thesisVerificationStatus"] = "NOT_CONFIGURED"
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
        report["generationMetadata"]["thesisVerificationStatus"] = (
            "COMPLETED" if completed == len(reviews) else "PARTIAL" if completed else "FAILED"
        )
        report["generationMetadata"]["thesisVerificationSource"] = "OPENAI_WEB_SEARCH" if completed else "NONE"
        if completed:
            self._sync_evidence_verification(report)
            self._apply_thesis_learning_and_principles(report)

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

        for security in report.get("securityEvidenceReviews", []):
            annotations = security.setdefault("chartAnnotations", [])
            seen = {
                (item.get("date"), item.get("type"), item.get("tradeId"), item.get("sourceUrl"))
                for item in annotations
            }
            for evidence in security.get("evidenceReviews", []):
                outcome = outcomes_by_trade.get(evidence.get("tradeId")) or {}
                for claim in outcome.get("claimResults") or []:
                    for source in claim.get("sources") or []:
                        published_at = str(source.get("publishedAt") or "")[:10]
                        if not published_at:
                            continue
                        marker = {
                            "date": published_at,
                            "type": "EVIDENCE_EVENT",
                            "tradeId": evidence.get("tradeId"),
                            "label": str(source.get("title") or claim.get("claim") or "근거 자료"),
                            "sourceUrl": source.get("url"),
                        }
                        key = (marker["date"], marker["type"], marker["tradeId"], marker["sourceUrl"])
                        if key not in seen:
                            annotations.append(marker)
                            seen.add(key)
            annotations.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("tradeId") or "")))

    @staticmethod
    def _apply_thesis_learning_and_principles(report: dict) -> None:
        """Feed completed web-verification judgments into insights and proposals."""
        outcomes = [
            item.get("thesisOutcome", {})
            for item in report.get("keyTradeReviews", [])[:3]
            if item.get("thesisOutcome", {}).get("verificationStatus") == "COMPLETED"
        ]
        realized = sum(item.get("verdict") == "REALIZED" for item in outcomes)
        partial = sum(item.get("verdict") == "PARTIALLY_REALIZED" for item in outcomes)
        not_realized = sum(item.get("verdict") == "NOT_REALIZED" for item in outcomes)
        total = len(outcomes)
        if not total:
            return
        insight = report.setdefault("learningInsights", {})
        insight["thesisOutcomeSummary"] = {
            "assessedTradeCount": total,
            "realizedTradeCount": realized,
            "partiallyRealizedTradeCount": partial,
            "notRealizedTradeCount": not_realized,
            "source": "OPENAI_WEB_SEARCH",
        }
        thesis_text = (
            f"핵심 거래 {total}건의 투자 근거를 사후 검증한 결과, "
            f"실현 {realized}건·일부 실현 {partial}건·미실현 {not_realized}건입니다."
        )
        insight["thesisNarrative"] = thesis_text
        insight["narrative"] = f"{insight.get('narrative', '')} {thesis_text}".strip()
        if not_realized + partial < 2:
            return
        proposal = {
            "recommendationId": 3001,
            "opportunityId": "THESIS_VALIDATION:audit.pre_trade_thesis_validation",
            "recommendationCode": "THESIS_VALIDATION",
            "proposalType": "DISCOVERY",
            "principleType": "EVIDENCE_DISCIPLINE",
            "title": "매수 전 투자 근거와 확인 시점 기록",
            "description": "매수·매도 전 핵심 근거와 그 근거가 확인될 공시·실적·이벤트 시점을 기록하고, 사후에 실현 여부를 점검한다.",
            "targetRule": "audit.pre_trade_thesis_validation",
            "currentValue": None,
            "sourcePrincipleText": None,
            "proposedValue": True,
            "allowedMinimum": True,
            "allowedMaximum": True,
            "strengthDirection": "ENABLE",
            "changeType": "NEW_RULE",
            "ruleJson": {"audit": {"pre_trade_thesis_validation": True}},
            "evidence": {"assessedTradeCount": total, "partiallyRealizedTradeCount": partial, "notRealizedTradeCount": not_realized},
            "judgmentSource": "OPENAI_WEB_SEARCH",
            "proposalSource": "OPENAI_WEB_SEARCH",
        }
        discoveries = report.setdefault("principleDiscoveries", [])
        if not any(item.get("recommendationCode") == "THESIS_VALIDATION" for item in discoveries):
            discoveries.append(proposal)
            report["recommendedPrinciples"] = discoveries + report.get("principleReinforcements", [])
        actions = report.setdefault("improvementActions", [])
        if not any(item.get("category") == "EVIDENCE_DISCIPLINE" for item in actions):
            actions.append({
                "category": "EVIDENCE_DISCIPLINE",
                "title": "투자 근거 사후 점검",
                "action": "각 매매 근거에 확인 시점을 기록하고, 결과 발표 후 근거가 실제로 실현됐는지 점검합니다.",
                "judgmentSource": "OPENAI_WEB_SEARCH",
            })

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
        search_agent = EvidenceSearchAgent(self.api_key, self.thesis_model, settings.LLM_TIMEOUT)
        judgment_agent = EvidenceJudgmentAgent(self.api_key, self.thesis_model, settings.LLM_TIMEOUT)
        dossier = search_agent.search(review, checked_until)
        return judgment_agent.judge(review, dossier, checked_until)

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
