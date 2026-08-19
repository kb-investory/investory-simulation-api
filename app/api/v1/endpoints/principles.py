"""
================================================================================
[API Endpoint Router] principles.py
================================================================================
■ 엔드포인트:
  - POST /api/v1/principles/proposals/accept : 검증된 기존 원칙 강화안 적용
  - GET  /api/v1/principles/recommendations : 성향 및 시뮬레이션 복기 기반 추천 원칙 목록 조회
  - POST /api/v1/principles                 : 추천된/사용자정의 원칙을 실제 사용자 활성 원칙으로 저장
================================================================================
"""

import os
import json
import asyncio
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict, AliasChoices

from app.api.error_responses import internal_server_error

router = APIRouter(tags=["Principles Management"])
logger = logging.getLogger(__name__)

# [DTO 스키마 정의]
class PrincipleItem(BaseModel):

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    recommendationId: Optional[int] = Field(default=None, validation_alias=AliasChoices("recommendationId", "recommendation_id"))
    principleText: Optional[str] = Field(default=None, validation_alias=AliasChoices("principleText", "principle_text"))
    ruleJson: Optional[Dict[str, Any]] = Field(default=None, validation_alias=AliasChoices("ruleJson", "rule_json"))
    sortOrder: Optional[int] = Field(default=1, validation_alias=AliasChoices("sortOrder", "sort_order"))


class SavePrinciplesRequest(BaseModel):
    principles: List[PrincipleItem]


class AcceptPrincipleProposalRequest(BaseModel):
    simulationId: int
    # recommendationId is positional and can shift when the principle order
    # changes. evaluationId is derived from principleSetItemId and is stable, so
    # new clients should send it and let recommendationId stay for old ones.
    recommendationId: Optional[int] = None
    evaluationId: Optional[str] = None


def _rule_path_exists(rule_json: dict, dotted_path: str) -> bool:
    current = rule_json
    for key in dotted_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _merge_rule_json(base: dict, patch: dict) -> dict:
    merged = dict(base or {})
    for section, values in (patch or {}).items():
        if isinstance(values, dict):
            merged[section] = {**(merged.get(section) or {}), **values}
        else:
            merged[section] = values
    return merged


