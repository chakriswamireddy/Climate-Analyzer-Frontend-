"""Error envelope + exception handlers (docs/02 §7.2, §8; 03 §6).

Every failure — validation, not-found, rate limit, provider, DB, crash — is
rendered through :func:`envelope`, so clients only ever see a stable machine
code, a human message, an optional per-field detail and the ``trace_id``.
Stack traces stay in the logs (S-12).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pymongo.errors import PyMongoError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.trace import get_trace_id

log = logging.getLogger(__name__)

# code → (HTTP status, retryable default) — the table in docs/02 §7.2.
CATALOG: dict[str, tuple[int, bool]] = {
    "BAD_REQUEST": (400, False),
    "UNAUTHORIZED": (401, False),
    "NOT_FOUND": (404, False),  # unknown *route* (region/event 404s are coded below)
    "REGION_NOT_FOUND": (404, False),
    "EVENT_NOT_FOUND": (404, False),
    "REGION_NOT_READY": (409, True),
    "VALIDATION_ERROR": (422, False),
    "RATE_LIMITED": (429, True),
    "INTERNAL_ERROR": (500, True),
    "PROVIDER_ERROR": (502, True),
    "SERVICE_UNAVAILABLE": (503, True),
    "TIMEOUT": (504, True),
}


class ApiError(Exception):
    """Typed application error → envelope (docs/02 §7.2)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        default_status, default_retryable = CATALOG.get(code, (500, True))
        self.code = code
        self.message = message
        self.status_code = status_code or default_status
        self.details = details
        self.retryable = default_retryable if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds


def envelope(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    retry_after_seconds: int | None = None,
    trace_id: str = "",
) -> dict[str, Any]:
    """The error body from docs/02 §7.2."""
    default_status, default_retryable = CATALOG.get(code, (500, True))
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "retryable": default_retryable if retryable is None else retryable,
            "retry_after_seconds": retry_after_seconds,
        },
        "trace_id": trace_id,
    }


def error_response(
    code: str,
    message: str,
    *,
    status_code: int | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    retry_after_seconds: int | None = None,
    trace_id: str = "",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    default_status, _ = CATALOG.get(code, (500, True))
    response_headers = dict(headers or {})
    if retry_after_seconds is not None:
        response_headers.setdefault("Retry-After", str(retry_after_seconds))
    return JSONResponse(
        status_code=status_code or default_status,
        content=envelope(
            code,
            message,
            details=details,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            trace_id=trace_id,
        ),
        headers=response_headers,
    )


def api_error_response(error: ApiError, request: Request) -> JSONResponse:
    return error_response(
        error.code,
        error.message,
        status_code=error.status_code,
        details=error.details,
        retryable=error.retryable,
        retry_after_seconds=error.retry_after_seconds,
        trace_id=get_trace_id(request),
    )


def _validation_details(error: dict[str, Any]) -> dict[str, Any] | None:
    """``loc`` → ``{"field": …, "reason": …}`` (TC-I-09 asserts ``field``)."""
    loc = [part for part in error.get("loc", ()) if part not in ("body", "query", "path")]
    reason = str(error.get("msg", "")).removeprefix("Value error, ")
    if not loc:
        return {"reason": reason}
    return {"field": str(loc[-1]), "reason": reason}


def install_error_handlers(app: FastAPI) -> None:
    """Register the §7.2 envelope handlers on ``app``."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return api_error_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = list(exc.errors())
        first = errors[0] if errors else {"msg": "invalid request", "loc": ()}
        messages = [str(item.get("msg", "")).removeprefix("Value error, ") for item in errors[:5]]
        return error_response(
            "VALIDATION_ERROR",
            "; ".join(dict.fromkeys(messages)),
            details=_validation_details(first),
            trace_id=get_trace_id(request),
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_validation(request: Request, exc: ValidationError) -> JSONResponse:
        errors: list[dict[str, Any]] = exc.errors()
        first = errors[0] if errors else {"msg": "invalid payload", "loc": ()}
        return error_response(
            "VALIDATION_ERROR",
            str(first.get("msg", "invalid payload")).removeprefix("Value error, "),
            details=_validation_details(first),
            trace_id=get_trace_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            404: "NOT_FOUND",
            405: "BAD_REQUEST",
            413: "BAD_REQUEST",
            429: "RATE_LIMITED",
            503: "SERVICE_UNAVAILABLE",
        }.get(exc.status_code, "INTERNAL_ERROR")
        headers = dict(getattr(exc, "headers", None) or {})
        return error_response(
            code,
            str(exc.detail),
            status_code=exc.status_code,
            trace_id=get_trace_id(request),
            headers=headers,
        )


def crash_response(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort 500/503 rendered by the middleware (never a stack trace)."""
    if isinstance(exc, PyMongoError):
        # CF-14: DB trouble is a retryable 503, not an opaque crash.
        log.warning("mongo failure while serving request: %s", exc)
        return error_response(
            "SERVICE_UNAVAILABLE",
            "Service temporarily unavailable — please retry",
            retry_after_seconds=10,
            trace_id=get_trace_id(request),
        )
    log.exception("unhandled error while serving %s %s", request.method, request.url.path)
    return error_response(
        "INTERNAL_ERROR",
        "An unexpected error occurred — quote the trace id when reporting it",
        trace_id=get_trace_id(request),
    )


__all__ = [
    "ApiError",
    "CATALOG",
    "api_error_response",
    "crash_response",
    "envelope",
    "error_response",
    "install_error_handlers",
]
