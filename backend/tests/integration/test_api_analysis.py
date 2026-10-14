"""Analysis endpoints — TC-I-07..TC-I-10, TC-I-13, TC-I-15, TC-I-16,
TC-I-19..TC-I-21 (docs/04 §3; flows CF-04/CF-05; EC-12/EC-21).

Reads are served from precomputed stats; the provider is only ever touched
by the CF-05 on-demand path, which these tests drive deliberately.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx
import pytest

from app.main import create_app
from app.models.api import last_full_year
from app.providers.base import RetryableProviderError
from app.services.region import RegionBuilder
from tests.integration.conftest import HYDERABAD, invalid_cases, seed_region, settle

TIMESERIES = "/api/v1/analysis/timeseries"
SUMMARY = "/api/v1/analysis/summary"
EVENTS = "/api/v1/analysis/events"
INSPECT = "/api/v1/analysis/inspect"


async def _empty_ready_region(mongo_db) -> str:
    """A ``ready`` region with *no* annual stats -> every GET is a cache miss."""
    region_id, _ = await RegionBuilder(mongo_db).ensure(
        kind="city", name="Hyderabad", spec=HYDERABAD
    )
    await mongo_db["regions"].update_one({"_id": region_id}, {"$set": {"status": "ready"}})
    return region_id


# -- TC-I-07 / TC-I-08 -------------------------------------------------------


async def test_single_flight_under_cold_get_storm(client, mongo_db, provider_factory, app) -> None:
    """TC-I-07: 10 parallel cold GETs -> one provider fetch, everyone served."""
    region_id = await _empty_ready_region(mongo_db)
    app.state.settings.ondemand_wait_seconds = 30
    provider_factory.state["delay"] = 0.3
    params = {"region_id": region_id, "start_year": 2016, "end_year": 2020}

    responses = await asyncio.gather(*(client.get(TIMESERIES, params=params) for _ in range(10)))

    statuses = [response.status_code for response in responses]
    assert set(statuses) <= {200, 409}, statuses
    assert statuses.count(200) >= 1
    assert len(provider_factory.state["calls"]) == 1, (
        f"expected one provider fetch, got {provider_factory.state['calls']}"
    )

    await settle()
    runs = await mongo_db["ingest_runs"].find({"region_id": region_id}).to_list(None)
    assert len(runs) == 1
    assert runs[0]["status"] == "success"


async def test_analysis_reads_db_not_provider(client, mongo_db, provider_factory) -> None:
    """TC-I-08: precomputed data -> 200 with the provider sabotaged."""
    provider_factory.state["fail"] = RuntimeError("analysis must not call the provider")
    region_id = await seed_region(mongo_db, years=range(1990, 2021))

    response = await client.get(
        TIMESERIES, params={"region_id": region_id, "start_year": 1990, "end_year": 2020}
    )

    assert response.status_code == 200, response.text
    assert provider_factory.state["calls"] == []
    payload = response.json()
    assert payload["meta"]["algo_version"] == "1.0.0"
    assert len(payload["series"]) == 31
    assert payload["rolling_mean"]["values"], "rolling overlay must be populated"


# -- TC-I-09 validation matrix ----------------------------------------------


@pytest.mark.parametrize(
    "endpoint,params,field",
    invalid_cases(),
    ids=[f"{case[0]}-{case[2]}" for case in invalid_cases()],
)
async def test_validation_matrix(client, endpoint, params, field) -> None:
    """TC-I-09: invalid params -> 422 VALIDATION_ERROR with ``details.field``."""
    response = await client.get(f"/api/v1/analysis/{endpoint}", params=params)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["field"] == field
    assert response.json()["trace_id"].startswith("tr_")


@pytest.mark.parametrize(
    "body,field",
    [
        (
            {
                "kind": "bbox",
                "name": "x",
                "bbox": {"min_lat": 10, "min_lon": 100, "max_lat": 12, "max_lon": -100},
            },
            "bbox",  # V-16 antimeridian
        ),
        (
            {
                "kind": "bbox",
                "name": "x",
                "bbox": {"min_lat": 0, "min_lon": 0, "max_lat": 9, "max_lon": 9},
            },
            "bbox",  # V-06 area > 25 deg2
        ),
        (
            {
                "kind": "bbox",
                "name": "x",
                "bbox": {"min_lat": 0, "min_lon": 0, "max_lat": 11, "max_lon": 2},
            },
            "bbox",  # V-06 span > 10 deg
        ),
        ({"kind": "city", "name": "Bad" + chr(7) + "name"}, "name"),  # V-08 control char
        ({"name": "no kind"}, "kind"),
        ({"kind": "city", "name": "no point"}, "point"),
    ],
    ids=["antimeridian", "area", "span", "control-char", "kind", "point"],
)
async def test_region_body_validation_matrix(client, body, field) -> None:
    response = await client.post("/api/v1/regions", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["field"] == field


async def test_search_query_length_rule(client) -> None:
    response = await client.get("/api/v1/regions/search", params={"q": "a"})
    assert response.status_code == 422
    assert response.json()["error"]["details"]["field"] == "q"


async def test_oversized_body_is_413(client) -> None:
    """V-15 / EC-27: JSON bodies > 10 KB -> 413 BAD_REQUEST."""
    body = {"kind": "bbox", "name": "x", "pad": "a" * 11_000}
    response = await client.post("/api/v1/regions", json=body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_valid_edge_cases_pass(client, mongo_db) -> None:
    """Range edges: 1950 start, current year behind the flag (DEC-04/17)."""
    region_id = await seed_region(mongo_db, years=range(1950, date.today().year + 1))
    base = {"region_id": region_id, "start_year": 1950}

    oldest = await client.get(TIMESERIES, params={**base, "end_year": last_full_year()})
    assert oldest.status_code == 200, oldest.text
    assert len(oldest.json()["series"]) == last_full_year() - 1950 + 1

    flagged = await client.get(
        TIMESERIES,
        params={**base, "end_year": date.today().year, "include_current_year": True},
    )
    assert flagged.status_code == 200, flagged.text

    unflagged = await client.get(TIMESERIES, params={**base, "end_year": date.today().year})
    assert unflagged.status_code == 422
    assert unflagged.json()["error"]["details"]["field"] == "end_year"


async def test_unknown_region_is_404(client) -> None:
    response = await client.get(TIMESERIES, params={"region_id": "r_" + "a" * 16})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REGION_NOT_FOUND"


# -- TC-I-10 ETag ------------------------------------------------------------


async def test_etag_enables_304_on_repeat(client, mongo_db) -> None:
    """TC-I-10: second GET with If-None-Match -> 304, empty body."""
    region_id = await seed_region(mongo_db, years=range(1990, 2021))
    params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}

    first = await client.get(TIMESERIES, params=params)
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag.startswith('"') and len(etag) > 10
    assert first.headers["cache-control"] == "public, max-age=300"

    second = await client.get(TIMESERIES, params=params, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert "content-length" not in second.headers

    # a different range is a different representation
    other = await client.get(
        TIMESERIES, params={**params, "start_year": 1991}, headers={"If-None-Match": etag}
    )
    assert other.status_code == 200
    assert other.headers["etag"] != etag


async def test_etag_304_for_cdn_weak_form(client, mongo_db) -> None:
    """EC-34: the CDN downgrades the strong ETag to ``W/"…"`` on gzip, so the
    browser/proxy echoes the weak form back — If-None-Match is a weak
    comparison (RFC 9110 §13.1.2) and must still answer 304."""
    region_id = await seed_region(mongo_db, years=range(1990, 2021))
    params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}

    first = await client.get(TIMESERIES, params=params)
    assert first.status_code == 200
    etag = first.headers["etag"]

    weak = etag if etag.startswith("W/") else f"W/{etag}"
    second = await client.get(TIMESERIES, params=params, headers={"If-None-Match": weak})
    assert second.status_code == 304
    assert second.content == b""

    # a weak tag for a *different* representation still misses
    other = await client.get(
        TIMESERIES, params={**params, "start_year": 1991}, headers={"If-None-Match": weak}
    )
    assert other.status_code == 200
    assert other.headers["etag"] != etag


async def test_l2_cache_serves_hit_on_repeat(client, mongo_db) -> None:
    region_id = await seed_region(mongo_db, years=range(1990, 2021))
    params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}

    first = await client.get(TIMESERIES, params=params)
    second = await client.get(TIMESERIES, params=params)

    assert first.json()["meta"]["cache"] == "miss"
    assert second.json()["meta"]["cache"] == "hit"
    assert first.headers["etag"] == second.headers["etag"]


# -- TC-I-13 data_version consistency ---------------------------------------


async def test_ingest_bump_changes_etag_and_data_version(
    client, mongo_db, admin_headers, provider_factory
) -> None:
    """TC-I-13: recompute between GETs -> new data_version + ETag, L2 purged."""
    region_id = await seed_region(mongo_db, years=range(1990, 2021))
    params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}

    first = await client.get(TIMESERIES, params=params)
    assert first.json()["meta"]["data_version"] == "dv_1"
    etag_before = first.headers["etag"]

    recompute = await client.post(
        "/api/v1/admin/recompute", json={"region_id": region_id}, headers=admin_headers
    )
    assert recompute.status_code == 202, recompute.text
    await settle()

    second = await client.get(TIMESERIES, params=params)
    assert second.status_code == 200
    assert second.json()["meta"]["data_version"] == "dv_2"
    assert second.headers["etag"] != etag_before
    assert second.json()["meta"]["cache"] == "miss"
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["data_version"] == "dv_2"


# -- TC-I-15 events pagination ----------------------------------------------


async def test_events_pagination_sort_and_filters(client, mongo_db) -> None:
    """TC-I-15: 84 events over 4 pages; stable sort; min_duration narrows."""
    region_id = await seed_region(mongo_db, years=range(1990, 2021), events=84)
    base = {"region_id": region_id, "start_year": 1990, "end_year": 2020, "page_size": 25}

    seen: list[str] = []
    for page, expected in ((1, 25), (2, 25), (3, 25), (4, 9)):
        response = await client.get(EVENTS, params={**base, "page": page})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 84
        assert payload["page"] == page
        assert len(payload["items"]) == expected
        seen.extend(item["event_id"] for item in payload["items"])
    assert len(set(seen)) == 84

    ascending = await client.get(EVENTS, params={**base, "sort": "start_date:asc"})
    dates = [item["start_date"] for item in ascending.json()["items"]]
    assert dates == sorted(dates)

    descending = await client.get(EVENTS, params={**base, "sort": "start_date:desc"})
    dates = [item["start_date"] for item in descending.json()["items"]]
    assert dates == sorted(dates, reverse=True)

    filtered = await client.get(EVENTS, params={**base, "min_duration": 5})
    expected_total = await mongo_db["hazard_events"].count_documents(
        {"region_id": region_id, "duration_days": {"$gte": 5}, "algo_version": "1.0.0"}
    )
    assert 0 < filtered.json()["total"] < 84
    assert filtered.json()["total"] == expected_total

    # EC-21: rows from other years never leak into a narrowed page
    narrowed = await client.get(EVENTS, params={**base, "start_year": 2000})
    expected_narrowed = await mongo_db["hazard_events"].count_documents(
        {
            "region_id": region_id,
            "algo_version": "1.0.0",
            "year": {"$gte": 2000, "$lte": 2020},
        }
    )
    assert narrowed.json()["total"] == expected_narrowed


async def test_inspect_returns_daily_rows_for_one_event(client, mongo_db) -> None:
    region_id = await seed_region(mongo_db, years=range(1990, 1995), events=1)
    event = await mongo_db["hazard_events"].find_one({"region_id": region_id})
    start = date.fromisoformat(event["start_date"])
    await mongo_db["daily_weather"].insert_many(
        [
            {
                "region_id": region_id,
                "date": (start + timedelta(days=offset)).isoformat(),
                "year": (start + timedelta(days=offset)).year,
                "doy": (start + timedelta(days=offset)).timetuple().tm_yday,
                "tmax_c": 42.0,
                "quality": "valid",
                "grid_cells_ok": 9,
                "source": "open-meteo-archive",
                "fetched_at": "2026-01-01T00:00:00Z",
            }
            for offset in range(event["duration_days"])
        ]
    )

    response = await client.get(
        INSPECT, params={"region_id": region_id, "start_date": event["start_date"]}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["event"]["event_id"] == str(event["_id"])
    assert len(payload["rows"]) == event["duration_days"]
    assert set(payload["rows"][0]) == {"date", "tmax_c", "threshold", "qualifies", "quality"}
    assert payload["rows"][0]["quality"] == "valid"

    missing = await client.get(INSPECT, params={"region_id": region_id, "start_date": "1990-01-01"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "EVENT_NOT_FOUND"


# -- TC-I-16 on-demand bounded wait (CF-05) ---------------------------------


async def test_on_demand_backfill_resolves_within_budget(
    client, mongo_db, provider_factory, app
) -> None:
    """Missing years, fast provider -> the GET waits and answers 200 (CF-05)."""
    region_id = await _empty_ready_region(mongo_db)
    app.state.settings.ondemand_wait_seconds = 8
    provider_factory.state["delay"] = 0.5

    response = await client.get(
        TIMESERIES, params={"region_id": region_id, "start_year": 2016, "end_year": 2020}
    )

    assert response.status_code == 200, response.text
    assert provider_factory.state["calls"]
    assert response.json()["meta"]["cache"] == "miss"
    await settle()
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    stats = await mongo_db["hazard_annual_stats"].count_documents(
        {"region_id": region_id, "algo_version": "1.0.0"}
    )
    assert stats == 5


async def test_on_demand_timeout_504_while_run_continues(
    client, mongo_db, provider_factory, app
) -> None:
    """Slow provider -> 504 TIMEOUT, run keeps going, retry later succeeds."""
    region_id = await _empty_ready_region(mongo_db)
    app.state.settings.ondemand_wait_seconds = 1
    provider_factory.state["delay"] = 3.0
    params = {"region_id": region_id, "start_year": 2016, "end_year": 2020}

    timed_out = await client.get(TIMESERIES, params=params)
    assert timed_out.status_code == 504, timed_out.text
    error = timed_out.json()["error"]
    assert error["code"] == "TIMEOUT"
    assert error["retryable"] is True
    assert error["retry_after_seconds"] == 5

    running = await mongo_db["ingest_runs"].count_documents(
        {"region_id": region_id, "status": "running"}
    )
    assert running == 1, "the background run must survive the 504"

    await settle()
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"

    app.state.settings.ondemand_wait_seconds = 30
    retry = await client.get(TIMESERIES, params=params)
    assert retry.status_code == 200, retry.text


async def test_joined_run_failure_answers_504_not_degraded_200(
    client, mongo_db, provider_factory, app
) -> None:
    """TC-I-21 / CF-05: a single-flight loser inherits the winner's *failure*.

    The winner's on-demand run dies on a provider error while a second GET is
    joined onto it. 0% of the requested years exist, so the EC-12 rule (<80%)
    requires 504 retryable for *both* callers — never a 200 body assembled
    from the gap: the FE would stop polling and render a suppressed panel that
    the restored data never reaches (found live while running cf05.spec.ts).
    """
    region_id = await _empty_ready_region(mongo_db)
    app.state.settings.ondemand_wait_seconds = 8
    provider_factory.state["delay"] = 0.3
    provider_factory.state["fail"] = RetryableProviderError("provider down")
    params = {"region_id": region_id, "start_year": 2016, "end_year": 2020}

    responses = await asyncio.gather(*(client.get(SUMMARY, params=params) for _ in range(2)))

    statuses = [response.status_code for response in responses]
    assert statuses == [504, 504], [response.text for response in responses]
    for response in responses:
        error = response.json()["error"]
        assert error["code"] == "TIMEOUT"
        assert error["retryable"] is True
        assert error["retry_after_seconds"] == 5

    await settle()
    runs = await mongo_db["ingest_runs"].find({"region_id": region_id}).to_list(None)
    assert runs, "the losing caller must not invent a run"
    assert {run["status"] for run in runs} == {"failed_retryable"}

    # the gap must not have been cached as a degraded body either
    again = await client.get(SUMMARY, params=params)
    assert again.status_code == 504, again.text


async def test_region_backfilling_is_409_with_retry_after(client, mongo_db) -> None:
    region_id = await seed_region(mongo_db, years=range(1990, 1995), status="backfilling")
    response = await client.get(TIMESERIES, params={"region_id": region_id})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "REGION_NOT_READY"
    assert error["retryable"] is True

    status = await client.get(f"/api/v1/regions/{region_id}")
    assert response.headers["retry-after"] == str(status.json()["poll_after_seconds"])


# -- TC-I-19 partial coverage (EC-12) ---------------------------------------


async def test_partial_coverage_year_gets_caveats(client, mongo_db) -> None:
    """TC-I-19: a gap year -> 200 + caveats + coverage_pct < 100."""
    region_id = await seed_region(
        mongo_db, years=range(1990, 2021), coverage_overrides={1997: 85.0}
    )
    params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}

    timeseries = await client.get(TIMESERIES, params=params)
    assert timeseries.status_code == 200
    payload = timeseries.json()
    row = next(item for item in payload["series"] if item["year"] == 1997)
    assert row["coverage_pct"] < 100
    assert any("1997" in caveat for caveat in payload["meta"]["caveats"])
    assert payload["meta"]["caveats"][0].startswith("coverage:")

    summary = await client.get(SUMMARY, params=params)
    assert summary.status_code == 200
    body = summary.json()
    assert body["headline_kind"] in {
        "pct_increase",
        "pct_decrease",
        "no_change",
        "new_activity",
        "range_too_short",
        "suppressed_low_coverage",
    }
    assert any("1997" in caveat for caveat in body["caveats"])
    assert body["meta"]["algo_version"] == "1.0.0"
    assert body["stats"]["ols"] is not None


async def test_missing_year_is_zero_filled_and_flagged(client, mongo_db) -> None:
    """A year with no stats -> zeros + coverage 0 + gap caveat (sec 7.4)."""
    region_id = await seed_region(mongo_db, years=[1990, 1991, 1993, 1994])
    response = await client.get(
        TIMESERIES, params={"region_id": region_id, "start_year": 1990, "end_year": 1994}
    )
    assert response.status_code == 200
    payload = response.json()
    gap = next(item for item in payload["series"] if item["year"] == 1992)
    assert gap["heatwave_days"] == 0
    assert gap["coverage_pct"] == 0.0
    assert any("1992" in caveat for caveat in payload["meta"]["caveats"])


# -- TC-I-20 algo bump -------------------------------------------------------


async def test_algo_bump_serves_versioned_rows_and_recomputes(
    mongo_db, api_settings, provider_factory, admin_headers
) -> None:
    """TC-I-20: new ALGO_VERSION ignores old rows; recompute writes new ones."""
    region_id = await seed_region(
        mongo_db, years=range(1990, 2021), algo_version="1.0.0", days_per_year=6
    )
    await mongo_db["hazard_annual_stats"].insert_many(
        [
            {
                "region_id": region_id,
                "hazard": "heatwave",
                "year": year,
                "event_count": 1,
                "heatwave_days": 42,
                "mean_duration": 3.0,
                "max_duration": 4,
                "mean_exceedance": 1.0,
                "coverage_pct": 99.5,
                "algo_version": "2.0.0",
                "data_version": "dv_1",
                "computed_at": "2026-01-01T00:00:00Z",
            }
            for year in range(1990, 2021)
        ]
    )

    bumped = api_settings.model_copy(update={"algo_version": "2.0.0"})
    application = create_app(
        settings=bumped, db_provider=lambda: mongo_db, provider_factory=provider_factory
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            params = {"region_id": region_id, "start_year": 1990, "end_year": 2020}
            response = await async_client.get(TIMESERIES, params=params)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["meta"]["algo_version"] == "2.0.0"
            # versioned keys: the 2.0.0 rows win even though 1.0.0 also exists
            assert {row["heatwave_days"] for row in payload["series"]} == {42}

            recompute = await async_client.post(
                "/api/v1/admin/recompute",
                json={"region_id": region_id},
                headers=admin_headers,
            )
            assert recompute.status_code == 202, recompute.text
            await settle()

            after = await async_client.get(TIMESERIES, params=params)
            assert after.json()["meta"]["algo_version"] == "2.0.0"
            assert after.json()["meta"]["data_version"] == "dv_2"

    # the old algo's rows are never deleted by a recompute (EC-21)
    assert (
        await mongo_db["hazard_annual_stats"].count_documents(
            {"region_id": region_id, "algo_version": "1.0.0"}
        )
        == 31
    )
    assert provider_factory.state["calls"], "recompute must fetch through the provider"
