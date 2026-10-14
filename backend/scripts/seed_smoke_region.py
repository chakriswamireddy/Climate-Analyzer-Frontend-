"""Seed a deterministic ready region for the CI API smoke (docs/04 §1).

Offline by design: annual stats + events are written directly (no provider
call), so the CI smoke can never depend on Open-Meteo quota (EC-31). The
region id is the canonical DEC-23 hash of its spec, so re-runs are idempotent
(the stats are replaced). Prints the region id on stdout for the smoke step.
"""

from __future__ import annotations

import asyncio
import sys

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.db.indexes import ensure_indexes
from app.db.repositories import RegionRepository, utcnow
from app.models.region import point_spec, region_id_from_spec
from app.services.region import RegionBuilder

HAZARD = "heatwave"
YEARS = list(range(1990, 2021))
NAME = "CI Smoke"
SPEC = point_spec(17.0, 80.0)
REGION_ID = region_id_from_spec(SPEC)


async def main() -> str:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client[settings.mongodb_db]
        await ensure_indexes(db, daily_ttl_days=settings.daily_weather_ttl_days)

        builder = RegionBuilder(db)
        await builder.ensure(kind="city", name=NAME, spec=SPEC)
        repo = RegionRepository(db)
        await repo.mark_ready(
            REGION_ID,
            data_version="dv_1",
            coverage={
                "start_year": YEARS[0],
                "end_year": YEARS[-1],
                "years": len(YEARS),
            },
        )

        # constant-but-realistic fixture: 5 heatwave days + one 5-day event
        # per year — coherent with the events rows below (total = 5×31).
        await db["hazard_annual_stats"].delete_many({"region_id": REGION_ID, "hazard": HAZARD})
        await db["hazard_annual_stats"].insert_many(
            [
                {
                    "region_id": REGION_ID,
                    "hazard": HAZARD,
                    "year": year,
                    "event_count": 1,
                    "heatwave_days": 5,
                    "mean_duration": 5.0,
                    "max_duration": 5,
                    "mean_exceedance": 1.2,
                    "coverage_pct": 100,
                    "algo_version": settings.algo_version,
                    "data_version": "dv_1",
                    "computed_at": utcnow(),
                }
                for year in YEARS
            ]
        )
        await db["hazard_events"].delete_many({"region_id": REGION_ID, "hazard": HAZARD})
        await db["hazard_events"].insert_many(
            [
                {
                    "region_id": REGION_ID,
                    "hazard": HAZARD,
                    "start_date": f"{year}-05-05",
                    "end_date": f"{year}-05-09",
                    "year": year,
                    "duration_days": 5,
                    "mean_exceedance": 1.2,
                    "peak_exceedance": 2.1,
                    "algo_version": settings.algo_version,
                    "data_version": "dv_1",
                    "computed_at": utcnow(),
                }
                for year in YEARS
            ]
        )
        return REGION_ID
    finally:
        client.close()


if __name__ == "__main__":
    print(asyncio.run(main()))
    sys.exit(0)
