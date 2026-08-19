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
    API_V1_STR: str = "/api/v1"
    
    # API Keys
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    OPENDART_API_KEY: str = os.getenv("OPENDART_API_KEY", "")
    KRX_OPEN_API_KEY: str = os.getenv("KRX_OPEN_API_KEY", "")
    
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
    
    # Server Config
    PORT: int = int(os.getenv("PORT", "8000"))

settings = Settings()
