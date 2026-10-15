"""Integration: ingest against a real single-node Mongo replica set.

TC-I-04 (happy path), TC-I-05 (provider 5xx → retry → success),
TC-I-06 (schema drift → fatal, no partial writes). Provider HTTP is mocked
with respx; the database is real (docs/04 §9).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from app.models.ingest import IngestStatus, IngestTrigger
from app.models.region import bbox_spec, point_spec
from app.providers.base import ProviderHttpError, SchemaDriftError
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


@respx.mock
async def test_window_is_clamped_to_the_archive_horizon(mongo_db, settings) -> None:
    """EC-35: a current-year window stops at today, not Dec 31.

    Open-Meteo rejects a future ``end_date`` with 400 and a non-429 4xx is a
    :class:`FatalProviderError`, so the unclamped window turned CF-05's
    on-demand backfill for 2026 into a 502 "unusable data" (found live in P6).
    """
    today = datetime.now(UTC).date()
    end_of_year = date(today.year, 12, 31)
    payload = open_meteo_payload(make_readings(START, today))
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)

    service = _service(mongo_db, settings)
    try:
        summary = await service.run(region_id, START, end_of_year, IngestTrigger.CLI)
    finally:
        await service.provider.aclose()

    assert summary["status"] == IngestStatus.SUCCESS.value
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["start_date"] == START.isoformat()
    assert params["end_date"] == today.isoformat()  # never past the archive horizon

    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    assert region["coverage"]["end_year"] == today.year
    last = await mongo_db["daily_weather"].find_one({"region_id": region_id}, sort=[("date", -1)])
    assert last["date"] == today.isoformat()


@respx.mock
async def test_fully_future_window_records_honest_noop_success(mongo_db, settings) -> None:
    """EC-35: nothing fetchable → no provider call, run succeeds, no writes.

    ``ensure_ready`` then sees the years still missing and serves the EC-12
    partial instead of a fatal 502 (CF-05 failure path).
    """
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json={}))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)

    service = _service(mongo_db, settings)
    try:
        summary = await service.run(
            region_id, date(2030, 1, 1), date(2030, 12, 31), IngestTrigger.CLI
        )
    finally:
        await service.provider.aclose()

    assert summary["status"] == IngestStatus.SUCCESS.value
    assert summary["counts"] == {}
    assert route.call_count == 0
    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == 0

    run = await mongo_db["ingest_runs"].find_one({"region_id": region_id})
    assert run["status"] == IngestStatus.SUCCESS.value
    assert run["error"] is None
    assert run["finished_at"] is not None


@respx.mock
async def test_partial_window_extends_coverage_instead_of_shrinking_it(mongo_db, settings) -> None:
    """EC-36: coverage describes everything stored, not the last window.

    An on-demand run fetches only its own years; deriving coverage from that
    window replaced the region's 2018–2020 extent with the single year just
    fetched (found live in P6 right after EC-35 let CF-05 through).
    """
    extra_start, extra_end = date(2021, 1, 1), date(2021, 12, 31)
    payloads = {
        START.isoformat(): open_meteo_payload(make_readings(START, END)),
        extra_start.isoformat(): open_meteo_payload(make_readings(extra_start, extra_end)),
    }
    route = respx.get(ARCHIVE_URL).mock(
        side_effect=lambda request: httpx.Response(
            200, json=payloads[request.url.params["start_date"]]
        )
    )
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)

    service = _service(mongo_db, settings)
    try:
        full = await service.run(region_id, START, END, IngestTrigger.CLI)
        partial = await service.run(region_id, extra_start, extra_end, IngestTrigger.ON_DEMAND)
    finally:
        await service.provider.aclose()

    assert full["status"] == IngestStatus.SUCCESS.value
    assert partial["status"] == IngestStatus.SUCCESS.value
    assert route.call_count == 2

    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["coverage"] == {"start_year": 2018, "end_year": 2021, "years": 4}
    assert await mongo_db["daily_weather"].count_documents({"region_id": region_id}) == (
        EXPECTED_DAYS + 365
    )


@respx.mock
async def test_retryable_failure_records_last_error_while_still_backfilling(
    mongo_db, settings, payload
) -> None:
    """CF-02: a retryable failure must not masquerade as healthy progress.

    Docs/02 §14.2 keeps the region ``backfilling`` while retries remain (only
    a fatal run flips a never-succeeded region to ``failed``), but the failure
    is recorded on the region as ``last_error`` so the UI can surface it; the
    next success clears it via ``mark_ready``.
    """
    fast = settings.model_copy(update={"provider_max_retries": 0})
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(503))
    region_id, _ = await RegionBuilder(mongo_db).ensure(kind="city", name="Hyderabad", spec=POINT)

    with pytest.raises(ProviderHttpError):
        await _run(_service(mongo_db, fast), region_id)

    assert route.call_count == 1  # retries disabled for a fast failure
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "backfilling"
    assert region["last_error"]["code"] == "PROVIDER_HTTP_ERROR"
    assert "503" in region["last_error"]["message"]
    run = await mongo_db["ingest_runs"].find_one({"region_id": region_id})
    assert run["status"] == IngestStatus.FAILED_RETRYABLE.value
    assert run["error"]["code"] == "PROVIDER_HTTP_ERROR"

    # the next attempt succeeds and clears the surfaced error
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    summary = await _run(_service(mongo_db, fast), region_id)

    assert summary["status"] == IngestStatus.SUCCESS.value
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    assert "last_error" not in region
