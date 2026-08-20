"""
================================================================================
[API Endpoint Router] simulation.py
================================================================================
■ 프론트엔드 대응 엔드포인트 (7종 완전 매핑):
  1. GET  /api/simulation/simulations/overview            : 시뮬레이션 개요 조회 (대응 화면: xCJcT, WYSMi)
  2. POST /api/simulation/simulation-bots/compile         : 최신 원칙 봇 생성/컴파일 요청 (대응 화면: Inbqv)
  3. GET  /api/simulation/simulation-bots/compile-jobs/{jobId} : 봇 생성 상태 조회 (대응 화면: Inbqv, AZCR3)
  4. GET  /api/simulation/simulation-bots/comparators      : 비교 기준 봇 목록 조회 (대응 화면: Huymt)
  5. POST /api/simulation/simulations/run                  : 시뮬레이션 실행 (대응 화면: y9DNLy)
  6. GET  /api/simulation/simulations/{simulationId}       : 시뮬레이션 상세 조회 (대응 화면: p3vHxf, rGj4P, GTmqX)
  7. GET  /api/simulation/simulations/latest               : 최근 시뮬레이션 성과 조회 (대응 화면: xCJcT)
================================================================================
"""

import uuid
import asyncio
import logging
from copy import deepcopy
from threading import Lock
from typing import Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.api.error_responses import internal_server_error

from app.modules.simulation.rules.compiler import AIRuleCompiler, RuleCompilationError
from app.modules.simulation.rules.strengthen_spec import RULE_STRENGTHEN_SPEC
from app.modules.simulation.persistence.capital_calculator import InitialCapitalCalculator
from app.modules.simulation.analytics.report_generator import SimulationReportGenerator
from app.modules.simulation.analytics.analytics import find_divergence_moments
from app.modules.simulation.persistence.repository import SimulationDataError, SimulationRepository
from app.modules.simulation.analytics.comparator_details import (
    build_comparators,
    build_personal_comparator,
)
from app.modules.simulation.persistence.db_persistence import (
    get_simulation_history_from_db,
    load_simulation_from_db_by_id,
    save_simulation_report_to_db,
    get_latest_completed_simulation_id_from_db,
)

from app.api.endpoints.simulation_helpers import (
    RuleCompileRequest, RuleConfirmationRequest, SimulationRunRequest,
    SIMULATION_RUN_CACHE, TEST_USER_ID,
)
from app.api.endpoints.simulation_run_service import SimulationRunService

router = APIRouter(tags=["Simulation & Rules"])
logger = logging.getLogger(__name__)

ANALYTICS_RESPONSE_FIELDS = (
    "orderAudits",
    "screeningAudits",
    "variantMetrics",
    "benchmarks",
    "benchmarkData",
    "randomDistribution",
    "securityContributions",
    "actionContributions",
    "divergenceMoments",
    "behaviorPatterns",
    "actualPrincipleCompliance",
    "positionSnapshots",
    "principleItems",
    "ruleConfirmations",
    "securitySnapshots",
    "dailyPerformance",
)
COMPILE_JOB_CACHE: Dict[str, dict] = {}
REPORT_NARRATIVE_LOCK = Lock()
REPORT_NARRATIVE_IN_PROGRESS: set[int] = set()


def _analytics_response(data: dict) -> dict:
    return {field: data.get(field) for field in ANALYTICS_RESPONSE_FIELDS}


def _nested_rule_value(data: dict, dotted_path: str):
    current = data
    for key in str(dotted_path).split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _schedule_report_enrichment(
    background_tasks: BackgroundTasks,
    simulation_id: int,
    base_report: dict,
) -> bool:
    """Schedule at most one model enrichment task for each simulation run."""
    with REPORT_NARRATIVE_LOCK:
        if simulation_id in REPORT_NARRATIVE_IN_PROGRESS:
            return False
        REPORT_NARRATIVE_IN_PROGRESS.add(simulation_id)
    background_tasks.add_task(
        _enrich_simulation_report_in_background,
        simulation_id,
        deepcopy(base_report),
    )
    return True


