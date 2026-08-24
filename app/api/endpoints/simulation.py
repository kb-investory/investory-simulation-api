"""
================================================================================
[API Endpoint Router] simulation.py
================================================================================
■ 프론트엔드 대응 엔드포인트 (8종 완전 매핑):
  1. GET  /simulation/overview                        : 시뮬레이션 개요 조회 (대응 화면: xCJcT, WYSMi)
  2. POST /simulation/bots/compile                     : 최신 원칙 봇 생성/컴파일 요청 (비동기, 대응 화면: Inbqv)
  3. GET  /simulation/bots/compile-jobs/{jobId}        : 봇 생성 상태 조회 (대응 화면: Inbqv, AZCR3)
  4. GET  /simulation/bots/comparators                 : 비교 기준 봇 목록 조회 (대응 화면: Huymt)
  5. POST /simulation/run                              : 시뮬레이션 실행 (대응 화면: y9DNLy)
  6. GET  /simulation/latest                           : 최근 시뮬레이션 성과 조회 (대응 화면: xCJcT)
  7. GET  /simulation/{simulationId}                   : 시뮬레이션 상세 조회 (대응 화면: p3vHxf, rGj4P, GTmqX)
  8. GET  /simulation/{simulationId}/report            : AI 시뮬레이션 복기 및 결과 리포트 조회
================================================================================
"""

import uuid
import asyncio
import logging
import threading
from copy import deepcopy
from threading import Lock
from typing import Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.api.error_responses import internal_server_error
from app.core.auth import get_current_user_id

from app.modules.simulation.rules.compiler import AIRuleCompiler, RuleCompilationError
from app.modules.simulation.rules.strengthen_spec import RULE_STRENGTHEN_SPEC
from app.modules.simulation.persistence.capital_calculator import InitialCapitalCalculator
from app.modules.simulation.analytics.report_generator import SimulationReportGenerator
from app.modules.simulation.analytics.report_analysis import deliverable
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
    get_simulation_owner_id,
)

from app.api.endpoints.simulation_helpers import (
    RuleCompileRequest, RuleConfirmationRequest, SimulationRunRequest,
    SIMULATION_RUN_CACHE,
)
from app.api.endpoints.simulation_run_service import SimulationRunService, submit_simulation_run

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
COMPILE_LOCK = Lock()
# At most one in-flight compile per user — a second POST while one is still
# running (e.g. an impatient retry click) reuses the same job instead of
# kicking off a duplicate LLM call (#22).
COMPILE_IN_PROGRESS: Dict[int, str] = {}
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
    simulation_id: int,
    base_report: dict,
) -> bool:
    """Schedule at most one model enrichment task for each simulation run.

    #34: 더 이상 FastAPI BackgroundTasks(HTTP 응답 이후에만 실행)에 묶이지 않는다 — 호출부
    (SimulationRunService.run_async_phase)가 이미 자체 bounded worker pool 안에서 돌고
    있어서, 여기서는 그 워커 슬롯을 LLM 타임아웃까지 붙잡지 않도록 별도 데몬 스레드로
    fire-and-forget만 한다. get_simulation_report처럼 요청-응답 흐름 안에서 호출되는
    경우도 있어(라우터 아래쪽 참고) BackgroundTasks 의존을 완전히 제거하는 쪽이 두 호출부
    모두에 맞다.
    """
    with REPORT_NARRATIVE_LOCK:
        if simulation_id in REPORT_NARRATIVE_IN_PROGRESS:
            return False
        REPORT_NARRATIVE_IN_PROGRESS.add(simulation_id)
    threading.Thread(
        target=_enrich_simulation_report_in_background,
        args=(simulation_id, deepcopy(base_report)),
        daemon=True,
    ).start()
    return True


