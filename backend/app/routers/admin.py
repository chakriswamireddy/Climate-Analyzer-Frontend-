"""Admin endpoints (docs/02 §7.5, DEC-06, V-14).

Static bearer token from ``ADMIN_API_TOKEN``:

* exactly one ``X-Admin-Token`` header value;
* compared with ``hmac.compare_digest`` (timing-safe);
* missing/invalid/misconfigured → ``401 UNAUTHORIZED`` envelope;
* token never logged or echoed.

Rate limiting (5/min) is enforced by the middleware on ``/admin/*`` (§11).
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings
from app.core.deps import get_app_db, get_app_settings, get_region_service
from app.core.errors import ApiError
from app.db.repositories import IngestRunRepository
from app.models.api import AdminRunsQuery, RecomputeBody, admin_runs_query
from app.models.ingest import IngestTrigger
from app.routers import no_store
from app.services.ingest import IngestAlreadyRunning
from app.services.region import RegionService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(request: Request, settings: Settings = Depends(get_app_settings)) -> None:
    """V-14: single header value, constant-time compare, stable 401."""
    values = request.headers.getlist("x-admin-token")
    expected = settings.admin_api_token
    if len(values) != 1 or not expected or not hmac.compare_digest(values[0], expected):
        raise ApiError("UNAUTHORIZED", "missing or invalid admin token")


@router.post("/recompute", dependencies=[Depends(require_admin)])
async def recompute(
    body: RecomputeBody,
    service: RegionService = Depends(get_region_service),
    settings: Settings = Depends(get_app_settings),
) -> Any:
    if await service.regions.get(body.region_id) is None:
        raise ApiError("REGION_NOT_FOUND", f"unknown region {body.region_id}")
    if body.rebuild_thresholds:
        await service.db["threshold_cache"].delete_many(
            {"region_id": body.region_id, "algo_version": settings.algo_version}
        )
        log.info("threshold cache cleared for %s", body.region_id)
    try:
        run_id = await service.backfill_now(body.region_id, trigger=IngestTrigger.ADMIN)
    except IngestAlreadyRunning as exc:
        raise ApiError(
            "REGION_NOT_READY",
            "an ingest run is already in flight for this region",
            retry_after_seconds=settings.poll_after_seconds,
        ) from exc
    return no_store(
        {"run_id": str(run_id), "region_id": body.region_id, "status": "queued"},
        status_code=202,
    )


@router.get("/ingest-runs", dependencies=[Depends(require_admin)])
async def ingest_runs(
    query: AdminRunsQuery = Depends(admin_runs_query),
    db: Any = Depends(get_app_db),
) -> object:
    runs = IngestRunRepository(db)
    total = await runs.count(query.region_id)
    docs = await runs.page(
        region_id=query.region_id,
        skip=(query.page - 1) * query.page_size,
        limit=query.page_size,
    )
    return no_store(
        {
            "items": [_run_out(doc) for doc in docs],
            "page": query.page,
            "page_size": query.page_size,
            "total": total,
        }
    )


def _run_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(doc["_id"]),
        "region_id": doc.get("region_id"),
        "window": doc.get("window"),
        "status": doc.get("status"),
        "attempts": doc.get("attempts"),
        "trigger": doc.get("trigger"),
        "counts": doc.get("counts") or {},
        "error": doc.get("error"),
        "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "duration_ms": doc.get("duration_ms"),
    }


__all__ = ["require_admin", "router"]
