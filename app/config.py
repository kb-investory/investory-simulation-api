"""
================================================================================
[Investory Application Config] app/config.py
================================================================================
■ 역할:
  - .env 및 .env.local 환경 변수(API Key, 포트, LLM 모델 설정)를 통합 로드하고 전역 설정 객체(settings)를 제공합니다.
================================================================================
"""

import os
from dotenv import load_dotenv

# 로컬 환경 변수 파일 (.env.local 우선, 없으면 .env)
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(root_dir, ".env.local"))
load_dotenv(os.path.join(root_dir, ".env"))
load_dotenv()

class Settings:
    PROJECT_NAME: str = "Investory AI Backend Server"
    VERSION: str = "1.0.0"
    API_PREFIX: str = "/simulation"
    
    # API Keys
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENDART_API_KEY: str = os.getenv("OPENDART_API_KEY", "")
    KRX_OPEN_API_KEY: str = os.getenv("KRX_OPEN_API_KEY", "")

    # kbinvestory-backend가 발급하는 RS256 액세스 토큰의 검증 전용 JWKS 엔드포인트.
    # 이 서비스는 토큰을 발급하지 않는다 — PyJWKClient로 공개키만 가져와 서명을 검증한다.
    AUTH_JWKS_URL: str = os.getenv("AUTH_JWKS_URL", "")

    # LLM Model Configurations
    #
    # Two tiers, chosen by what the task actually needs:
    #
    #   REASONING_MODEL - multi-step judgment where being wrong is expensive:
    #     mapping a sentence onto an executable rule, searching the web and
    #     deciding whether a thesis held. Spends far more output tokens on
    #     reasoning and runs slower, so only tasks that are already asynchronous
    #     should use it.
    #
    #   FAST_MODEL - high-volume classification and prose where the facts are
    #     already fixed by the deterministic engine. Latency and cost dominate
    #     because these run once per disclosure or once per report.
    REASONING_MODEL: str = os.getenv("REASONING_MODEL", "gpt-5-nano")
    FAST_MODEL: str = os.getenv("FAST_MODEL", "gpt-4o-mini")

    # Natural language principle -> executable rule JSON. Runs behind an async
    # compile job, and every later principle evaluation depends on this mapping.
    COMPILER_MODEL: str = os.getenv("COMPILER_MODEL") or REASONING_MODEL
    # Thesis verification is two different jobs and they do not want the same
    # model. Gathering dated sources is retrieval, and the reasoning tier spends
    # a minute and a half on it for no better result. Deciding whether those
    # sources actually support the user's thesis is the judgment, and there the
    # reasoning tier is measurably more willing to answer "not confirmed"
    # instead of inflating a single article into a realized thesis.
    # Verification costs two model calls per key trade, one of them a web
    # search, and the result currently has nowhere to appear: the response
    # contract carries no thesisOutcome. It stays off until a screen reads it.
    EVIDENCE_VERIFICATION_ENABLED: bool = (
        os.getenv("EVIDENCE_VERIFICATION_ENABLED", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    THESIS_VERIFICATION_MODEL: str = os.getenv("THESIS_VERIFICATION_MODEL", "")
    EVIDENCE_SEARCH_MODEL: str = (
        os.getenv("EVIDENCE_SEARCH_MODEL") or THESIS_VERIFICATION_MODEL or FAST_MODEL
    )
    EVIDENCE_JUDGMENT_MODEL: str = (
        os.getenv("EVIDENCE_JUDGMENT_MODEL") or THESIS_VERIFICATION_MODEL or REASONING_MODEL
    )
    # One short classification per disclosure, batched over many rows.
    DISCLOSURE_MODEL: str = os.getenv("DISCLOSURE_MODEL") or FAST_MODEL
    # Explanatory prose only; judgments and numbers are never taken from it.
    REPORT_MODEL: str = os.getenv("REPORT_MODEL") or FAST_MODEL

    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "30"))
    # Reasoning models interleave several tool calls before answering, so they
    # need both a longer wall clock and room to finish. Without the token budget
    # the response comes back "incomplete" with reasoning but no answer.
    REASONING_LLM_TIMEOUT: int = int(os.getenv("REASONING_LLM_TIMEOUT", "120"))
    REASONING_MAX_OUTPUT_TOKENS: int = int(os.getenv("REASONING_MAX_OUTPUT_TOKENS", "8000"))
    MAX_LLM_CALLS_PER_RUN: int = int(os.getenv("MAX_LLM_CALLS_PER_RUN", "50"))

    
    # Database Config
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "dev.investory.kr")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "investory")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB: str = os.getenv("MYSQL_DB", "investory")
    
    @property
    def DATABASE_URL(self) -> str:
        env_url = os.getenv("DATABASE_URL")
        if env_url and "localhost:5432" not in env_url:
            return env_url
        return f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"

    # SQLAlchemy pooled-connection settings (#29) — get_db_connection()이 이 풀에서 pymysql
    # 커넥션을 빌려온다. 동시 요청 상한은 스레드풀 크기(uvicorn 기본 워커 스레드, asyncio.to_thread
    # 기본 executor)와도 맞물리므로, 그 값들과 같이 조정할 것.
    #
    # 기본값 30+20=50은 #31 로컬 격리 환경 실측(50명 동시 POST /simulation/run) 기반이다 —
    # 10+10=20이던 초기값은 커넥션을 짧게 반납하도록 고친 뒤에도 50명 동시 피크를 못 받아
    # 40%가 QueuePool timeout으로 실패했고, 30+20으로 올리자 50/50 전부 성공했다. MySQL
    # max_connections(로컬 151)에서 다른 서비스 몫을 빼고도 여유가 있는지 배포 대상 DB 기준으로
    # 재확인할 것 — 특히 Cloud SQL처럼 max_connections가 인스턴스 티어에 따라 훨씬 낮은 환경.
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "30"))
    DB_POOL_MAX_OVERFLOW: int = int(os.getenv("DB_POOL_MAX_OVERFLOW", "20"))
    DB_POOL_RECYCLE_SECONDS: int = int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800"))

    # Server Config
    PORT: int = int(os.getenv("PORT", "8000"))

settings = Settings()