def _require_owned_simulation(simulation_id: int, user_id: int) -> None:
    """Refuse a simulation that does not belong to the caller.

    Ownership is read from the database rather than the in-memory cache: that
    cache is process-wide and its entries do not all carry an owner, so
    trusting it is what let one account's run reach another account.

    A stranger's id answers 404, not 403, so probing ids cannot be used to
    learn which simulations exist.
    """
    owner_id = get_simulation_owner_id(simulation_id)
    if owner_id is None or owner_id != user_id:
        raise HTTPException(
            status_code=404,
            detail=f"시뮬레이션 ID({simulation_id})를 찾을 수 없습니다.",
        )


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
    user_id: int,
    account_id: Optional[int] = None,
) -> dict:
    resolved_account_id = repository.resolve_account_id(user_id, account_id)
    evidence = repository.load_comparator_evidence(user_id, resolved_account_id)
    return evidence if isinstance(evidence, dict) else {}


def _personal_bot_detail(
    repository: SimulationRepository,
    user_id: int,
    bot: dict,
    principle_items: Optional[List[dict]] = None,
    account_id: Optional[int] = None,
) -> dict:
    if principle_items is None:
        try:
            principle_items = repository.load_principles(user_id)
        except SimulationDataError as error:
            if error.code != "PRINCIPLES_NOT_FOUND":
                raise
            principle_items = []
    return build_personal_comparator(bot, principle_items, _load_detail_evidence(repository, user_id, account_id))


def _completed_compile_response(
    repository: SimulationRepository,
    user_id: int,
    job_id: str,
    bot: dict,
    principle_items: List[dict],
    *,
    compile_cache_hit: bool,
    compilation_metadata: dict,
    account_id: Optional[int] = None,
) -> dict:
    resolved_account_id = repository.resolve_account_id(user_id, account_id)
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
        "botDetail": _personal_bot_detail(repository, user_id, bot, principle_items, resolved_account_id),
        # The compiler flags thresholds it had to guess. Returning them here is
        # what turns audit.needs_user_confirmation from a dead field into a
        # question the user can actually answer.
        "ruleConfirmation": _rule_confirmation_view(repository, user_id, bot),
    }
    COMPILE_JOB_CACHE[job_id] = response
    return response


# ==============================================================================
# 1. 시뮬레이션 개요 조회 (대응 화면: xCJcT, WYSMi)
# ==============================================================================
def _get_overview_db_task(user_id: int, requested_account_id: Optional[int] = None):
    repository = SimulationRepository()
    account_id = repository.resolve_account_id(user_id, requested_account_id)
    overview = repository.load_overview(user_id, account_id)
    overview["accountId"] = account_id
    return overview

