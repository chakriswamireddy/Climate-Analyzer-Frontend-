"""HTTP routers (docs/02 §7): health, hazards, regions, analysis, admin,
client-error beacon.

Routers stay thin: validate via dependency models (``app.models.api``),
delegate to services (``app.services.*``), shape the documented payload.
Middleware owns trace ids, rate limits, security headers and ETags.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

__all__ = ["no_store", "ok"]


def ok(payload: dict[str, Any], cache_control: str) -> JSONResponse:
    """Success JSON with an explicit ``Cache-Control`` (drives L3 ETag/304)."""
    return JSONResponse(content=payload, headers={"Cache-Control": cache_control})


def no_store(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """Uncacheable JSON: health, admin, POSTs (docs/02 §10)."""
    return JSONResponse(
        content=payload, status_code=status_code, headers={"Cache-Control": "no-store"}
    )
