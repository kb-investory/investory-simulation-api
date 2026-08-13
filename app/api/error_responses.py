"""Safe API error responses with server-side correlation logging."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException


def internal_server_error(
    logger: logging.Logger,
    error: Exception,
    *,
    code: str,
    message: str,
    **safe_context: Any,
) -> HTTPException:
    """Log the original exception and return a non-sensitive 500 response."""
    error_id = uuid.uuid4().hex[:12]
    logger.error(
        "Unhandled API error error_id=%s code=%s context=%s",
        error_id,
        code,
        safe_context,
        exc_info=(type(error), error, error.__traceback__),
    )
    return HTTPException(
        status_code=500,
        detail={
            "code": code,
            "message": message,
            "errorId": error_id,
        },
    )
