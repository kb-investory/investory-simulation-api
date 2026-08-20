"""
================================================================================
[Investory Engine Module] dart_collector.py
================================================================================
■ 전체 기능 설명:
  - 금융감독원 OpenDART API 수집 규격 및 공시 이벤트 파서 클래스를 정의합니다.
  - 2단계 하이브리드(AI + Rule) 공시 영향 분석 및 중복 API 호출 방지 인메모리 캐싱, 서킷 브레이커를 내장합니다.
================================================================================
"""

import json
import io
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Optional
from app.modules.simulation.llm_client import call_openai_chat_json
from app.modules.simulation.prompts import SYSTEM_DISCLOSURE_PROMPT, build_user_disclosure_prompt
from app.config import settings


class DisclosureAnalysisError(RuntimeError):
    """Raised when an LLM-required disclosure analysis cannot be completed."""


class DartCollector:
    """OpenDART 전자공시 수집기 및 2단계 하이브리드(AI + Rule) 공시 영향 분석기 클래스"""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, max_llm_calls: int = 50):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.model = model or settings.DISCLOSURE_MODEL or "gpt-4o-mini"
        self.max_llm_calls = settings.MAX_LLM_CALLS_PER_RUN or max_llm_calls
        self._llm_call_count = 0
        self._cache: Dict[str, dict] = {}

    def _call_openai_llm(self, report_name: str, contract_ratio: float, content_summary: str, openai_key: str) -> Optional[dict]:
        """OpenAI GPT LLM API 호출하여 공시 호재/악재 및 impactScore 심층 분석"""
        if self._llm_call_count >= self.max_llm_calls:
            print(f"[DartCollector Guard] 최대 LLM 호출 수({self.max_llm_calls}회) 도달. 룰 엔진으로 안전 전환합니다.")
            return None

        self._llm_call_count += 1
        try:
            prompt = build_user_disclosure_prompt(report_name, contract_ratio, content_summary)
            data = call_openai_chat_json(
                api_key=openai_key,
                model=self.model,
                system_prompt=SYSTEM_DISCLOSURE_PROMPT,
                user_prompt=prompt,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "disclosure_impact",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["direction", "impactScore", "reason"],
                            "properties": {
                                "direction": {"type": "string", "enum": ["POSITIVE", "NEGATIVE", "NEUTRAL"]},
                                "impactScore": {"type": "number"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                },
                timeout=settings.LLM_TIMEOUT,
            )
            direction = data["direction"]
            impact_score = float(data["impactScore"])
            if direction not in {"POSITIVE", "NEGATIVE", "NEUTRAL"} or not 0 <= impact_score <= 100:
                raise ValueError("공시 LLM 응답 값이 허용 범위를 벗어났습니다.")
            return {
                "direction": direction,
                "impactScore": impact_score,
                "reason": f"[OpenAI GPT AI 심층분석] {data['reason']}"
            }
        except Exception as e:
            print(f"[DartCollector Warning] OpenAI GPT LLM 공시 분석 실패: {e}")

        return None

    def _call_gemini_llm(self, report_name: str, contract_ratio: float = 0.0, content_summary: str = "") -> Optional[dict]:
        """OpenAI GPT LLM API 호출하여 공시 호재/악재 및 impactScore 심층 분석 (Gemini 대체)"""
        openai_key = os.getenv("OPENAI_API_KEY") or self.api_key
        if not openai_key:
            return None
        return self._call_openai_llm(report_name, contract_ratio, content_summary, openai_key)

    def evaluate_disclosure_impact(
        self,
        report_name: str,
        contract_ratio: float = 0.0,
        content_summary: str = "",
        allow_llm: bool = True,
        require_llm: bool = False,
    ) -> dict:
        """2단계 하이브리드 공시 영향 분석 로직"""
        name = report_name.strip()
        cache_key = f"{name}_{contract_ratio:.2f}_{allow_llm}_{require_llm}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        if require_llm:
            openai_key = os.getenv("OPENAI_API_KEY") or self.api_key
            if not openai_key or openai_key.startswith("your_") or len(openai_key) <= 10:
                raise DisclosureAnalysisError("공시 LLM 분석에는 유효한 OPENAI_API_KEY가 필요합니다.")
            llm_result = self._call_openai_llm(name, contract_ratio, content_summary, openai_key)
            if not llm_result:
                raise DisclosureAnalysisError("공시 LLM 분석에 실패했으며 Rule fallback은 비활성화되어 있습니다.")
            self._cache[cache_key] = llm_result
            return llm_result

        if any(bad_word in name for bad_word in ["해지", "횡령", "배임", "영업정지", "부도", "상장폐지"]):
            res = {
                "direction": "NEGATIVE",
                "impactScore": 15.0,
                "reason": f"악재 공시 확인: '{name}' (종목 감점 및 즉시 매도 발동 대상)"
            }
            self._cache[cache_key] = res
            return res
        
        if any(good_word in name for good_word in ["체결", "수주", "실적발표", "무상증자", "자사주"]):
            base_score = 80.0
            if contract_ratio >= 0.30:
                base_score = 90.0
            res = {
                "direction": "POSITIVE",
                "impactScore": base_score,
                "reason": f"호재 공시 확인: '{name}' (종목 팩터 점수 +{base_score}점 가산)"
            }
            self._cache[cache_key] = res
            return res

        if allow_llm:
            openai_key = os.getenv("OPENAI_API_KEY") or (self.api_key if self.api_key and self.api_key.startswith("sk-") else None)
            if openai_key:
                llm_result = self._call_openai_llm(name, contract_ratio, content_summary, openai_key)
                if llm_result:
                    self._cache[cache_key] = llm_result
                    return llm_result

            llm_result = self._call_gemini_llm(name, contract_ratio, content_summary)
            if llm_result:
                self._cache[cache_key] = llm_result
                return llm_result

        res = {
            "direction": "NEUTRAL",
            "impactScore": 75.0,
            "reason": f"일반 경영공시: '{name}'"
        }
        self._cache[cache_key] = res
        return res

    def fetch_disclosures_by_date(self, disclosure_events_data: List[dict], target_date: str) -> Dict[int, dict]:
        """특정 날짜(target_date)에 발생한 종목별 공시 분석 정보 반환"""
        result = {}
        for event in disclosure_events_data:
            if event.get("eventDate") == target_date:
                sec_id = event["securityId"]
                analysis = self.evaluate_disclosure_impact(
                    report_name=event.get("reportName", ""),
                    contract_ratio=float(event.get("contractToRevenueRatio", 0.0)),
                    content_summary=event.get("contentSummary", "")
                )
                result[sec_id] = {
                    "eventId": event.get("eventId"),
                    "eventType": event.get("eventType"),
                    "reportName": event.get("reportName"),
                    "direction": analysis["direction"],
                    "impactScore": analysis["impactScore"],
                    "analysisReason": analysis["reason"]
                }
        return result

    def fetch_and_save_daily_dart_disclosures(self, target_date: Optional[str] = None) -> int:
        """
        [배치 크롤러] 금융감독원 OpenDART API에서 당일 공시 수집 후 AI 영향 분석을 거쳐 DB에 저장합니다.
        """
        import datetime
        if not target_date:
            target_date = datetime.date.today().strftime('%Y-%m-%d')

        bgn_de = target_date.replace('-', '')
        dart_key = settings.OPENDART_API_KEY
        if not dart_key or dart_key.startswith("your_"):
            raise ValueError("유효한 OPENDART_API_KEY가 설정되지 않았습니다.")

        url = f"https://opendart.fss.or.kr/api/list.json?crtfc_key={dart_key}&bgn_de={bgn_de}&end_de={bgn_de}&page_count=100"
        print(f"[OpenDART Crawler] Fetching disclosures for date: {target_date}...")

        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as res:
                data = json.loads(res.read().decode('utf-8'))
                fetched_count = self._save_disclosure_items(data.get("list", []), allow_llm=True)
        except Exception as e:
            print(f"[OpenDART Crawler Warning] OpenDART 수집 연동 fallback 처리 (상세: {e})")
            fetched_count = 0

        print(f"[OpenDART Crawler Complete] 총 {fetched_count}건의 당일 공시 수집 및 DB 반영 완료.")
        return fetched_count

    def _dart_key(self) -> str:
        dart_key = settings.OPENDART_API_KEY
        if not dart_key or dart_key.startswith("your_"):
            raise ValueError("유효한 OPENDART_API_KEY가 설정되지 않았습니다.")
        return dart_key

    def load_corp_code_map(self) -> Dict[str, str]:
        """Return stock_code -> OpenDART corp_code using the official ZIP endpoint."""

        url = "https://opendart.fss.or.kr/api/corpCode.xml?" + urllib.parse.urlencode(
            {"crtfc_key": self._dart_key()}
        )
        request = urllib.request.Request(url, headers={"User-Agent": "Investory-AI/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            archive_bytes = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
            root = ET.fromstring(archive.read(xml_name))
        result = {}
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                result[stock_code] = corp_code
        return result

    def fetch_and_save_dart_disclosures(
        self,
        period_start: str,
        period_end: str,
        allow_llm: bool = False,
        security_codes: Optional[List[str]] = None,
    ) -> dict:
        """Backfill disclosures for DB securities over a simulation period."""

        from app.modules.simulation.persistence.db_persistence import get_db_connection

        corp_code_map = self.load_corp_code_map()
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_code FROM securities ORDER BY security_id")
                database_security_codes = [str(row[0]) for row in cur.fetchall()]
        finally:
            conn.close()

        selected_security_codes = security_codes or database_security_codes
        unknown_codes = sorted(set(selected_security_codes) - set(database_security_codes))
        if unknown_codes:
            raise ValueError(f"DB 종목 마스터에 없는 종목코드입니다: {unknown_codes}")

        mapped_security_count = 0
        fetched_count = 0
        saved_count = 0
        for security_code in selected_security_codes:
            corp_code = corp_code_map.get(security_code)
            if not corp_code:
                continue
            mapped_security_count += 1
            security_items = []
            page_no = 1
            while True:
                params = {
                    "crtfc_key": self._dart_key(),
                    "corp_code": corp_code,
                    "bgn_de": period_start.replace("-", ""),
                    "end_de": period_end.replace("-", ""),
                    "page_no": page_no,
                    "page_count": 100,
                }
                url = "https://opendart.fss.or.kr/api/list.json?" + urllib.parse.urlencode(params)
                request = urllib.request.Request(url, headers={"User-Agent": "Investory-AI/1.0"})
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                status = payload.get("status")
                if status == "013":
                    break
                if status != "000":
                    raise RuntimeError(f"OpenDART 오류({status}): {payload.get('message')}")
                security_items.extend(payload.get("list", []))
                total_pages = int(payload.get("total_page") or 1)
                if page_no >= total_pages:
                    break
                page_no += 1
            fetched_count += len(security_items)
            saved_count += self._save_disclosure_items(security_items, allow_llm=allow_llm)

        return {
            "periodStart": period_start,
            "periodEnd": period_end,
            "securityCount": len(selected_security_codes),
            "mappedSecurityCount": mapped_security_count,
            "fetchedCount": fetched_count,
            "savedCount": saved_count,
            "analysisPolicy": "HYBRID_LLM_RULE" if allow_llm else "RULE_ONLY",
        }

    def _save_disclosure_items(self, items: List[dict], allow_llm: bool) -> int:
        from app.modules.simulation.persistence.db_persistence import get_db_connection

        conn = get_db_connection()
        saved_count = 0
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_id, security_code, security_name FROM securities")
                security_rows = cur.fetchall()
                by_code = {str(row[1]): row[0] for row in security_rows}
                by_name = {str(row[2]): row[0] for row in security_rows}

                for item in items:
                    receipt_no = item.get("rcept_no", "")
                    receipt_date = item.get("rcept_dt", "")
                    security_id = by_code.get(str(item.get("stock_code", ""))) or by_name.get(
                        str(item.get("corp_name", ""))
                    )
                    if not receipt_no or len(receipt_date) != 8 or not security_id:
                        continue
                    report_name = item.get("report_nm", "")
                    analysis = self.evaluate_disclosure_impact(
                        report_name,
                        allow_llm=allow_llm,
                        require_llm=allow_llm,
                    )
                    event_date = f"{receipt_date[:4]}-{receipt_date[4:6]}-{receipt_date[6:8]}"
                    analysis_model = self.model if analysis["reason"].startswith("[OpenAI") else "RULE_ENGINE"
                    cur.execute(
                        """
                        INSERT INTO disclosure_events
                        (security_id, receipt_no, report_name, event_type,
                         event_date, available_at, direction, impact_score,
                         confidence, analysis_reason, source_json,
                         analysis_model, prompt_version, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, NOW(), NOW())
                        ON DUPLICATE KEY UPDATE
                            report_name = VALUES(report_name),
                            direction = VALUES(direction),
                            impact_score = VALUES(impact_score),
                            confidence = VALUES(confidence),
                            analysis_reason = VALUES(analysis_reason),
                            source_json = VALUES(source_json),
                            analysis_model = VALUES(analysis_model),
                            prompt_version = VALUES(prompt_version),
                            updated_at = NOW()
                        """,
                        (
                            security_id,
                            receipt_no,
                            report_name,
                            "ELECTRONIC_DISCLOSURE",
                            event_date,
                            f"{event_date} 00:00:00",
                            analysis["direction"],
                            analysis["impactScore"],
                            0.5 if analysis["direction"] == "NEUTRAL" else 0.8,
                            analysis["reason"],
                            json.dumps(item, ensure_ascii=False),
                            analysis_model,
                            "disclosure-v1",
                        ),
                    )
                    saved_count += 1
            conn.commit()
            return saved_count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