def _enrich_simulation_report_in_background(simulation_id: int, base_report: dict) -> None:
    """Enrich one persisted deterministic report without blocking its API response."""

    try:
        enriched_report = SimulationReportGenerator().enrich_report(deepcopy(base_report))
        save_simulation_report_to_db(simulation_id, enriched_report)
        cached_detail = SIMULATION_RUN_CACHE.get(simulation_id)
        if cached_detail is not None:
            cached_detail["report_json"] = enriched_report
            cached_detail["reportJson"] = enriched_report
    except Exception as error:
        logger.warning(
            "Background report narrative enrichment failed for simulation %s",
            simulation_id,
            exc_info=error,
        )
    finally:
        with REPORT_NARRATIVE_LOCK:
            REPORT_NARRATIVE_IN_PROGRESS.discard(simulation_id)


def _load_detail_evidence(
    repository: SimulationRepository,
    account_id: Optional[int] = None,
) -> dict:
    resolved_account_id = repository.resolve_account_id(TEST_USER_ID, account_id)
    evidence = repository.load_comparator_evidence(TEST_USER_ID, resolved_account_id)
    return evidence if isinstance(evidence, dict) else {}


def _personal_bot_detail(
    repository: SimulationRepository,
    bot: dict,
    principle_items: Optional[List[dict]] = None,
    account_id: Optional[int] = None,
) -> dict:
    if principle_items is None:
        try:
            principle_items = repository.load_principles(TEST_USER_ID)
        except SimulationDataError as error:
            if error.code != "PRINCIPLES_NOT_FOUND":
                raise
            principle_items = []
    return build_personal_comparator(bot, principle_items, _load_detail_evidence(repository, account_id))


def _completed_compile_response(
    repository: SimulationRepository,
    job_id: str,
    bot: dict,
    principle_items: List[dict],
    *,
    compile_cache_hit: bool,
    compilation_metadata: dict,
    account_id: Optional[int] = None,
) -> dict:
    resolved_account_id = repository.resolve_account_id(TEST_USER_ID, account_id)
    response = {
        "jobId": job_id,
        "status": "COMPLETED",
        "progressPercent": 100,
        "personalBotId": bot["personalBotId"],
        "botVersion": f"v{bot['botVersion']}.0" if isinstance(bot.get("botVersion"), int) else bot.get("botVersion"),
        "ruleSchema": bot["ruleSchema"],
        "profileSource": {
            "source": "MYSQL",
            "analysisRunId": bot.get("analysisRunId"),
            "analysisVersion": bot.get("analysisVersion"),
        },
        "ruleCompilation": compilation_metadata,
        "compileCacheHit": compile_cache_hit,
        "accountId": resolved_account_id,
        "botDetail": _personal_bot_detail(repository, bot, principle_items, resolved_account_id),
        # The compiler flags thresholds it had to guess. Returning them here is
        # what turns audit.needs_user_confirmation from a dead field into a
        # question the user can actually answer.
        "ruleConfirmation": _rule_confirmation_view(repository, bot),
    }
    COMPILE_JOB_CACHE[job_id] = response
    return response


# ==============================================================================
# 1. 시뮬레이션 개요 조회 (대응 화면: xCJcT, WYSMi)
# ==============================================================================
def _get_overview_db_task(requested_account_id: Optional[int] = None):
    repository = SimulationRepository()
    account_id = repository.resolve_account_id(TEST_USER_ID, requested_account_id)
    overview = repository.load_overview(TEST_USER_ID, account_id)
    overview["accountId"] = account_id
    return overview

