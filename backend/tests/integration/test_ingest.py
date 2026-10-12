"""Integration: ingest against a real single-node Mongo replica set.

TC-I-04 (happy path), TC-I-05 (provider 5xx → retry → success),
TC-I-06 (schema drift → fatal, no partial writes). Provider HTTP is mocked
with respx; the database is real (docs/04 §9).
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from app.models.ingest import IngestStatus, IngestTrigger
from app.models.region import bbox_spec, point_spec
from app.providers.base import SchemaDriftError
from app.providers.open_meteo import OpenMeteoProvider
from app.services.ingest import IngestService
from app.services.region import RegionBuilder
from tests.conftest import ARCHIVE_URL
from tests.fixtures.fake_pipeline import FakeHazardPipeline
from tests.fixtures.series_factory import make_readings, open_meteo_payload

START, END = date(2018, 1, 1), date(2020, 12, 31)
EXPECTED_DAYS = 1096  # 365 + 366 + 365
POINT = point_spec(17.385, 78.4867)


def _service(db, settings) -> IngestService:
    provider = OpenMeteoProvider(settings)
    return IngestService(db, provider, settings=settings, pipeline=FakeHazardPipeline())


@pytest.fixture
def payload() -> dict:
    return open_meteo_payload(make_readings(START, END))


async def _run(service: IngestService, region_id: str):
    try:
        return await service.run(region_id, START, END, IngestTrigger.CLI)
    finally:
        await service.provider.aclose()


@respx.mock
async def test_ingest_happy_path_writes_every_collection(mongo_db, settings, payload) -> None:
    """TC-I-04: daily rows, events, annual stats; region ready; audit counts."""
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    region_id, created = await RegionBuilder(mongo_db).ensure(
        kind="city", name="Hyderabad", spec=POINT
    )
    assert created is True

    service = _service(mongo_db, settings)
    summary = await _run(service, region_id)

    assert summary["status"] == IngestStatus.SUCCESS.value
    assert summary["attempts"] == 1
    assert summary["data_version"] == "dv_1"
    counts = summary["counts"]
    assert counts["rows_in"] == EXPECTED_DAYS
    assert counts["cell_rows_in"] == EXPECTED_DAYS
    assert counts["years"] == 3
    assert counts["events"] == 3
    assert counts["annual_stats"] == 3
    assert (
        counts["rows_valid"] + counts["missing"] + counts["outliers"] + counts["degraded_grid"]
        == EXPECTED_DAYS
    )

    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == (
        EXPECTED_DAYS
    )
    assert await mongo_db["hazard_events"].count_documents({"region_id": region_id}) == 3
    assert await mongo_db["hazard_annual_stats"].count_documents({"region_id": region_id}) == 3

    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    assert region["data_version"] == "dv_1"
    assert region["coverage"] == {"start_year": 2018, "end_year": 2020, "years": 3}
    assert "last_error" not in region

    run = await mongo_db["ingest_runs"].find_one({"region_id": region_id})
    assert run["status"] == IngestStatus.SUCCESS.value
    assert run["attempts"] == 1
    assert run["trigger"] == IngestTrigger.CLI.value
    assert run["finished_at"] is not None
    assert run["duration_ms"] >= 0
    assert run["counts"]["events"] == 3
    assert run["window"] == {"start": "2018-01-01", "end": "2020-12-31"}
    assert run["error"] is None


@respx.mock
async def test_ingest_is_idempotent(mongo_db, settings, payload) -> None:
    """Re-running the same window upserts rather than duplicating (EC-03)."""
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)
    service = _service(mongo_db, settings)

    first = await _run(service, region_id)
    second = await _run(service, region_id)

    assert first["data_version"] == "dv_1"
    assert second["data_version"] == "dv_2"
    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == (
        EXPECTED_DAYS
    )
    assert await mongo_db["hazard_events"].count_documents({"region_id": region_id}) == 3
    assert await mongo_db["ingest_runs"].count_documents({"region_id": region_id}) == 2


@respx.mock
async def test_provider_5xx_then_success_records_attempts(mongo_db, settings, payload) -> None:
    """TC-I-05: two 5xx then 200 → 3 attempts observed, run succeeds."""
    route = respx.get(ARCHIVE_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(200, json=payload),
        ]
    )
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)
    service = _service(mongo_db, settings)

    summary = await _run(service, region_id)

    assert route.call_count == 3
    assert summary["status"] == IngestStatus.SUCCESS.value
    assert summary["attempts"] == 3
    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == (
        EXPECTED_DAYS
    )
    run = await mongo_db["ingest_runs"].find_one({"region_id": region_id})
    assert run["status"] == IngestStatus.SUCCESS.value
    assert run["attempts"] == 3


@respx.mock
async def test_schema_drift_fails_run_fatal_without_partial_writes(mongo_db, settings) -> None:
    """TC-I-06: missing temperature_2m_max → failed_fatal, region failed, no rows."""
    drifted = open_meteo_payload(make_readings(START, END), omit={"temperature_2m_max"})
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=drifted))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)
    service = _service(mongo_db, settings)

    with pytest.raises(SchemaDriftError):
        await _run(service, region_id)

    assert route.call_count == 1  # fatal errors are never retried
    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == 0
    assert await mongo_db["hazard_events"].count_documents({"region_id": region_id}) == 0

    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "failed"
    assert region["last_error"]["code"] == "PROVIDER_SCHEMA_DRIFT"
    assert region["data_version"] == "dv_0"

    run = await mongo_db["ingest_runs"].find_one({"region_id": region_id})
    assert run["status"] == IngestStatus.FAILED_FATAL.value
    assert run["error"]["code"] == "PROVIDER_SCHEMA_DRIFT"
    assert run["finished_at"] is not None


@respx.mock
async def test_grid_region_ingests_via_areal_mean(mongo_db, settings) -> None:
    """Bbox regions fetch 9 cells and persist the 3×3 areal mean (DEC-10)."""
    cell_readings = [make_readings(START, END, cell=cell) for cell in range(9)]
    # 4 cells of the grid are unavailable on every day → below the 5/9 rule
    for readings in cell_readings[5:]:
        for reading in readings:
            reading.tmax_c = None
            reading.tmin_c = None

    # One batched request per decade-chunk answers with a list of 9 locations
    batched = [open_meteo_payload(readings) for readings in cell_readings]
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=batched))

    region_id, _ = await RegionBuilder(mongo_db).ensure(
        kind="bbox",
        name="My box",
        spec=bbox_spec([17.1, 78.3, 17.7, 78.9]),
    )
    service = _service(mongo_db, settings)
    summary = await _run(service, region_id)

    assert summary["status"] == IngestStatus.SUCCESS.value
    assert summary["counts"]["cell_rows_in"] == EXPECTED_DAYS * 9
    assert summary["counts"]["rows_in"] == EXPECTED_DAYS
    assert summary["counts"]["degraded_grid"] == EXPECTED_DAYS

    doc = await mongo_db["daily_weather"].find_one({"region_id": region_id, "date": "2018-01-01"})
    assert doc["quality"] == "degraded_grid"
    assert doc["grid_cells_ok"] == 5
    assert doc["tmax_c"] is not None
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
