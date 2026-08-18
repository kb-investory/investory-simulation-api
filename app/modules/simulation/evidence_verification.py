"""Separated web-search and judgment agents for post-trade evidence verification."""

from __future__ import annotations

import json
import urllib.request
from typing import Optional


def _output_text(response_payload: dict) -> Optional[str]:
    if response_payload.get("output_text"):
        return response_payload["output_text"]
    return next(
        (
            content.get("text")
            for item in response_payload.get("output", [])
            for content in item.get("content", [])
            if content.get("type") == "output_text"
        ),
        None,
    )


class _ResponsesAgent:
    def __init__(self, api_key: str, model: str, timeout: int):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _request(self, payload: dict) -> dict:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
        parsed = json.loads(_output_text(raw) or "")
        if not isinstance(parsed, dict):
            raise ValueError("Evidence agent returned a non-object response")
        return parsed


class EvidenceSearchAgent(_ResponsesAgent):
    """Collect dated sources and facts without deciding whether a thesis was right."""

    def search(self, review: dict, checked_until: str) -> dict:
        prompt = f"""당신은 투자 기록의 근거 자료 검색 담당자다. 아래 근거를 검증 가능한 주장으로 나누고 웹에서 관련 자료를 찾는다.
판정하거나 투자 의견을 내리지 말고, 확인된 사실과 출처만 반환한다. 회사 공시·실적 발표·회사 IR을 우선하고 신뢰 가능한 보도를 보조로 사용한다.
거래일 이전에 공개된 자료와 거래일 이후 공개된 자료를 publishedAt으로 구분할 수 있게 실제 발행일을 기록한다. 존재하지 않는 URL을 만들지 않는다.

종목: {review.get('securityName')}
거래: {review.get('action')}, 거래일 {str(review.get('tradedAt') or '')[:10]}
사용자 근거: {review.get('decisionReason')}
검색 기준일: {checked_until}
"""
        schema = {
            "type": "json_schema",
            "name": "evidence_search_dossier",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claimEvidence"],
                "properties": {
                    "claimEvidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["claim", "evidence", "sources"],
                            "properties": {
                                "claim": {"type": "string"},
                                "evidence": {"type": "string"},
                                "sources": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["title", "publisher", "publishedAt", "url"],
                                        "properties": {
                                            "title": {"type": "string"},
                                            "publisher": {"type": "string"},
                                            "publishedAt": {"type": ["string", "null"]},
                                            "url": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    }
                },
            },
        }
        result = self._request({
            "model": self.model,
            "tools": [{"type": "web_search"}],
            "input": prompt,
            "text": {"format": schema},
        })
        result["searchedUntil"] = checked_until
        result["searchSource"] = "OPENAI_WEB_SEARCH"
        return result


class EvidenceJudgmentAgent(_ResponsesAgent):
    """Judge a searched dossier without seeing or using post-trade price returns."""

    def judge(self, review: dict, dossier: dict, checked_until: str) -> dict:
        prompt = f"""당신은 투자 근거 사후 판정 담당자다. 검색 담당자가 수집한 자료만 사용해 사용자 근거가 실제로 확인 또는 실현됐는지 판정한다.
주가 수익률은 입력에도 없으며 판정에 사용하지 않는다. 자료가 부족하면 UNCONFIRMED로 판정한다.

종목: {review.get('securityName')}
거래: {review.get('action')}, 거래일 {str(review.get('tradedAt') or '')[:10]}
사용자 근거: {review.get('decisionReason')}
검색 자료: {json.dumps(dossier, ensure_ascii=False)}
"""
        schema = {
            "type": "json_schema",
            "name": "evidence_judgment",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["verdict", "verdictLabel", "summary", "claimResults"],
                "properties": {
                    "verdict": {"type": "string", "enum": ["REALIZED", "PARTIALLY_REALIZED", "NOT_REALIZED", "UNCONFIRMED"]},
                    "verdictLabel": {"type": "string"},
                    "summary": {"type": "string"},
                    "claimResults": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["claim", "status", "evidence", "sources"],
                            "properties": {
                                "claim": {"type": "string"},
                                "status": {"type": "string", "enum": ["REALIZED", "PARTIALLY_REALIZED", "NOT_REALIZED", "UNCONFIRMED"]},
                                "evidence": {"type": "string"},
                                "sources": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["title", "publisher", "publishedAt", "url"],
                                        "properties": {
                                            "title": {"type": "string"},
                                            "publisher": {"type": "string"},
                                            "publishedAt": {"type": ["string", "null"]},
                                            "url": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        parsed = self._request({
            "model": self.model,
            "input": prompt,
            "text": {"format": schema},
        })
        claims = parsed.get("claimResults") if isinstance(parsed.get("claimResults"), list) else []
        source_count = sum(len(item.get("sources") or []) for item in claims if isinstance(item, dict))
        return {
            "verdict": parsed["verdict"],
            "verdictLabel": str(parsed.get("verdictLabel") or parsed["verdict"]),
            "summary": str(parsed.get("summary") or ""),
            "checkedUntil": checked_until,
            "claimResults": claims,
            "sourceCount": source_count,
            "verificationStatus": "COMPLETED",
            "searchSource": dossier.get("searchSource", "OPENAI_WEB_SEARCH"),
            "judgmentSource": "OPENAI_EVIDENCE_JUDGMENT",
        }
