"""Integration: real HeatwavePipeline through IngestService (docs/02 §5.3, §13).

Proves the P2 wiring end-to-end against a real Mongo replica set: the first
ingest computes and persists ``threshold_cache`` in its own transaction, then
writes events + annual stats inside the window transaction; a second ingest of
the same window hits the cache (no recompute, no duplicate threshold doc).
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import respx

from app.models.ingest import IngestStatus, IngestTrigger
from app.models.region import point_spec
from app.providers.open_meteo import OpenMeteoProvider
from app.services.ingest import IngestService
from app.services.region import RegionBuilder
from tests.conftest import ARCHIVE_URL
from tests.fixtures.series_factory import make_readings, open_meteo_payload

START, END = date(1991, 1, 1), date(2020, 12, 31)
POINT = point_spec(17.385, 78.4867)


def _payload() -> dict:
    overrides = {
        (date(2018, 5, 10) + timedelta(days=offset)).isoformat(): {"tmax_c": 45.0}
        for offset in range(5)
    }
    return open_meteo_payload(make_readings(START, END, overrides=overrides))


def _service(db, settings) -> IngestService:
    provider = OpenMeteoProvider(settings)
    return IngestService(db, provider, settings=settings)


async def test_real_pipeline_persists_thresholds_then_reuses_cache(mongo_db, settings) -> None:
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=_payload()))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)
    service = _service(mongo_db, settings)
    try:
        first = await service.run(region_id, START, END, IngestTrigger.CLI)
    finally:
        await service.provider.aclose()

    assert first["status"] == IngestStatus.SUCCESS.value
    assert first["counts"]["events"] >= 1
    assert first["counts"]["annual_stats"] == 30

    threshold_doc = await mongo_db["threshold_cache"].find_one(
        {"region_id": region_id, "algo_version": settings.algo_version}
    )
    assert threshold_doc is not None
    assert len(threshold_doc["by_doy"]) == 365
    assert threshold_doc["baseline"] == settings.baseline_period

    events = await mongo_db["hazard_events"].find({"region_id": region_id}).to_list(length=None)
    assert len(events) >= 1
    assert any(e["duration_days"] >= 3 for e in events)
    assert any(e["year"] == 2018 for e in events)

    annual = (
        await mongo_db["hazard_annual_stats"].find({"region_id": region_id}).to_list(length=None)
    )
    assert len(annual) == 30
    assert all(0.0 <= a["coverage_pct"] <= 100.0 for a in annual)

    service2 = _service(mongo_db, settings)
    try:
        second = await service2.run(region_id, START, END, IngestTrigger.CLI)
    finally:
        await service2.provider.aclose()

    assert second["status"] == IngestStatus.SUCCESS.value
    assert second["data_version"] == "dv_2"
    docs = await mongo_db["threshold_cache"].count_documents(
        {"region_id": region_id, "algo_version": settings.algo_version}
    )
    assert docs == 1
    events_after = await mongo_db["hazard_events"].count_documents({"region_id": region_id})
    assert events_after == len(events)