@router.get("/simulations/overview", summary="1. 시뮬레이션 개요 및 준비 상태 조회")
async def get_simulation_overview(
    start_date: Optional[str] = None,
    account_id: Optional[int] = None,
    accountId: Optional[int] = None,
):
    """
    [대응 화면: xCJcT, WYSMi]
    - DB 기반 시뮬레이션 적격 가능 날짜 범위, 시작 자본금 추천치, 계좌 연동 상태 개요를 조회합니다.
    """
    if account_id and accountId and account_id != accountId:
        raise HTTPException(status_code=422, detail="account_id와 accountId 값이 서로 다릅니다.")
    overview = await asyncio.to_thread(_get_overview_db_task, account_id or accountId)
    target_account_id = overview["accountId"]
    requested_start = start_date or overview["eligibleStartDate"]
    capital_info = None
    capital_error = None
    try:
        capital_info = await asyncio.to_thread(
            InitialCapitalCalculator().calculate,
            start_date=requested_start,
            account_id=target_account_id,
        )
    except SimulationDataError as error:
        capital_error = {"code": error.code, "message": error.message, "details": error.details}

    return {
        "isReady": capital_info is not None,
        "eligiblePeriod": {
            "startDate": overview["eligibleStartDate"],
            "endDate": overview["eligibleEndDate"],
            "totalDays": overview["journalDays"],
        },
        "recommendedInitialCapital": capital_info.get("totalInitialCapital") if capital_info else None,
        "accountId": target_account_id,
        "initialCapitalBreakdown": capital_info,
        "connectedAccountsCount": overview["connectedAccountsCount"],
        "recentSimulationCount": overview["recentSimulationCount"],
        "priceDataRange": {
            "startDate": overview["priceStartDate"],
            "endDate": overview["priceEndDate"],
            "tradingDayCount": overview["tradingDayCount"],
            "securityCount": overview["securityCount"],
        },
        "dataSource": "MYSQL",
        "usesMockData": False,
        "dataError": capital_error,
    }


# ==============================================================================
# 1-1. 초기 자금 계산 API (holding_snapshots ERD 기반)
# ==============================================================================
@router.get("/simulations/initial-capital", summary="초기 자금 계산 (holding_snapshots ERD 기반)")
@router.get("/simulations/calculate-initial-capital", summary="초기 자금 계산 하위 호환 엔드포인트")
def calculate_initial_capital(
    start_date: Optional[str] = None,
    account_id: Optional[int] = None,
    startDate: Optional[str] = None,
    accountId: Optional[int] = None,
):
    """
    [ERD holding_snapshots 기반 초기 자금 산출 API]
    - 선택한 시작일(start_date) 기준 보유 종목의 평가 금액 합계(holding_snapshots.market_value 합계)를 자동 연산합니다.
    """
    try:
        if start_date and startDate and start_date != startDate:
            raise HTTPException(status_code=422, detail="start_date와 startDate 값이 서로 다릅니다.")
        if account_id and accountId and account_id != accountId:
            raise HTTPException(status_code=422, detail="account_id와 accountId 값이 서로 다릅니다.")
        requested_start_date = start_date or startDate or "2026-03-01"
        repository = SimulationRepository()
        requested_account_id = repository.resolve_account_id(
            TEST_USER_ID,
            account_id or accountId,
        )
        calculator = InitialCapitalCalculator()
        return calculator.calculate(
            start_date=requested_start_date,
            account_id=requested_account_id,
        )
    except HTTPException:
        raise
    except SimulationDataError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="INITIAL_CAPITAL_INTERNAL_ERROR",
            message="초기 자금을 계산하는 중 서버 오류가 발생했습니다.",
            account_id=account_id or accountId,
            start_date=start_date or startDate,
        ) from e


