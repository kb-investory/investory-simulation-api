"""
================================================================================
[Auth] app/core/auth.py
================================================================================
■ 역할:
  - kbinvestory-backend가 RS256으로 서명한 액세스 토큰을 JWKS 공개키로 검증하고,
    `sub` 클레임에서 userId를 추출하는 FastAPI 의존성(get_current_user_id)을 제공합니다.
  - 이 서비스는 토큰을 발급하지 않습니다 — 검증 전용입니다 (#3, #4 참고).
================================================================================
"""

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import settings

# PyJWKClient가 키 fetch·캐싱·로테이션을 알아서 처리한다 — 매 요청마다 새로 안 받아옴.
# AUTH_JWKS_URL이 아직 비어있는 환경(로컬/CI, #3 선행 의존성)에서도 앱이 뜰 수 있도록
# 생성 자체는 첫 인증 요청이 들어올 때까지 미룬다 — PyJWKClient는 생성 시점에 URL 스킴을
# 즉시 검증하므로, 빈 문자열로 모듈 임포트 시점에 생성하면 앱 기동 자체가 실패한다.
_jwk_client: PyJWKClient | None = None
_bearer = HTTPBearer(auto_error=False)


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(settings.AUTH_JWKS_URL)
    return _jwk_client


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> int:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다.")

    token = credentials.credentials
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(token, signing_key.key, algorithms=["RS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효하지 않은 토큰입니다.")

    if payload.get("tokenType") != "ACCESS":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "액세스 토큰이 아닙니다.")

    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰에 사용자 정보가 없습니다.")
