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
    COMPILER_MODEL: str = os.getenv("COMPILER_MODEL", "gpt-4o-mini")
    DISCLOSURE_MODEL: str = os.getenv("DISCLOSURE_MODEL", "gpt-4o-mini")
    REPORT_MODEL: str = os.getenv("REPORT_MODEL", "gpt-4o-mini")
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "30"))
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
