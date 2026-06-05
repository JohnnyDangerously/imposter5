from __future__ import annotations

from typing import Any
from fastapi.responses import JSONResponse


def error_response(
    error_code: str,
    message: str,
    status_code: int = 400,
    **extra: Any,
) -> JSONResponse:
    content = {
        "ok": False,
        "error": error_code,
        "message": message,
        **extra,
    }
    return JSONResponse(content=content, status_code=status_code)


def success_response(
    data: dict[str, Any] | None = None,
    status_code: int = 200,
    **extra: Any,
) -> JSONResponse:
    content = {"ok": True}
    if data:
        content.update(data)
    if extra:
        content.update(extra)
    return JSONResponse(content=content, status_code=status_code)


def unauthorized(message: str = "Authentication required") -> JSONResponse:
    return error_response("unauthorized", message, 401)