# ==============================================================================
# 2. 최신 원칙 봇 생성 요청 (대응 화면: Inbqv)
# ==============================================================================
@router.post("/simulation-bots/compile", summary="2. 최신 원칙 봇 생성/컴파일 요청")
@router.post("/rules/compile", summary="AI Rule Compiler 하위 호환 엔드포인트")
def compile_simulation_bot(req: RuleCompileRequest):
    """
    [대응 화면: Inbqv]
    - 사용자의 자연어 투자 원칙과 6축 성향을 수신하여 8대 영역 표준 Rule JSON 봇을 컴파일 생성합니다.
    """
    try:
        repository = SimulationRepository()
        account_id = repository.resolve_account_id(TEST_USER_ID, req.account_id)
        principle_items = repository.load_principles(TEST_USER_ID)
        principles = req.principles or [item["principleText"] for item in principle_items]
        profile = repository.load_latest_investor_profile(TEST_USER_ID)
        actual_trades = req.actual_trades
        if actual_trades is None:
            overview = repository.load_overview(TEST_USER_ID, account_id)
            actual_trades = repository.load_actual_trades(
                account_id,
                overview["eligibleStartDate"],
                overview["eligibleEndDate"],
            )
        compiler = AIRuleCompiler()
        input_hash = compiler.build_input_fingerprint(principles, profile, actual_trades)
        existing_bot = repository.find_compiled_personal_bot_by_input_hash(TEST_USER_ID, input_hash)
        if existing_bot:
            compilation_metadata = dict(existing_bot.get("ruleCompilation") or {})
            compilation_metadata.update({
                "reusedCompiledBot": True,
                "inputHash": input_hash,
            })
            return _completed_compile_response(
                repository,
                f"JOB_REUSED_{existing_bot['personalBotId']}",
                existing_bot,
                principle_items,
                compile_cache_hit=True,
                compilation_metadata=compilation_metadata,
                account_id=account_id,
            )
        schema = compiler.compile(principles, profile, actual_trades)
        compiler.last_compilation_metadata["inputHash"] = input_hash
        saved_bot = repository.save_compiled_personal_bot(
            TEST_USER_ID,
            schema.to_dict(),
            profile,
            compiler.last_compilation_metadata,
            input_hash,
        )
        # DB의 createdAt까지 다시 읽어 comparator 조회와 완전히 같은 상세 응답을 만듭니다.
        saved_bot = repository.load_compiled_personal_bot(TEST_USER_ID, saved_bot["personalBotId"])
        job_id = f"JOB_{uuid.uuid4().hex[:8].upper()}"

        return _completed_compile_response(
            repository,
            job_id,
            saved_bot,
            principle_items,
            compile_cache_hit=False,
            compilation_metadata=compiler.last_compilation_metadata,
            account_id=account_id,
        )
    except SimulationDataError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )
    except RuleCompilationError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": error.code, "message": error.message, "fallbackUsed": False},
        )
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="RULE_COMPILATION_INTERNAL_ERROR",
            message="원칙 봇을 생성하는 중 서버 오류가 발생했습니다.",
        ) from e


# ==============================================================================
# 3. 최신 원칙 봇 생성 상태 조회 (대응 화면: Inbqv, AZCR3)
# ==============================================================================
@router.get("/simulation-bots/compile-jobs/{job_id}", summary="3. 원칙 봇 생성 상태 비동기 조회")
def get_compile_job_status(job_id: str):
    """
    [대응 화면: Inbqv, AZCR3]
    - 원칙 봇 생성 작업 진행 상태(RUNNING, COMPLETED)를 조회합니다.
    """
    result = COMPILE_JOB_CACHE.get(job_id)
    if result is None and job_id.startswith("JOB_REUSED_"):
        personal_bot_id = job_id.removeprefix("JOB_REUSED_")
        try:
            repository = SimulationRepository()
            bot = repository.load_compiled_personal_bot(TEST_USER_ID, personal_bot_id)
            result = _completed_compile_response(
                repository,
                job_id,
                bot,
                repository.load_principles(TEST_USER_ID),
                compile_cache_hit=True,
                compilation_metadata=dict(bot.get("ruleCompilation") or {}),
            )
        except SimulationDataError:
            result = None
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "COMPILE_JOB_NOT_FOUND", "message": "컴파일 작업을 찾을 수 없습니다."})
    return {**result, "message": "AI 원칙 봇 전략 생성이 완료되었습니다."}