@router.post("/principles/proposals/accept", summary="원칙 평가 강화안 적용")
def accept_principle_proposal(req: AcceptPrincipleProposalRequest):
    """Apply only a server-stored and validated V3 proposal to the active principle set."""
    from app.modules.simulation.db_persistence import (
        get_db_connection,
        load_simulation_from_db_by_id,
    )

    detail = load_simulation_from_db_by_id(req.simulationId)
    report = (detail or {}).get("report_json") or (detail or {}).get("reportJson") or {}
    if report.get("reportVersion") not in {"DETERMINISTIC_V10", "DETERMINISTIC_V11", "DETERMINISTIC_V12", "DETERMINISTIC_V13"}:
        raise HTTPException(status_code=409, detail="새 분석 버전의 리포트를 먼저 조회해 주세요.")
    evaluation_suggestions = [
        item.get("suggestion")
        for item in report.get("principleEvaluations", [])
        if isinstance(item, dict) and isinstance(item.get("suggestion"), dict)
    ]
    proposals = (
        report.get("principleDiscoveries", [])
        + report.get("principleReinforcements", [])
        + evaluation_suggestions
    )
    if req.evaluationId is None and req.recommendationId is None:
        raise HTTPException(status_code=422, detail="evaluationId 또는 recommendationId가 필요합니다.")
    proposal = None
    if req.evaluationId:
        proposal = next(
            (item for item in proposals if str(item.get("evaluationId") or "") == req.evaluationId),
            None,
        )
    if not proposal and req.recommendationId is not None:
        proposal = next(
            (item for item in proposals if int(item.get("recommendationId") or 0) == req.recommendationId),
            None,
        )
    if not proposal:
        raise HTTPException(status_code=404, detail="적용할 원칙 제안을 찾을 수 없습니다.")
    # The idempotency table is keyed on recommendationId, so resolve it from the
    # stored proposal rather than trusting the client's positional guess.
    recommendation_id = int(proposal.get("recommendationId") or req.recommendationId or 0)
    if not recommendation_id:
        raise HTTPException(status_code=422, detail="원칙 제안에 적용 식별자가 없습니다.")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            proposal_type = str(proposal.get("proposalType") or "")
            if proposal_type not in {"DISCOVERY", "REINFORCEMENT"}:
                raise HTTPException(status_code=422, detail="지원하지 않는 원칙 제안 유형입니다.")
            cur.execute(
                """
                INSERT IGNORE INTO principle_proposal_applications
                (simulation_run_id, user_id, recommendation_id, proposal_type,
                 application_status, proposal_snapshot_json, created_at)
                VALUES (%s, 1, %s, %s, 'PROCESSING', %s, NOW())
                """,
                (
                    req.simulationId,
                    recommendation_id,
                    proposal_type,
                    json.dumps(proposal, ensure_ascii=False),
                ),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    SELECT app.application_status, app.proposal_type,
                           app.principle_set_item_id, item.principle_text, item.rule_json
                    FROM principle_proposal_applications app
                    LEFT JOIN principle_set_items item
                      ON item.principle_set_item_id = app.principle_set_item_id
                    WHERE app.simulation_run_id = %s
                      AND app.user_id = 1
                      AND app.recommendation_id = %s
                    FOR UPDATE
                    """,
                    (req.simulationId, recommendation_id),
                )
                existing = cur.fetchone()
                if existing and existing[0] == "APPLIED":
                    existing_rule = existing[4] or {}
                    if isinstance(existing_rule, str):
                        try:
                            existing_rule = json.loads(existing_rule)
                        except Exception:
                            existing_rule = {}
                    conn.commit()
                    return {
                        "status": "SUCCESS",
                        "applicationType": (
                            "DISCOVERY_ADDED"
                            if existing[1] == "DISCOVERY"
                            else "REINFORCEMENT_UPDATED"
                        ),
                        "principleSetItemId": existing[2],
                        "recommendationId": recommendation_id,
                        "principleText": existing[3] or "",
                        "ruleJson": existing_rule,
                        "idempotentReplay": True,
                    }
                raise HTTPException(status_code=409, detail="동일한 원칙 제안을 적용 중입니다.")

            cur.execute(
                "SELECT principle_set_id FROM principle_sets WHERE user_id = 1 ORDER BY principle_set_id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="활성 원칙 세트가 없습니다.")
            principle_set_id = int(row[0])
            principle_text = proposal.get("description") or proposal.get("title") or "투자 원칙"
            rule_json = proposal.get("ruleJson") or {}
            # Reinforcement tightens the execution threshold this service owns.
            # The sentence itself belongs to the principle service and to the
            # user who wrote it, so it is never overwritten here.
            source_principle_text = str(proposal.get("sourcePrincipleText") or "").strip()

            if proposal.get("proposalType") == "DISCOVERY":
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM principle_set_items WHERE principle_set_id = %s",
                    (principle_set_id,),
                )
                sort_order = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO principle_set_items
                    (principle_set_id, principle_recommendation_id, principle_text, rule_json, sort_order)
                    VALUES (%s, NULL, %s, %s, %s)
                    """,
                    (
                        principle_set_id,
                        principle_text,
                        json.dumps(rule_json, ensure_ascii=False),
                        sort_order,
                    ),
                )
                principle_item_id = int(cur.lastrowid)
                applied_type = "DISCOVERY_ADDED"
                applied_rule_json = rule_json
            else:
                cur.execute(
                    """
                    SELECT principle_set_item_id, principle_text, rule_json
                    FROM principle_set_items
                    WHERE principle_set_id = %s
                    ORDER BY sort_order
                    """,
                    (principle_set_id,),
                )
                candidates = cur.fetchall()
                requested_item_id = proposal.get("principleSetItemId")
                source_text = str(proposal.get("sourcePrincipleText") or "").strip()
                target_rule = str(proposal.get("targetRule") or "")
                matched = None
                for candidate in candidates:
                    candidate_rule = candidate[2] or {}
                    if isinstance(candidate_rule, str):
                        try:
                            candidate_rule = json.loads(candidate_rule)
                        except Exception:
                            candidate_rule = {}
                    if requested_item_id is not None and int(candidate[0]) == int(requested_item_id):
                        matched = (candidate, candidate_rule)
                        break
                    if requested_item_id is not None:
                        continue
                    if (
                        source_text and str(candidate[1] or "").strip() == source_text
                    ) or _rule_path_exists(candidate_rule, target_rule):
                        matched = (candidate, candidate_rule)
                        break
                if not matched:
                    raise HTTPException(
                        status_code=409,
                        detail="강화할 기존 원칙 항목을 식별할 수 없습니다.",
                    )
                candidate, candidate_rule = matched
                merged_rule = _merge_rule_json(candidate_rule, rule_json)
                cur.execute(
                    """
                    UPDATE principle_set_items
                    SET rule_json = %s
                    WHERE principle_set_item_id = %s
                    """,
                    (
                        json.dumps(merged_rule, ensure_ascii=False),
                        candidate[0],
                    ),
                )
                principle_item_id = int(candidate[0])
                applied_type = "REINFORCEMENT_UPDATED"
                applied_rule_json = merged_rule
                # Report back the sentence that is actually stored, not the
                # generic template text bundled with the proposal.
                principle_text = str(candidate[1] or "") or source_principle_text or principle_text
            cur.execute(
                """
                UPDATE principle_proposal_applications
                SET application_status = 'APPLIED',
                    principle_set_item_id = %s,
                    applied_at = NOW()
                WHERE simulation_run_id = %s
                  AND user_id = 1
                  AND recommendation_id = %s
                """,
                (principle_item_id, req.simulationId, recommendation_id),
            )
        conn.commit()
        return {
            "status": "SUCCESS",
            "applicationType": applied_type,
            "principleSetItemId": principle_item_id,
            "recommendationId": recommendation_id,
            "principleText": principle_text,
            "ruleJson": applied_rule_json,
            "idempotentReplay": False,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as error:
        conn.rollback()
        raise internal_server_error(
            logger,
            error,
            code="PRINCIPLE_PROPOSAL_ACCEPT_INTERNAL_ERROR",
            message="원칙 제안을 적용하는 중 서버 오류가 발생했습니다.",
            simulation_id=req.simulationId,
        ) from error
    finally:
        conn.close()

@router.get("/principles/recommendations", summary="추천 원칙 목록 조회")
def get_recommended_principles():
    """
    [대응 화면: 원칙 추천 팝업/페이지]
    - MySQL DB 기반으로 사용자의 원칙 및 추천 원칙 목록을 조회합니다.
    """
    try:
        from app.modules.simulation.db_persistence import get_db_connection
        conn = get_db_connection()
        recommendations = []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT item.principle_set_item_id, item.principle_text, item.rule_json, item.sort_order
                FROM principle_set_items item
                JOIN principle_sets pset ON item.principle_set_id = pset.principle_set_id
                WHERE pset.user_id = 1
                ORDER BY item.sort_order ASC
            """)
            rows = cur.fetchall()
            for r in rows:
                rule_json = {}
                if r[2]:
                    try:
                        rule_json = json.loads(r[2]) if isinstance(r[2], str) else r[2]
                    except Exception:
                        rule_json = {}

                recommendations.append({
                    "recommendationId": r[0],
                    "principleType": "CUSTOM",
                    "title": r[1] or "투자 원칙",
                    "description": r[1] or "",
                    "principleText": r[1] or "",
                    "ruleJson": rule_json,
                    "sortOrder": r[3] or 1
                })
        conn.close()

        return {
            "recommendations": recommendations,
            "dataSource": "MYSQL",
            "usesMockData": False,
        }
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="PRINCIPLES_READ_INTERNAL_ERROR",
            message="추천 원칙을 조회하는 중 서버 오류가 발생했습니다.",
        ) from e


def _save_principles_db_task(principles_list):
    from app.config import settings
    import pymysql

    conn = pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DB,
        charset='utf8mb4',
        autocommit=True,
        connect_timeout=3
    )

    saved_items = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT principle_set_id FROM principle_sets WHERE user_id = 1 LIMIT 1")
            row = cur.fetchone()
            set_id = row[0] if row else 1

            for idx, item in enumerate(principles_list, 1):
                item_text = item.principleText or "선택된 투자 원칙"
                rule_json_str = json.dumps(item.ruleJson or {})
                item_order = item.sortOrder or idx
                rec_id = item.recommendationId

                existing_id = None
                if rec_id:
                    cur.execute("SELECT principle_set_item_id FROM principle_set_items WHERE principle_set_id = %s AND principle_recommendation_id = %s", (set_id, rec_id))
                    r_ex = cur.fetchone()
                    if r_ex:
                        existing_id = r_ex[0]

                if not existing_id:
                    cur.execute("SELECT principle_set_item_id FROM principle_set_items WHERE principle_set_id = %s AND sort_order = %s", (set_id, item_order))
                    r_ex = cur.fetchone()
                    if r_ex:
                        existing_id = r_ex[0]

                if existing_id:
                    cur.execute("""
                        UPDATE principle_set_items
                        SET principle_text = %s, rule_json = %s, sort_order = %s
                        WHERE principle_set_item_id = %s
                    """, (item_text, rule_json_str, item_order, existing_id))
                    p_item_id = existing_id
                else:
                    cur.execute("""
                        INSERT INTO principle_set_items (principle_set_id, principle_recommendation_id, principle_text, rule_json, sort_order)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (set_id, rec_id, item_text, rule_json_str, item_order))
                    p_item_id = cur.lastrowid

                saved_items.append({
                    "principleSetItemId": p_item_id,
                    "recommendationId": rec_id,
                    "principleText": item_text,
                    "ruleJson": item.ruleJson or {},
                    "sortOrder": item_order,
                    "isConfirmed": True
                })
    finally:
        conn.close()

    return saved_items


@router.post("/principles", summary="추천 원칙 저장 및 사용자 원칙 업데이트")
async def save_user_principles(req: SavePrinciplesRequest):
    """
    [대응 화면: 원칙 선택 저장]
    - 선택한 추천 원칙을 MySQL DB principle_sets / principle_set_items 테이블에 실제로 저장 및 업데이트합니다.
    """
    try:
        saved_items = await asyncio.to_thread(_save_principles_db_task, req.principles)
        return {
            "status": "SUCCESS",
            "message": f"총 {len(saved_items)}개의 투자 원칙이 성공적으로 DB에 적용되었습니다.",
            "principles": saved_items
        }
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="PRINCIPLES_WRITE_INTERNAL_ERROR",
            message="원칙을 저장하는 중 서버 오류가 발생했습니다.",
        ) from e
