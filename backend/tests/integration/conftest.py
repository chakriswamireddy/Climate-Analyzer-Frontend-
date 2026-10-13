"""Fixtures + helpers for the P3 API integration tests (docs/04 §3, TC-I-*).

Every test gets:

* the shared ``mongo_db`` / ``settings`` fixtures from ``tests/conftest.py``;
* a fresh app (``create_app``) wired to that database, test settings and a
  **scripted provider** — deterministic, delayable, failure-injectable, with
  a shared call log so single-flight assertions are possible;
* a clean L2 (response + geocode) cache and rate-limit buckets per test.

``seed_region`` writes ready-region fixtures (annual stats + events) directly
so read-path tests never depend on a full ingest.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from app.core.cache import response_cache
from app.core.config import Settings
from app.main import create_app
from app.models.region import point_spec
from app.providers.base import FetchResult
from app.providers.geocoding import geocode_cache
from app.services.presets import clear_cache as clear_presets
from app.services.region import RegionBuilder
from tests.fixtures.series_factory import make_readings

HYDERABAD = point_spec(17.385, 78.4867)
ADMIN_TOKEN = "test-admin-token"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


class ScriptedProvider:
    """Weather provider double driven by a per-test state dict.

    ``state`` keys: ``delay`` (seconds), ``fail`` (exception to raise),
    ``gap_years`` (years dropped from the response), ``calls`` (window log).
    """

    source = "open-meteo-archive"

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def fetch_daily(self, spec, start: date, end: date) -> FetchResult:
        state = self.state
        state["calls"].append((start.isoformat(), end.isoformat()))
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["fail"] is not None:
            raise state["fail"]
        gap_years: set[int] = state["gap_years"]
        readings = [r for r in make_readings(start, end) if int(r.date[:4]) not in gap_years]
        return FetchResult(readings=readings, rows_in=len(readings), attempts=1)

    async def aclose(self) -> None:  # pragma: no cover — parity with real client
        return None


@pytest.fixture
def provider_factory():
    """Factory whose shared state each test configures before the call."""
    state: dict[str, Any] = {
        "delay": 0.0,
        "fail": None,
        "gap_years": set(),
        "calls": [],
    }

    def factory() -> ScriptedProvider:
        return ScriptedProvider(state)

    factory.state = state
    return factory


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    """Test API settings: short backfill window, scheduler off, admin token."""
    return settings.model_copy(
        update={
            "initial_backfill_start": "2016-01-01",
            "scheduler_enabled": False,
            "admin_api_token": ADMIN_TOKEN,
            "ondemand_wait_seconds": 8,
        }
    )


@pytest.fixture
async def app(api_settings: Settings, mongo_db, provider_factory):
    response_cache.clear()
    geocode_cache.clear()
    clear_presets()
    application = create_app(
        settings=api_settings,
        db_provider=lambda: mongo_db,
        provider_factory=provider_factory,
    )
    async with application.router.lifespan_context(application):
        yield application
    response_cache.clear()
    geocode_cache.clear()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=60.0
    ) as async_client:
        yield async_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


# -- seeding helpers ---------------------------------------------------------


async def seed_region(
    db,
    *,
    years: range | list[int],
    kind: str = "city",
    name: str = "Hyderabad",
    spec=None,
    algo_version: str = "1.0.0",
    data_version: str = "dv_1",
    coverage_pct: float = 99.5,
    coverage_overrides: dict[int, float] | None = None,
    events: int = 0,
    status: str = "ready",
    days_per_year: int = 6,
) -> str:
    """Insert a ready region with annual stats (and optional events)."""
    spec = spec or HYDERABAD
    region_id, _ = await RegionBuilder(db).ensure(kind=kind, name=name, spec=spec)
    years = list(years)
    overrides = coverage_overrides or {}
    await db["regions"].update_one(
        {"_id": region_id},
        {
            "$set": {
                "status": status,
                "data_version": data_version,
                "coverage": {
                    "start_year": min(years),
                    "end_year": max(years),
                    "years": len(years),
                },
            }
        },
    )
    stats = [
        {
            "region_id": region_id,
            "hazard": "heatwave",
            "year": year,
            "event_count": 1,
            "heatwave_days": days_per_year + (index // 3),
            "mean_duration": 3.5,
            "max_duration": 5,
            "mean_exceedance": 1.2,
            "coverage_pct": overrides.get(year, coverage_pct),
            "algo_version": algo_version,
            "data_version": data_version,
            "computed_at": "2026-01-01T00:00:00Z",
        }
        for index, year in enumerate(years)
    ]
    if stats:
        await db["hazard_annual_stats"].insert_many(stats)
    if events:
        await db["hazard_events"].insert_many(
            _events(region_id, events, algo_version=algo_version, data_version=data_version)
        )
    return region_id


def _events(
    region_id: str, count: int, *, algo_version: str, data_version: str
) -> list[dict[str, Any]]:
    """``count`` events starting 5 May each year, ~3 per year, varied lengths."""
    docs: list[dict[str, Any]] = []
    for index in range(count):
        year = 1990 + index // 3
        month_offset = index % 3
        start = date(year, 5, 5) + timedelta(days=10 * month_offset)
        duration = 3 + (index % 4)
        docs.append(
            {
                "region_id": region_id,
                "hazard": "heatwave",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=duration - 1)).isoformat(),
                "year": year,
                "duration_days": duration,
                "mean_exceedance": 1.0 + (index % 5) / 10,
                "peak_exceedance": 2.0 + (index % 5) / 10,
                "algo_version": algo_version,
                "data_version": data_version,
            }
        )
    return docs


def invalid_cases() -> list[tuple[str, dict[str, Any], str]]:
    """(endpoint, query params, expected ``details.field``) for V-01..V-16."""
    base = {"region_id": "r_" + "0" * 16}
    year = date.today().year
    return [
        ("timeseries", {**base, "start_year": 2000, "end_year": 1990}, "start_year"),
        ("timeseries", {**base, "start_year": 1949}, "start_year"),
        ("timeseries", {**base, "end_year": year}, "end_year"),
        ("timeseries", {**base, "hazard": "asteroid"}, "hazard"),
        ("timeseries", {"region_id": "not-a-region"}, "region_id"),
        ("timeseries", {**base, "method": "tea leaves"}, "method"),
        ("events", {**base, "page": 0}, "page"),
        ("events", {**base, "page_size": 101}, "page_size"),
        ("events", {**base, "min_duration": 0}, "min_duration"),
        ("events", {**base, "sort": "year:desc"}, "sort"),
    ]


async def settle(timeout: float = 60.0) -> None:
    """Wait for every tracked background task (backfills, on-demand runs)."""
    from app.services.tasks import drain

    assert await drain(timeout), "background tasks did not finish in time"


__all__ = [
    "ADMIN_TOKEN",
    "GEOCODE_URL",
    "HYDERABAD",
    "ScriptedProvider",
    "invalid_cases",
    "seed_region",
    "settle",
]
