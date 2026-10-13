"""Application entrypoint: ``create_app()`` wiring for P3 (docs/05 §P3).

Composition order matters:

* ``install_error_handlers`` renders every typed failure as the §7.2 envelope;
* ``ApiMiddleware`` (trace id → body cap → rate limit → security headers →
  trace injection → ETag/304) is added **first**, so the CORS middleware
  added afterwards sits *outside* it — preflight short-circuits before rate
  limiting and every response (including 429s) carries
  ``Access-Control-Allow-Origin`` (docs/02 §3, S-06);
* the lifespan owns index bootstrap, the APScheduler start/stop (§14.1) and
  the L2 cache teardown, and never fails boot on a dependency outage (CF-14).

``app = create_app()`` stays module-global so ``uvicorn app.main:app`` and the
smoke tests keep working; tests call ``create_app(...)`` with their own
settings/db/provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.cache import response_cache
from app.core.config import Settings, get_settings
from app.core.deps import ProviderFactory
from app.core.errors import install_error_handlers
from app.core.middleware import ApiMiddleware
from app.core.ratelimit import RateLimiter
from app.db.indexes import ensure_indexes
from app.db.mongo import close, get_db, ping
from app.providers.open_meteo import OpenMeteoProvider
from app.routers import admin, analysis, client_errors, hazards, health, regions
from app.services.scheduler import SchedulerService

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Index bootstrap → scheduler start → serve → clean shutdown (§14.1).

    Mongo being down at boot is a warning, not a crash: liveness must stay
    independent of dependencies (docs/02 §7.1), and ``python -m app.cli
    indexes`` is the operational fallback.
    """
    settings: Settings = app.state.settings
    db_provider: Callable[[], Any] | None = getattr(app.state, "db_provider", None)
    db = db_provider() if db_provider is not None else get_db()
    try:
        ready = await asyncio.wait_for(ping(db.client), timeout=2.0)
    except TimeoutError:
        ready = False
    if ready:
        try:
            await ensure_indexes(db, daily_ttl_days=settings.daily_weather_ttl_days)
        except Exception as exc:  # noqa: BLE001 — boot must not fail on indexes
            logger.warning("could not ensure Mongo indexes at startup: %s", exc)
    else:
        logger.warning("Mongo unreachable at startup; run `python -m app.cli indexes` later")

    scheduler: SchedulerService | None = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        response_cache.clear()
        if db_provider is None:
            close()


def create_app(
    *,
    settings: Settings | None = None,
    db_provider: Callable[[], Any] | None = None,
    provider_factory: ProviderFactory | None = None,
) -> FastAPI:
    """Build a fully wired app instance (docs/05 P3 checklist)."""
    settings = settings or get_settings()
    app = FastAPI(
        title="Climate Hazard Trend Analyzer API",
        version="0.1.0",
        description="Historical climate hazard analysis (docs/02 API contracts)",
        lifespan=lifespan,
    )
    install_error_handlers(app)

    app.state.settings = settings
    if db_provider is not None:
        app.state.db_provider = db_provider
    factory: ProviderFactory = provider_factory or (lambda: OpenMeteoProvider(settings))
    app.state.provider_factory = factory
    app.state.limiter = RateLimiter(
        {
            "analysis": settings.rate_limit_analysis_per_min,
            "region": settings.rate_limit_region_per_min,
            "admin": settings.rate_limit_admin_per_min,
            "client_errors": settings.rate_limit_client_errors_per_min,
        }
    )
    app.state.response_cache = response_cache
    db = db_provider() if db_provider is not None else get_db()
    app.state.scheduler = SchedulerService(db, settings, provider_factory=factory)

    # Inner hop first, CORS second → CORS ends up outermost (see module doc).
    app.add_middleware(
        ApiMiddleware,
        limiter=app.state.limiter,
        max_body_bytes=settings.max_body_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Admin-Token", "If-None-Match"],
        expose_headers=[
            "X-Trace-Id",
            "ETag",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
        max_age=600,
    )

    app.include_router(health.router)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(client_errors.router, prefix=API_PREFIX)
    app.include_router(hazards.router, prefix=API_PREFIX)
    app.include_router(regions.router, prefix=API_PREFIX)
    app.include_router(analysis.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    return app


app = create_app()


__all__ = ["app", "create_app", "lifespan"]
