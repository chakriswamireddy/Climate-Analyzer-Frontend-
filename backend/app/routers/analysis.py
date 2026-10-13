"""Analysis GETs (docs/02 §7.4, FR-04/05/07/10/12).

Thin wrappers: FastAPI validates via the shared query dependencies (V-01..V-13
→ 422), :class:`~app.services.analysis.AnalysisService` does the work, and the
response carries the §7.1 cache directives that switch on the middleware's
strong-ETag/304 path (§10, TC-I-10).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_analysis_service
from app.models.api import (
    AnalysisQuery,
    EventsQuery,
    InspectQuery,
    analysis_query,
    events_query,
    inspect_query,
)
from app.routers import ok
from app.services.analysis import AnalysisService

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/timeseries")
async def timeseries(
    query: AnalysisQuery = Depends(analysis_query),
    service: AnalysisService = Depends(get_analysis_service),
) -> object:
    return ok(await service.timeseries(query), "public, max-age=300")


@router.get("/summary")
async def summary(
    query: AnalysisQuery = Depends(analysis_query),
    service: AnalysisService = Depends(get_analysis_service),
) -> object:
    return ok(await service.summary(query), "public, max-age=300")


@router.get("/events")
async def events(
    query: EventsQuery = Depends(events_query),
    service: AnalysisService = Depends(get_analysis_service),
) -> object:
    return ok(await service.events_page(query), "public, max-age=300")


@router.get("/inspect")
async def inspect(
    query: InspectQuery = Depends(inspect_query),
    service: AnalysisService = Depends(get_analysis_service),
) -> object:
    return ok(await service.inspect(query), "public, max-age=3600")


__all__ = ["router"]
