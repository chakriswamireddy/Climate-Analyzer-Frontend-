"""FastAPI dependency wiring (docs/02 §3: routers → services → pure functions).

Everything the HTTP layer needs is resolved here from ``app.state`` so tests
can swap the database, settings and provider factory per app instance without
touching a process-wide singleton.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.db.mongo import get_db
from app.providers.base import WeatherProvider
from app.providers.open_meteo import OpenMeteoProvider
from app.services.analysis import AnalysisService
from app.services.ingest import IngestService
from app.services.region import RegionService
from app.services.scheduler import SchedulerService

ProviderFactory = Callable[[], WeatherProvider]


def get_app_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    return settings or get_settings()


def get_app_db(request: Request) -> Any:
    provider = getattr(request.app.state, "db_provider", None)
    return provider() if provider is not None else get_db()


def get_provider_factory(request: Request) -> ProviderFactory:
    factory = getattr(request.app.state, "provider_factory", None)
    if factory is not None:
        return factory
    settings = get_app_settings(request)

    def _factory() -> WeatherProvider:
        return OpenMeteoProvider(settings)

    return _factory


def get_region_service(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    db: Any = Depends(get_app_db),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
) -> RegionService:
    return RegionService(db, settings, provider_factory=provider_factory)


def get_analysis_service(
    settings: Settings = Depends(get_app_settings),
    db: Any = Depends(get_app_db),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
) -> AnalysisService:
    return AnalysisService(db, settings, provider_factory=provider_factory)


def get_ingest_service(
    settings: Settings = Depends(get_app_settings),
    db: Any = Depends(get_app_db),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
) -> IngestService:
    """Ingest service bound to one provider instance for synchronous runs."""
    return IngestService(db, provider_factory(), settings=settings)


def get_scheduler(
    request: Request,
    settings: Settings = Depends(get_app_settings),
    db: Any = Depends(get_app_db),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
) -> SchedulerService:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        scheduler = SchedulerService(db, settings, provider_factory=provider_factory)
        request.app.state.scheduler = scheduler
    return scheduler


__all__ = [
    "ProviderFactory",
    "get_analysis_service",
    "get_app_db",
    "get_app_settings",
    "get_ingest_service",
    "get_provider_factory",
    "get_region_service",
    "get_scheduler",
]
