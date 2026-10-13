"""Liveness + readiness probes (docs/02 §7.1; CF-14).

``/healthz`` — process up, never touches Mongo (NFR availability).
``/readyz``  — Mongo ping within a 2 s budget + scheduler heartbeat; 503 with
the retryable envelope when either is down so the platform stops routing
(TC-I-14).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings
from app.core.deps import get_app_db, get_app_settings
from app.core.errors import ApiError
from app.db.mongo import ping
from app.routers import no_store

router = APIRouter(tags=["ops"])

HEARTBEAT_LIMIT_S = 900.0  # sweeper runs every 5 min; 15 min = dead


@router.get("/healthz")
async def healthz() -> Any:
    """Liveness probe: the process answers (docs/02 §7.1, ``no-store``)."""
    return no_store({"status": "ok"})


@router.get("/readyz")
async def readyz(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> Any:
    """Readiness: Mongo ping (2 s) + scheduler heartbeat (§7.1, TC-I-14)."""
    db = get_app_db(request)
    try:
        mongo_ok = await asyncio.wait_for(ping(db.client), timeout=2.0)
    except TimeoutError:
        mongo_ok = False
    if not mongo_ok:
        raise ApiError(
            "SERVICE_UNAVAILABLE",
            "MongoDB unreachable — please retry shortly",
            retry_after_seconds=10,
        )

    heartbeat: str | None = None
    if settings.scheduler_enabled:
        scheduler = getattr(request.app.state, "scheduler", None)
        age = scheduler.heartbeat_age_s() if scheduler is not None else None
        if age is None or age > HEARTBEAT_LIMIT_S:
            raise ApiError(
                "SERVICE_UNAVAILABLE",
                "Background scheduler is not responding — please retry shortly",
                retry_after_seconds=10,
            )
        heartbeat = scheduler.last_heartbeat

    return no_store(
        {
            "status": "ready",
            "mongo": "ok",
            "scheduler": "ok" if settings.scheduler_enabled else "disabled",
            "scheduler_heartbeat": heartbeat,
        }
    )


__all__ = ["router"]
