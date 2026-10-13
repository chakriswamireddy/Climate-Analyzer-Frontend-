"""The single API middleware: trace id → body cap → rate limit → security
headers → JSON trace injection → ETag/304 (docs/02 §7.2, §8 V-15, §10, §11;
05 §1.1).

It is deliberately one pure-ASGI hop that buffers each response:

* it can render the error envelope for *anything* the app raises (CF-14,
  S-12) without a stack trace ever reaching the client;
* it can rewrite the body (inject ``trace_id``) and recompute
  ``Content-Length`` before a single byte leaves the process;
* ETag is computed on the fully assembled body (EC-16) after dropping the
  per-request fields, so a repeated GET is byte-stable → ``304`` (TC-I-10).

Ordering: CORS is installed *outside* this middleware (see ``create_app``) so
preflight requests short-circuit before rate limiting and every response —
including 429s — carries ``Access-Control-Allow-Origin``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request

from app.core.cache import etag_for
from app.core.errors import crash_response, error_response
from app.core.ratelimit import RateDecision, RateLimiter, classify
from app.core.trace import begin, end

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}
API_CSP = "default-src 'none'"
_MUTATING = {"POST", "PUT", "PATCH"}


class ApiMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        limiter: RateLimiter,
        max_body_bytes: int = 10_240,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id, token = begin(scope)
        rate_headers: dict[str, str] = {}
        try:
            request = Request(scope)
            chunks: list[dict] = []
            try:
                short = self._short_circuit(request, rate_headers)
                if short is not None:
                    chunks = short
                else:
                    inner_send = _collector(chunks)
                    await self.app(scope, receive, inner_send)
            except Exception as exc:  # noqa: BLE001 — envelope, never a stack trace
                chunks = _raw(crash_response(request, exc))
            await self._emit(request, send, chunks, trace_id, rate_headers)
        finally:
            end(token)

    # -- request side ------------------------------------------------------

    def _short_circuit(self, request: Request, rate_headers: dict[str, str]) -> list[dict] | None:
        # V-15: JSON bodies ≤ 10 KB (Content-Length guard; chunked bodies are
        # bounded by the same rule once FastAPI parses them via Pydantic).
        if request.method in _MUTATING:
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_body_bytes:
                return _raw(
                    error_response(
                        "BAD_REQUEST",
                        f"request body too large (limit {self.max_body_bytes} bytes)",
                        status_code=413,
                        trace_id=_scope_trace(request),
                    )
                )

        bucket_class = classify(request.url.path, request.method)
        if bucket_class is None:
            return None
        decision = self.limiter.check(_client_ip(request), bucket_class)
        rate_headers.update(_rate_headers(decision))
        if not decision.allowed:
            return _raw(
                error_response(
                    "RATE_LIMITED",
                    f"rate limit exceeded ({decision.limit}/min) — retry shortly",
                    retry_after_seconds=decision.retry_after_seconds,
                    trace_id=_scope_trace(request),
                    headers={"Retry-After": str(decision.retry_after_seconds or 1)},
                )
            )
        return None

    # -- response side -----------------------------------------------------

    async def _emit(
        self,
        request: Request,
        send: Any,
        chunks: list[dict],
        trace_id: str,
        rate_headers: dict[str, str],
    ) -> None:
        if not chunks:
            return
        start = next((m for m in chunks if m["type"] == "http.response.start"), None)
        if start is None:
            return
        status = int(start["status"])
        headers: dict[str, bytes] = {}
        order: list[str] = []
        for name, value in start.get("headers", []):
            key = name.decode("latin-1").lower()
            if key not in headers:
                order.append(key)
            headers[key] = value
        body = b"".join(m.get("body", b"") for m in chunks if m["type"] == "http.response.body")

        body = self._inject_trace(headers, body, trace_id)
        self._apply_security(request, headers, order)
        for name, value in rate_headers.items():
            _set(headers, order, name, value.encode("latin-1"))
        _set(headers, order, "x-trace-id", trace_id.encode("latin-1"))

        if request.method == "GET" and status == 200:
            body, status = self._maybe_not_modified(request, headers, order, body)

        content_type = headers.get("content-type", b"").decode("latin-1")
        if "json" in content_type:
            _set(headers, order, "content-length", str(len(body)).encode("latin-1"))
        if status == 304:
            headers.pop("content-length", None)
            order = [key for key in order if key != "content-length"]
            body = b""

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode("latin-1"), headers[k]) for k in order],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _inject_trace(self, headers: dict[str, bytes], body: bytes, trace_id: str) -> bytes:
        """Put ``trace_id`` in every JSON body that lacks it (docs/02 §7.2)."""
        if not _is_json(headers) or not body:
            return body
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body
        if not isinstance(payload, dict) or "trace_id" in payload:
            return body
        payload["trace_id"] = trace_id
        return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

    def _apply_security(
        self, request: Request, headers: dict[str, bytes], order: list[str]
    ) -> None:
        path = request.url.path
        is_api = path.startswith("/api/v1") or path in ("/healthz", "/readyz")
        if not is_api:
            return
        for name, value in SECURITY_HEADERS.items():
            _set(headers, order, name, value.encode("latin-1"))
        _set(headers, order, "content-security-policy", API_CSP.encode("latin-1"))

    def _maybe_not_modified(
        self, request: Request, headers: dict[str, bytes], order: list[str], body: bytes
    ) -> tuple[bytes, int]:
        """L3 HTTP caching: strong ETag + 304 on ``If-None-Match`` (§10)."""
        cache_control = headers.get("cache-control", b"").decode("latin-1")
        if "max-age" not in cache_control or not _is_json(headers) or not body:
            return body, 200
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body, 200
        if not isinstance(payload, dict):
            return body, 200
        etag = etag_for(payload)
        _set(headers, order, "etag", etag.encode("latin-1"))
        if _matches(request.headers.get("if-none-match", ""), etag):
            return b"", 304
        return body, 200


def _collector(sink: list[dict]):
    async def send(message: dict) -> None:
        sink.append(message)

    return send


def _raw(response: Any) -> list[dict]:
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in response.headers.items()
    ]
    return [
        {"type": "http.response.start", "status": response.status_code, "headers": headers},
        {"type": "http.response.body", "body": response.body},
    ]


def _is_json(headers: dict[str, bytes]) -> bool:
    return "json" in headers.get("content-type", b"").decode("latin-1")


def _set(headers: dict[str, bytes], order: list[str], name: str, value: bytes) -> None:
    if name not in headers:
        order.append(name)
    headers[name] = value


def _matches(if_none_match: str, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = [part.strip() for part in if_none_match.split(",")]
    return (
        "*" in candidates
        or etag in candidates
        or etag.strip("W/") in {item.strip('W/"') for item in candidates}
    )


def _client_ip(request: Request) -> str:
    """First ``X-Forwarded-For`` hop — the documented trust boundary (§11)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    client = request.scope.get("client")
    return client[0] if client else "unknown"


def _scope_trace(request: Request) -> str:
    state = request.scope.get("state") or {}
    return str(state.get("trace_id", ""))


def _rate_headers(decision: RateDecision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(max(decision.remaining, 0)),
        "X-RateLimit-Reset": str(decision.reset_seconds),
    }


__all__ = ["ApiMiddleware"]