@router.get("/overview", summary="1. 시뮬레이션 개요 및 준비 상태 조회")
async def get_simulation_overview(
    start_date: Optional[str] = None,
    account_id: Optional[int] = None,
    accountId: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
):
    """
    [대응 화면: xCJcT, WYSMi]
    - DB 기반 시뮬레이션 적격 가능 날짜 범위, 시작 자본금 추천치, 계좌 연동 상태 개요를 조회합니다.
    """
    if account_id and accountId and account_id != accountId:
        raise HTTPException(status_code=422, detail="account_id와 accountId 값이 서로 다릅니다.")
    overview = await asyncio.to_thread(_get_overview_db_task, user_id, account_id or accountId)
    target_account_id = overview["accountId"]
    requested_start = start_date or overview["eligibleStartDate"]
    capital_info = None
    capital_error = None
    if requested_start is None:
        # No explicit date requested and nothing to derive one from (no
        # journal entries, no snapshot, no trades) — calling
        # calculate(start_date=None) would silently pass NULL into every SQL
        # comparison downstream and fail with a misleading data-not-found
        # error instead of the actual "can't tell where to start" problem.
        capital_error = {
            "code": "ELIGIBLE_START_DATE_UNKNOWN",
            "message": "시뮬레이션 시작일을 추천할 수 없습니다 — 계좌에 거래내역이나 투자 일지가 없습니다.",
            "details": {"accountId": target_account_id},
        }
    else:
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
@router.get("/initial-capital", summary="초기 자금 계산 (holding_snapshots ERD 기반)")
def calculate_initial_capital(
    start_date: Optional[str] = None,
    account_id: Optional[int] = None,
    startDate: Optional[str] = None,
    accountId: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
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
            user_id,
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
@router.post("/bots/compile", summary="2. 최신 원칙 봇 생성/컴파일 요청")
def compile_simulation_bot(
    req: RuleCompileRequest,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
):
    """
    [대응 화면: Inbqv]
    - 사용자의 자연어 투자 원칙과 6축 성향을 수신하여 8대 영역 표준 Rule JSON 봇을 컴파일 생성합니다.
    - LLM 호출(최대 REASONING_LLM_TIMEOUT=120초)은 백그라운드로 넘어가며, 이 응답은
      즉시 job_id만 반환합니다. 실제 결과는 GET /bots/compile-jobs/{jobId}로 폴링합니다 (#22).
    """
    try:
        repository = SimulationRepository()
        account_id = repository.resolve_account_id(user_id, req.account_id)
        principle_items = repository.load_principles(user_id)
        principles = req.principles or [item["principleText"] for item in principle_items]
        profile = repository.load_latest_investor_profile(user_id)
        actual_trades = req.actual_trades
        if actual_trades is None:
            overview = repository.load_overview(user_id, account_id)
            actual_trades = repository.load_actual_trades(
                account_id,
                overview["eligibleStartDate"],
                overview["eligibleEndDate"],
            )
        compiler = AIRuleCompiler()
        input_hash = compiler.build_input_fingerprint(principles, profile, actual_trades)
        existing_bot = repository.find_compiled_personal_bot_by_input_hash(user_id, input_hash)
        if existing_bot:
            # A cache hit is a fast DB lookup, not an LLM call — no reason to
            # make the caller poll for something that's already known.
            compilation_metadata = dict(existing_bot.get("ruleCompilation") or {})
            compilation_metadata.update({
                "reusedCompiledBot": True,
                "inputHash": input_hash,
            })
            return _completed_compile_response(
                repository,
                user_id,
                f"JOB_REUSED_{existing_bot['personalBotId']}",
                existing_bot,
                principle_items,
                compile_cache_hit=True,
                compilation_metadata=compilation_metadata,
                account_id=account_id,
            )

        with COMPILE_LOCK:
            in_progress_job_id = COMPILE_IN_PROGRESS.get(user_id)
        if in_progress_job_id:
            in_progress = COMPILE_JOB_CACHE.get(in_progress_job_id) or {}
            return {
                "jobId": in_progress_job_id,
                "status": in_progress.get("status", "RUNNING"),
                "progressPercent": in_progress.get("progressPercent", 10),
                "message": "이미 진행 중인 투자봇 생성 작업이 있습니다.",
            }

        job_id = f"JOB_{uuid.uuid4().hex[:8].upper()}"
        COMPILE_JOB_CACHE[job_id] = {
            "jobId": job_id,
            "status": "RUNNING",
            "progressPercent": 10,
        }
        with COMPILE_LOCK:
            COMPILE_IN_PROGRESS[user_id] = job_id

        background_tasks.add_task(
            _compile_personal_bot_in_background,
            job_id,
            user_id,
            principles,
            profile,
            actual_trades,
            account_id,
            principle_items,
            input_hash,
        )
        return {
            "jobId": job_id,
            "status": "RUNNING",
            "progressPercent": 10,
            "message": "투자봇을 생성하고 있습니다.",
        }
    except SimulationDataError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )
    except Exception as e:
        raise internal_server_error(
            logger,
            e,
            code="RULE_COMPILATION_INTERNAL_ERROR",
            message="원칙 봇을 생성하는 중 서버 오류가 발생했습니다.",
        ) from e


