"""Application entrypoint (P0 skeleton + P1 data-layer boot).

Liveness (``/healthz``) never depends on Mongo — readiness (``/readyz``) and
routers arrive in P3 per docs/05-security-and-implementation-checklist.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close, get_db, ping

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Ensure schema validators + indexes exist before serving (docs/02 §6).

    Mongo being down at boot is a warning, not a crash: liveness must stay
    independent of dependencies (docs/02 §7.1), and ``python -m app.cli
    indexes`` is the operational fallback.
    """
    ready = False
    try:
        ready = await asyncio.wait_for(ping(), timeout=2.0)
    except TimeoutError:
        ready = False
    if ready:
        try:
            await ensure_indexes(get_db(), daily_ttl_days=get_settings().daily_weather_ttl_days)
        except Exception as exc:  # noqa: BLE001 — boot must not fail on indexes
            logger.warning("could not ensure Mongo indexes at startup: %s", exc)
    else:
        logger.warning("Mongo unreachable at startup; run `python -m app.cli indexes` later")
    yield
    close()


app = FastAPI(
    title="Climate Hazard Trend Analyzer API",
    version="0.1.0",
    description="Historical climate hazard analysis (docs/02 API contracts)",
    lifespan=lifespan,
)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness probe: process is up (docs/02 §7.1)."""
    return {"status": "ok"}
