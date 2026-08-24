"""
================================================================================
[Investory Unified FastAPI Main Application] app/main.py
================================================================================
■ 역할:
  - Investory 통합 AI 및 시뮬레이션 백엔드 서버의 메인 애플리케이션 진입점입니다.
  - CORS 설정, Swagger UI 문서화 정보, REST API 라우터 마운트 및 헬스체크를 담당합니다.
================================================================================
"""

from datetime import datetime, timezone
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.api.router import api_router
from app.core.metrics import HTTP_REQUEST_LATENCY_SECONDS, HTTP_REQUESTS_TOTAL

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Investory 개인 투자봇 시뮬레이션 & 6개 축 투자 성향 분석 통합 AI 백엔드 서버 API Documentation",
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc"
)


@app.middleware("http")
async def prometheus_metrics_middleware(request: Request, call_next):
    started = perf_counter()
    response = await call_next(request)
    # 라우팅 후에만 request.scope["route"]가 채워지므로, 매칭된 라우트의 path
    # 템플릿(예: "/simulations/{simulation_run_id}")을 label로 써서 path 파라미터마다
    # 별도 시계열이 생기는 카디널리티 폭발을 피한다. 매칭 실패(404 등)는 raw path로 남긴다.
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    labels = (request.method, path, str(response.status_code))
    HTTP_REQUESTS_TOTAL.labels(*labels).inc()
    HTTP_REQUEST_LATENCY_SECONDS.labels(*labels).observe(perf_counter() - started)
    return response


def _error_body(error_code: str, message: str, field_errors=None) -> dict:
    """The single error shape every endpoint answers with."""
    return {
        "errorCode": error_code,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fieldErrors": field_errors,
    }


@app.exception_handler(StarletteHTTPException)
def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Endpoints raise either a plain message or a dict carrying its own code.
    detail = exc.detail
    if isinstance(detail, dict):
        error_code = str(detail.get("code") or f"HTTP_{exc.status_code}")
        message = str(detail.get("message") or "")
    else:
        error_code = f"HTTP_{exc.status_code}"
        message = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(error_code, message),
    )


@app.exception_handler(RequestValidationError)
def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    field_errors = [
        {
            "field": ".".join(str(part) for part in item.get("loc", [])[1:]) or "body",
            "message": item.get("msg", ""),
        }
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", "요청 값을 확인해 주세요.", field_errors),
    )


# REST API 라우터 마운트
app.include_router(api_router, prefix=settings.API_PREFIX)

@app.on_event("startup")
def startup_event():
    try:
        from app.modules.simulation.collectors.batch_cron import start_batch_scheduler
        start_batch_scheduler(run_hour=16, run_minute=30)
    except Exception as e:
        print(f"[Startup Warning] Failed to launch batch scheduler: {e}")


@app.on_event("shutdown")
def shutdown_event():
    from app.modules.simulation.collectors.batch_cron import stop_batch_scheduler
    from app.modules.simulation.analytics.analytics import shutdown_monte_carlo_executor
    from app.api.endpoints.simulation_run_service import shutdown_run_executor

    stop_batch_scheduler()
    shutdown_monte_carlo_executor()
    shutdown_run_executor()

@app.get("/", summary="루트 웰컴 메시지")
def root_welcome():
    return {
        "status": "online",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs_url": "/docs",
        "message": "Investory Unified AI Backend Server is running successfully."
    }

@app.get("/health", summary="서버 헬스 체크")
def health_check():
    return {
        "status": "healthy",
        "compiler_model": settings.COMPILER_MODEL,
        "disclosure_model": settings.DISCLOSURE_MODEL
    }

@app.get("/metrics", summary="프로메테우스 메트릭")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# CORS 설정 (프론트엔드 연동)
#
# 일부러 app.add_middleware(CORSMiddleware, ...) 대신 앱 전체를 감싸는 방식을 쓴다.
# Starlette는 Exception/500 처리(ServerErrorMiddleware)를 사용자가 등록한 모든
# 미들웨어보다 바깥에 둔다 — 그래서 add_middleware로 넣은 CORS는 핸들링되지 않은
# 예외로 500이 나가는 응답에는 헤더를 못 붙인다. 이 경우 브라우저는 실제 500을
# "CORS 정책에 의해 차단됨"으로 보여줘서 진짜 원인을 가린다. ASGI 레벨에서 앱을
# 감싸면 ServerErrorMiddleware가 만드는 500 응답에도 CORS 헤더가 정상적으로 붙는다.
app = CORSMiddleware(
    app,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