# ==============================================================================
# 4. 비교 기준 봇 목록 조회 (대응 화면: Huymt)
# ==============================================================================
def _rule_confirmation_view(
    repository: SimulationRepository,
    bot: Optional[dict] = None,
) -> dict:
    """List every threshold the bot runs on, and say who decided each one.

    Never raises: this rides along on the compile response, and a missing
    confirmation table must not take the compiled bot down with it.
    """
    try:
        confirmations = repository.load_rule_confirmations(TEST_USER_ID)
    except Exception as error:
        logger.warning("Rule confirmations unavailable (%s)", type(error).__name__)
        confirmations = []
    confirmed_by_rule = {item["targetRule"]: item for item in confirmations}
    if bot is None:
        try:
            bot = repository.load_compiled_personal_bot(TEST_USER_ID)
        except Exception:
            bot = None
    if not bot:
        return {
            "confirmations": confirmations,
            "pendingConfirmations": [],
            "personalBotId": None,
        }

    rule_schema = bot.get("ruleSchema") or {}
    audit = rule_schema.get("audit") or {}
    pending = []
    for item in audit.get("interpreted_principles", []) or []:
        if not isinstance(item, dict):
            continue
        target_rule = str(item.get("ai_mapped_rule") or item.get("mappedRule") or "")
        if not target_rule or target_rule in confirmed_by_rule:
            continue
        pending.append({
            "targetRule": target_rule,
            "principleText": str(item.get("user_natural_text") or item.get("userNaturalText") or ""),
            "suggestedValue": _nested_rule_value(rule_schema, target_rule),
            "valueSource": "AI_INFERRED",
            "reason": next(
                (
                    str(row.get("reason") or "")
                    for row in audit.get("needs_user_confirmation", []) or []
                    if isinstance(row, dict) and str(row.get("field") or "") in target_rule
                ),
                "원칙 문구에 수치가 없어 AI가 기준값을 추정했습니다.",
            ),
        })
    return {
        "confirmations": confirmations,
        "pendingConfirmations": pending,
        "personalBotId": bot.get("personalBotId"),
        "aiConfidence": audit.get("ai_confidence"),
    }


@router.get("/simulation-bots/rule-confirmations", summary="4-1. 실행 기준 확정 상태 조회")
def get_rule_confirmations():
    """
    [대응 화면: 원칙 봇 기준 확인]
    - 사용자가 확정한 실행 기준과, AI가 추정해 확인이 필요한 기준을 함께 반환합니다.
    """
    repository = SimulationRepository()
    try:
        return _rule_confirmation_view(repository)
    except Exception as error:
        raise internal_server_error(
            logger,
            error,
            code="RULE_CONFIRMATIONS_READ_INTERNAL_ERROR",
            message="실행 기준 확정 상태를 조회하는 중 서버 오류가 발생했습니다.",
        ) from error


@router.post("/simulation-bots/rule-confirmations", summary="4-2. 실행 기준 확정")
def save_rule_confirmations(req: RuleConfirmationRequest):
    """
    [대응 화면: 원칙 봇 기준 확인]
    - AI가 추정한 기준값을 사용자가 확정합니다. 이후 원칙 평가는 확정값을 우선 적용합니다.
    """
    repository = SimulationRepository()
    try:
        unknown = sorted(
            {item.targetRule for item in req.confirmations} - set(RULE_STRENGTHEN_SPEC)
        )
        if unknown:
            raise SimulationDataError(
                "UNKNOWN_TARGET_RULE",
                "실행 규칙으로 존재하지 않는 경로가 포함되어 있습니다.",
                {"targetRules": unknown},
            )
        stored = repository.save_rule_confirmations(
            TEST_USER_ID,
            [item.model_dump() for item in req.confirmations],
        )
        return {
            "status": "SUCCESS",
            "confirmedCount": len(req.confirmations),
            "confirmations": stored,
            # A confirmed threshold changes what the bot executes, so the bot has
            # to be rebuilt before the next run reflects it.
            "recompileRequired": True,
            "message": "실행 기준을 확정했습니다. 투자봇을 다시 생성한 뒤 시뮬레이션을 실행해 주세요.",
        }
    except SimulationDataError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )
    except Exception as error:
        raise internal_server_error(
            logger,
            error,
            code="RULE_CONFIRMATIONS_WRITE_INTERNAL_ERROR",
            message="실행 기준을 확정하는 중 서버 오류가 발생했습니다.",
        ) from error