def _compile_personal_bot_in_background(
    job_id: str,
    user_id: int,
    principles: List[str],
    profile: dict,
    actual_trades: List[dict],
    account_id: int,
    principle_items: List[dict],
    input_hash: str,
) -> None:
    """Run the actual LLM compile off the request/response cycle.

    Never raises: this runs after the HTTP response is already gone, so every
    failure has to land in COMPILE_JOB_CACHE as a FAILED status for the next
    poll to see, instead of an exception nobody is left to catch.
    """
    repository = SimulationRepository()
    try:
        compiler = AIRuleCompiler()
        schema = compiler.compile(principles, profile, actual_trades)
        compiler.last_compilation_metadata["inputHash"] = input_hash
        saved_bot = repository.save_compiled_personal_bot(
            user_id,
            schema.to_dict(),
            profile,
            compiler.last_compilation_metadata,
            input_hash,
        )
        # DB의 createdAt까지 다시 읽어 comparator 조회와 완전히 같은 상세 응답을 만듭니다.
        saved_bot = repository.load_compiled_personal_bot(user_id, saved_bot["personalBotId"])
        _completed_compile_response(
            repository,
            user_id,
            job_id,
            saved_bot,
            principle_items,
            compile_cache_hit=False,
            compilation_metadata=compiler.last_compilation_metadata,
            account_id=account_id,
        )  # writes the COMPLETED result into COMPILE_JOB_CACHE[job_id] itself
    except RuleCompilationError as error:
        COMPILE_JOB_CACHE[job_id] = {
            "jobId": job_id,
            "status": "FAILED",
            "progressPercent": 100,
            "error": {"code": error.code, "message": error.message, "fallbackUsed": False},
        }
    except Exception as error:
        logger.error(
            "Background bot compilation failed for job %s",
            job_id,
            exc_info=error,
        )
        COMPILE_JOB_CACHE[job_id] = {
            "jobId": job_id,
            "status": "FAILED",
            "progressPercent": 100,
            "error": {
                "code": "RULE_COMPILATION_INTERNAL_ERROR",
                "message": "원칙 봇을 생성하는 중 서버 오류가 발생했습니다.",
            },
        }
    finally:
        with COMPILE_LOCK:
            if COMPILE_IN_PROGRESS.get(user_id) == job_id:
                COMPILE_IN_PROGRESS.pop(user_id, None)


# ==============================================================================
# 3. 최신 원칙 봇 생성 상태 조회 (대응 화면: Inbqv, AZCR3)
# ==============================================================================
@router.get("/bots/compile-jobs/{job_id}", summary="3. 원칙 봇 생성 상태 비동기 조회")
def get_compile_job_status(job_id: str, user_id: int = Depends(get_current_user_id)):
    """
    [대응 화면: Inbqv, AZCR3]
    - 원칙 봇 생성 작업 진행 상태(RUNNING, COMPLETED)를 조회합니다.
    """
    result = COMPILE_JOB_CACHE.get(job_id)
    if result is None and job_id.startswith("JOB_REUSED_"):
        personal_bot_id = job_id.removeprefix("JOB_REUSED_")
        try:
            repository = SimulationRepository()
            bot = repository.load_compiled_personal_bot(user_id, personal_bot_id)
            result = _completed_compile_response(
                repository,
                user_id,
                job_id,
                bot,
                repository.load_principles(user_id),
                compile_cache_hit=True,
                compilation_metadata=dict(bot.get("ruleCompilation") or {}),
            )
        except SimulationDataError:
            result = None
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "COMPILE_JOB_NOT_FOUND", "message": "컴파일 작업을 찾을 수 없습니다."})
    if result.get("status") != "COMPLETED":
        return result
    return {**result, "message": "AI 원칙 봇 전략 생성이 완료되었습니다."}


# ==============================================================================
# 4. 비교 기준 봇 목록 조회 (대응 화면: Huymt)
# ==============================================================================
def _rule_confirmation_view(
    repository: SimulationRepository,
    user_id: int,
    bot: Optional[dict] = None,
) -> dict:
    """List every threshold the bot runs on, and say who decided each one.

    Never raises: this rides along on the compile response, and a missing
    confirmation table must not take the compiled bot down with it.
    """
    try:
        confirmations = repository.load_rule_confirmations(user_id)
    except Exception as error:
        logger.warning("Rule confirmations unavailable (%s)", type(error).__name__)
        confirmations = []
    confirmed_by_rule = {item["targetRule"]: item for item in confirmations}
    if bot is None:
        try:
            bot = repository.load_compiled_personal_bot(user_id)
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


@router.get("/bots/rule-confirmations", summary="4-1. 실행 기준 확정 상태 조회")
def get_rule_confirmations(user_id: int = Depends(get_current_user_id)):
    """
    [대응 화면: 원칙 봇 기준 확인]
    - 사용자가 확정한 실행 기준과, AI가 추정해 확인이 필요한 기준을 함께 반환합니다.
    """
    repository = SimulationRepository()
    try:
        return _rule_confirmation_view(repository, user_id)
    except Exception as error:
        raise internal_server_error(
            logger,
            error,
            code="RULE_CONFIRMATIONS_READ_INTERNAL_ERROR",
            message="실행 기준 확정 상태를 조회하는 중 서버 오류가 발생했습니다.",
        ) from error


