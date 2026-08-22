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

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.api.router import api_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Investory 개인 투자봇 시뮬레이션 & 6개 축 투자 성향 분석 통합 AI 백엔드 서버 API Documentation",
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc"
)


# CORS 설정 (프론트엔드 연동)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

    stop_batch_scheduler()

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