@router.get("/simulation-bots/comparators", summary="4. 대조 비교 참가자 봇 4종 목록 조회")
def get_comparator_bots(
    personalBotId: Optional[str] = None,
    accountId: Optional[int] = None,
):
    """
    [대응 화면: Huymt]
    - 시뮬레이션에 참전하는 4개 대조군 봇(실제 나, 개인봇, 유명 퀀트봇, 원숭이봇)의 설명과 설정을 조회합니다.
    """
    repository = SimulationRepository()
    try:
        bot = repository.load_compiled_personal_bot(TEST_USER_ID, personalBotId)
        try:
            principle_items = repository.load_principles(TEST_USER_ID)
        except SimulationDataError as error:
            if error.code != "PRINCIPLES_NOT_FOUND":
                raise
            principle_items = []
        return build_comparators(bot, principle_items, _load_detail_evidence(repository, accountId))
    except SimulationDataError as error:
        status_code = 404 if personalBotId and error.code == "PERSONAL_BOT_NOT_COMPILED" else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )


# ==============================================================================
# 5. 시뮬레이션 백테스트 실행 (대응 화면: y9DNLy)
# ==============================================================================
@router.post("/simulations/run", summary="5. 4개 비교 참가자 시뮬레이션 백테스트 실행")
def run_simulation(req: SimulationRunRequest, background_tasks: BackgroundTasks = None):
    """
    [대응 화면: y9DNLy]
    - 4개 대조군 봇의 독립 백테스트를 일별 이벤트 루프로 연산합니다.
    """
    background_tasks = background_tasks or BackgroundTasks()
    try:
        service = SimulationRunService(
            req, background_tasks,
            schedule_report_enrichment=_schedule_report_enrichment,
        )
        return service.run()
    except SimulationDataError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )
    except RuleCompilationError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": error.code, "message": error.message, "fallbackUsed": False},
        )
    except HTTPException:
        raise
    except Exception as error:
        raise internal_server_error(
            logger,
            error,
            code="SIMULATION_RUN_INTERNAL_ERROR",
            message="시뮬레이션을 실행하는 중 서버 오류가 발생했습니다.",
            simulation_run_id=req.simulation_run_id,
        ) from error


# ==============================================================================
# 5-1. 시뮬레이션 비동기 실행 상태 조회
# ==============================================================================
@router.get("/simulations/{simulation_id}/status", summary="5-1. 시뮬레이션 실행 상태 비동기 조회")
def get_simulation_run_status(simulation_id: int):
    """
    [대응 화면: y9DNLy 진행률 폴링]
    - 시뮬레이션 실행 작업 진행 상태(RUNNING, COMPLETED)를 조회합니다.
    """
    from app.modules.simulation.persistence.db_persistence import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_status, error_message FROM simulation_runs WHERE simulation_run_id = %s AND user_id = %s",
                (simulation_id, TEST_USER_ID),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"시뮬레이션 ID({simulation_id})가 존재하지 않습니다.")
    progress = 100 if row[0] in {"COMPLETED", "FAILED"} else 50
    return {
        "simulationRunId": simulation_id,
        "status": row[0],
        "progressPercent": progress,
        "message": row[1] or ("시뮬레이션 백테스트 연산이 완료되었습니다." if row[0] == "COMPLETED" else "시뮬레이션을 처리하고 있습니다."),
    }


# ==============================================================================
# 5-2. 과거 시뮬레이션 히스토리 목록 조회 (대응 화면: 대시보드 하단 이력)
# ==============================================================================
@router.get("/simulations/history", summary="5-2. 과거 시뮬레이션 히스토리 목록 조회")
def get_simulation_history():
    """
    [대응 화면: SimulationDashboard.vue 이력 목록]
    - 사용자가 실행했던 과거 시뮬레이션 회차별 히스토리 기록 목록을 반환합니다.
    """
    db_history = get_simulation_history_from_db(user_id=1)
    return db_history or []