@router.post("/bots/rule-confirmations", summary="4-2. 실행 기준 확정")
def save_rule_confirmations(req: RuleConfirmationRequest, user_id: int = Depends(get_current_user_id)):
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
            user_id,
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


@router.get("/bots/comparators", summary="4. 대조 비교 참가자 봇 4종 목록 조회")
def get_comparator_bots(
    personalBotId: Optional[str] = None,
    accountId: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
):
    """
    [대응 화면: Huymt]
    - 시뮬레이션에 참전하는 4개 대조군 봇(실제 나, 개인봇, 유명 퀀트봇, 원숭이봇)의 설명과 설정을 조회합니다.
    """
    repository = SimulationRepository()
    try:
        bot = None
        personal_bot_error = None
        try:
            bot = repository.load_compiled_personal_bot(user_id, personalBotId)
        except SimulationDataError as error:
            # A specific bot id that doesn't exist is a real 404 below. Only
            # "nobody has compiled one yet" degrades — the other 3 comparators
            # never needed a personal bot in the first place.
            if personalBotId or error.code != "PERSONAL_BOT_NOT_COMPILED":
                raise
            personal_bot_error = {"code": error.code, "message": error.message}

        principle_items = []
        if bot is not None:
            try:
                principle_items = repository.load_principles(user_id)
            except SimulationDataError as error:
                if error.code != "PRINCIPLES_NOT_FOUND":
                    raise
                principle_items = []
        return build_comparators(
            bot, principle_items, _load_detail_evidence(repository, user_id, accountId),
            personal_bot_error=personal_bot_error,
        )
    except SimulationDataError as error:
        status_code = 404 if personalBotId and error.code == "PERSONAL_BOT_NOT_COMPILED" else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": error.message, "details": error.details},
        )


# ==============================================================================
# 5. 시뮬레이션 백테스트 실행 (대응 화면: y9DNLy)
# ==============================================================================
@router.post("/run", summary="5. 4개 비교 참가자 시뮬레이션 백테스트 실행 (비동기 제출, #34)")
def run_simulation(
    req: SimulationRunRequest,
    user_id: int = Depends(get_current_user_id),
):
    """
    [대응 화면: y9DNLy]
    - 4개 대조군 봇의 독립 백테스트를 일별 이벤트 루프로 연산합니다.
    - #34: 무거운 연산(백테스트+몬테카를로+분석+리포트)은 더 이상 이 응답을 막지 않는다.
      가벼운 검증/캐시 조회만 동기로 처리하고 즉시 job 상태를 반환한다 — 캐시 미스면
      status: "RUNNING"(클라이언트는 GET /{id}/status를 폴링), 캐시 히트면 폴링 없이
      곧장 status: "COMPLETED". 실제 결과는 완료 후 GET /{id}로 읽는다
      (POST /bots/compile과 동일한 제출+폴링 패턴).
    """
    try:
        service = SimulationRunService(
            req, user_id, schedule_report_enrichment=_schedule_report_enrichment,
        )
        cached = service.run_sync_phase()
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

    if cached is not None:
        return {
            "simulationRunId": cached["simulationRunId"],
            "status": "COMPLETED",
            "progressPercent": 100,
            "message": "이미 완료된 시뮬레이션입니다.",
        }

    submit_simulation_run(service)
    return {
        "simulationRunId": service.db_run_id,
        "status": "RUNNING",
        "progressPercent": 10,
        "message": "시뮬레이션을 실행하고 있습니다.",
    }


