"""Region endpoints (docs/02 §7.3): presets, geocode proxy, create, status.

* ``GET  /regions/presets``   curated city/state list from ``presets.json``;
* ``GET  /regions/search``    bounded Open-Meteo geocode proxy (503 on
  upstream trouble — FE falls back to manual coordinates, CF-01);
* ``POST /regions``           idempotent create → backfill enqueued as a
  background task (single-flight, §12; TC-I-01/02);
* ``GET  /regions/{id}``      status polling for the CF-02 wait path.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path

from app.core.config import Settings
from app.core.deps import get_app_settings, get_region_service
from app.core.errors import ApiError
from app.models.api import REGION_ID_RE, RegionCreateBody, SearchQuery, presets_query, search_query
from app.providers.base import ProviderError
from app.routers import no_store, ok
from app.services.presets import list_presets
from app.services.region import RegionService

router = APIRouter(prefix="/regions", tags=["regions"])


@router.get("/presets")
async def presets(type: str | None = Depends(presets_query)) -> object:
    items = list_presets(type)
    return ok({"items": items, "total": len(items)}, "public, max-age=86400")


@router.get("/search")
async def search(
    query: SearchQuery = Depends(search_query),
    service: RegionService = Depends(get_region_service),
) -> object:
    try:
        items = await service.search(query.q)
    except ProviderError as exc:
        # docs/02 §7.3: the proxy surfaces upstream trouble as a *503*
        # PROVIDER_ERROR (retryable) even though the catalog maps that code
        # to 502 for analysis backfills.
        raise ApiError(
            "PROVIDER_ERROR",
            "search unavailable — use manual coordinates",
            status_code=503,
            retry_after_seconds=10,
        ) from exc
    return ok({"items": items, "total": len(items)}, "public, max-age=86400")


@router.post("")
async def create_region(
    body: RegionCreateBody,
    service: RegionService = Depends(get_region_service),
    settings: Settings = Depends(get_app_settings),
) -> Any:
    region_id, created = await service.create(body)
    status = (await service.status(region_id))["status"]
    payload: dict[str, Any] = {
        "region_id": region_id,
        "status": status,
        "status_url": f"/api/v1/regions/{region_id}",
    }
    if status == "backfilling":
        payload["poll_after_seconds"] = settings.poll_after_seconds
    return no_store(payload, status_code=201 if created else 200)


@router.get("/{region_id}")
async def region_status(
    region_id: str = Path(..., pattern=REGION_ID_RE.pattern),
    service: RegionService = Depends(get_region_service),
) -> object:
    # CF-02 poll target: `no-cache, max-age=0` forces the browser to
    # revalidate on every 5 s poll (cheap ETag 304) instead of serving a
    # stale `backfilling` from HTTP cache for up to 30 s after the region
    # turned ready — the ETag gate needs the `max-age` token to stay on.
    return ok(await service.status(region_id), "no-cache, max-age=0")


__all__ = ["router"]