# ==============================================================================
# 6. 최근 시뮬레이션 성과 조회 (대응 화면: xCJcT)
# ==============================================================================
@router.get("/simulations/latest", summary="6. 최근 시뮬레이션 성과 및 결과 조회")
async def get_latest_simulation():
    """
    [대응 화면: xCJcT]
    - 가장 최근 실행된 시뮬레이션의 대시보드 성과 데이터를 조회합니다.
    - DB/캐시에 실행 기록이 없으면 404를 반환합니다.
    """
    if SIMULATION_RUN_CACHE:
        latest_id = list(SIMULATION_RUN_CACHE.keys())[-1]
        return await asyncio.to_thread(get_simulation_detail, latest_id)

    max_id = await asyncio.to_thread(
        get_latest_completed_simulation_id_from_db,
        TEST_USER_ID,
    )
    if max_id is None:
        raise HTTPException(status_code=404, detail="저장된 시뮬레이션이 없습니다.")
    return await asyncio.to_thread(get_simulation_detail, max_id)


# ==============================================================================
# 7. 시뮬레이션 상세 조회 (대응 화면: p3vHxf, rGj4P, GTmqX)
# ==============================================================================
@router.get("/simulations/{simulation_id}", summary="7. 특정 시뮬레이션 상세 결과 조회")
def get_simulation_detail(simulation_id: int):
    """
    [대응 화면: p3vHxf, rGj4P, GTmqX]
    - 특정 시뮬레이션 ID의 4개 봇 성과 비교, 일별 자산 그래프, 상세 체결 일지를 조회합니다.
    """
    # 1. 인메모리 세션 캐시에서 확인
    if simulation_id in SIMULATION_RUN_CACHE:
        cached = SIMULATION_RUN_CACHE[simulation_id]
        return {
            "simulationRun": {
                "simulationRunId": simulation_id,
                "periodStart": cached.get("periodStart"),
                "periodEnd": cached.get("periodEnd"),
                "initialCapital": cached.get("initialCapital"),
                "status": "COMPLETED"
            },
            "participantSummary": cached.get("participantSummary", []),
            "simulationVariants": [
                {"simulationVariantId": 1, "variantType": "ACTUAL_USER", "variantName": "실제 나"},
                {"simulationVariantId": 2, "variantType": "PERSONAL_BOT", "variantName": "나의 투자봇 v1"},
                {"simulationVariantId": 3, "variantType": "FAMOUS_STRATEGY", "variantName": "우량 가치·품질 퀀트 봇"},
                {"simulationVariantId": 4, "variantType": "RANDOM_BOT", "variantName": "원숭이 봇"}
            ],
            "simulatedTrades": cached.get("simulatedTrades", []),
            "dailyPerformance": cached.get("dailyPerformance", []),
            "dailySnapshots": cached.get("dailySnapshots", []),
            **_analytics_response(cached),
        }

    # 2. DB에서 저장된 시뮬레이션 데이터 조회
    db_detail = load_simulation_from_db_by_id(simulation_id)
    if db_detail:
        SIMULATION_RUN_CACHE[simulation_id] = db_detail
        return {
            "simulationRun": {
                "simulationRunId": simulation_id,
                "periodStart": db_detail.get("periodStart"),
                "periodEnd": db_detail.get("periodEnd"),
                "initialCapital": db_detail.get("initialCapital"),
                "status": "COMPLETED"
            },
            "participantSummary": db_detail.get("participantSummary", []),
            "simulationVariants": [
                {"simulationVariantId": 1, "variantType": "ACTUAL_USER", "variantName": "실제 나"},
                {"simulationVariantId": 2, "variantType": "PERSONAL_BOT", "variantName": "나의 투자봇 v1"},
                {"simulationVariantId": 3, "variantType": "FAMOUS_STRATEGY", "variantName": "우량 가치·품질 퀀트 봇"},
                {"simulationVariantId": 4, "variantType": "RANDOM_BOT", "variantName": "원숭이 봇"}
            ],
            "simulatedTrades": db_detail.get("simulatedTrades", []),
            "dailyPerformance": db_detail.get("dailyPerformance", []),
            "dailySnapshots": db_detail.get("dailySnapshots", []),
            **_analytics_response(db_detail),
        }

    raise HTTPException(status_code=404, detail=f"시뮬레이션 ID({simulation_id})가 존재하지 않습니다.")