# ==============================================================================
# 5-1. 시뮬레이션 비동기 실행 상태 조회
# ==============================================================================
@router.get("/{simulation_id}/status", summary="5-1. 시뮬레이션 실행 상태 비동기 조회")
def get_simulation_run_status(simulation_id: int, user_id: int = Depends(get_current_user_id)):
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
                (simulation_id, user_id),
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
@router.get("/history", summary="5-2. 과거 시뮬레이션 히스토리 목록 조회")
def get_simulation_history(user_id: int = Depends(get_current_user_id)):
    """
    [대응 화면: SimulationDashboard.vue 이력 목록]
    - 사용자가 실행했던 과거 시뮬레이션 회차별 히스토리 기록 목록을 반환합니다.
    """
    db_history = get_simulation_history_from_db(user_id=user_id)
    return db_history or []


# ==============================================================================
# 6. 최근 시뮬레이션 성과 조회 (대응 화면: xCJcT)
# ==============================================================================
@router.get("/latest", summary="6. 최근 시뮬레이션 성과 및 결과 조회")
async def get_latest_simulation(user_id: int = Depends(get_current_user_id)):
    """
    [대응 화면: xCJcT]
    - 가장 최근 실행된 시뮬레이션의 대시보드 성과 데이터를 조회합니다.
    - DB/캐시에 실행 기록이 없으면 404를 반환합니다.
    """
    # The in-memory cache is shared by the whole process, so its most recent
    # entry is whoever ran last on this server, not this user. The latest run
    # has to be resolved against the caller's own rows.
    max_id = await asyncio.to_thread(
        get_latest_completed_simulation_id_from_db,
        user_id,
    )
    if max_id is None:
        raise HTTPException(status_code=404, detail="저장된 시뮬레이션이 없습니다.")
    return await asyncio.to_thread(get_simulation_detail, max_id, user_id)


# ==============================================================================
# 7. 시뮬레이션 상세 조회 (대응 화면: p3vHxf, rGj4P, GTmqX)
# ==============================================================================
@router.get("/{simulation_id}", summary="7. 특정 시뮬레이션 상세 결과 조회")
def get_simulation_detail(simulation_id: int, user_id: int = Depends(get_current_user_id)):
    """
    [대응 화면: p3vHxf, rGj4P, GTmqX]
    - 특정 시뮬레이션 ID의 4개 봇 성과 비교, 일별 자산 그래프, 상세 체결 일지를 조회합니다.
    """
    _require_owned_simulation(simulation_id, user_id)
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
            # #34: POST /run이 더 이상 결과를 직접 응답하지 않으므로(비동기 job으로 전환),
            # run_async_phase가 측정한 단계별 소요시간(dataLoad/backtest/monteCarlo/analytics/
            # reportGeneration)을 확인할 수 있는 곳이 이 캐시 경유 상세 조회뿐이다. DB 재로드본
            # (db_detail)에는 애초에 이 필드가 없어 None으로 빠지는게 맞다 — 원래도 비영속 필드였다.
            "executionTimingMs": cached.get("executionTimingMs"),
            "excludedParticipants": cached.get("excludedParticipants", []),
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
            "executionTimingMs": db_detail.get("executionTimingMs"),
            "excludedParticipants": db_detail.get("excludedParticipants", []),
            **_analytics_response(db_detail),
        }

    raise HTTPException(status_code=404, detail=f"시뮬레이션 ID({simulation_id})가 존재하지 않습니다.")


# ==============================================================================
# 8. 새 결과 리포트 API (대응 화면: 리포트 탭 / 결과 복기)
# ==============================================================================
@router.get("/{simulation_id}/report", summary="8. AI 시뮬레이션 복기 및 결과 리포트 조회")
def get_simulation_report(
    simulation_id: int,
    user_id: int = Depends(get_current_user_id),
):
    """
    [대응 화면: 리포트 탭 / 결과 복기]
    - 1위가 누구냐에 따라 갈리는 결과(outcome)와 그 갈래가 쓰는 섹션만 반환합니다.
    - 저장본은 전체 분석을 유지하고, 화면이 읽는 형태로만 좁혀서 내보냅니다.
    """
    _require_owned_simulation(simulation_id, user_id)
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
                    simulation_id,
                    cached_report,
                )
            print(f"[Simulation Endpoint] simulation_id={simulation_id} 기존 저장된 report_json 반환")
            return deliverable(cached_report)

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
                simulation_id,
                report_data,
            )

        return deliverable(report_data) if report_data else report_data
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