# ==============================================================================
# 8. 새 결과 리포트 API (대응 화면: 리포트 탭 / 결과 복기)
# ==============================================================================
@router.get("/simulations/{simulation_id}/report", summary="8. AI 시뮬레이션 복기 및 결과 리포트 조회")
def get_simulation_report(simulation_id: int, background_tasks: BackgroundTasks):
    """
    [대응 화면: 리포트 탭 / 결과 복기]
    - 백테스트 실행 내역 기반 원칙 준수 복기(decisionReviews), 근거 검증(evidenceReviews),
      학습 인사이트(learningInsights), 기존 원칙 평가(principleEvaluations), 강화안을 종합 반환합니다.
    """
    try:
        # 1. 인메모리 캐시에서 먼저 확인
        if simulation_id in SIMULATION_RUN_CACHE:
            detail_data = SIMULATION_RUN_CACHE[simulation_id]
        else:
            # 2. 캐시 없으면 DB에서 직접 조회
            detail_data = load_simulation_from_db_by_id(simulation_id)
            if detail_data:
                SIMULATION_RUN_CACHE[simulation_id] = detail_data
            else:
                # 3. 캐시 및 DB에 데이터가 없으면 404 에러 발생
                raise HTTPException(status_code=404, detail=f"시뮬레이션 ID({simulation_id})가 존재하지 않아 리포트를 생성할 수 없습니다.")

        # 이미 캐시되거나 DB에 저장된 report_json이 존재하는 경우 바로 반환
        cached_report = detail_data.get("report_json") or detail_data.get("reportJson")
        if (
            cached_report
            and isinstance(cached_report, dict)
            and cached_report.get("reportVersion") == SimulationReportGenerator.REPORT_VERSION
        ):
            if cached_report.get("generationMetadata", {}).get("narrativeStatus") == "PENDING":
                _schedule_report_enrichment(
                    background_tasks,
                    simulation_id,
                    cached_report,
                )
            print(f"[Simulation Endpoint] simulation_id={simulation_id} 기존 저장된 report_json 반환")
            return cached_report

        generator = SimulationReportGenerator()
        report_analytics = _analytics_response(detail_data)
        report_analytics["ruleSchema"] = detail_data.get("ruleSchema") or {}
        try:
            report_repository = SimulationRepository(reuse_connection=True)
            report_prices = report_repository.load_daily_prices(
                detail_data["periodStart"],
                detail_data["periodEnd"],
            )
            report_analytics["divergenceMoments"] = find_divergence_moments(
                detail_data.get("simulatedTrades", []),
                report_prices,
            )
            report_analytics["dailyPrices"] = report_prices
        except Exception as error:
            logger.warning(
                "Could not refresh report trade outcomes for simulation %s; using stored analytics",
                simulation_id,
                exc_info=error,
            )
        report_data = generator.build_deterministic_report(
            simulation_run_id=simulation_id,
            simulated_trades=detail_data.get("simulatedTrades", []),
            participant_summary=detail_data.get("participantSummary", []),
            daily_performance=detail_data.get("dailyPerformance", detail_data.get("dailySnapshots", [])),
            analytics=report_analytics,
        )

        if report_data:
            save_simulation_report_to_db(simulation_id, report_data)
            detail_data["report_json"] = report_data
            detail_data["reportJson"] = report_data
            _schedule_report_enrichment(
                background_tasks,
                simulation_id,
                report_data,
            )

        return report_data
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="SIMULATION_REPORT_INTERNAL_ERROR",
            message="시뮬레이션 리포트를 생성하는 중 서버 오류가 발생했습니다.",
            simulation_id=simulation_id,
        ) from e
