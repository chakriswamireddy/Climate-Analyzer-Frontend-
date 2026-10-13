"""Region + registry endpoints — TC-I-01, TC-I-02, TC-I-03, TC-I-17.

(Covered elsewhere: TC-I-04/05/06 live in ``test_ingest.py`` at the service
layer, where provider HTTP is mocked with respx.)
"""

from __future__ import annotations

import asyncio

import httpx
import respx

from tests.integration.conftest import GEOCODE_URL, HYDERABAD, seed_region, settle

BBOX_BODY = {
    "kind": "bbox",
    "name": "My box",
    "bbox": {"min_lat": 17.1, "min_lon": 78.3, "max_lat": 17.7, "max_lon": 78.9},
}


async def test_post_regions_is_idempotent(client, mongo_db, provider_factory) -> None:
    """TC-I-01: same bbox twice → 201 then 200, same id, exactly 1 doc."""
    provider_factory.state["delay"] = 0.1

    first = await client.post("/api/v1/regions", json=BBOX_BODY)
    second = await client.post("/api/v1/regions", json=BBOX_BODY)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    first_payload, second_payload = first.json(), second.json()
    assert first_payload["region_id"] == second_payload["region_id"]
    assert first_payload["status"] == "backfilling"
    assert first_payload["poll_after_seconds"] >= 1
    assert first_payload["status_url"] == f"/api/v1/regions/{first_payload['region_id']}"
    assert await mongo_db["regions"].count_documents({}) == 1

    # status endpoint is the CF-02 poll target
    status = await client.get(first_payload["status_url"])
    assert status.status_code == 200

    await settle()
    region = await mongo_db["regions"].find_one({"_id": first_payload["region_id"]})
    assert region["status"] == "ready"
    assert region["data_version"] == "dv_1"
    runs = await mongo_db["ingest_runs"].find({"region_id": region["_id"]}).to_list(None)
    assert [run["status"] for run in runs] == ["success"]
    assert provider_factory.state["calls"], "backfill must hit the provider"


async def test_concurrent_region_create_single_flight(client, mongo_db, provider_factory) -> None:
    """TC-I-02: 5 parallel identical POSTs → 1 doc, one run, shared id."""
    provider_factory.state["delay"] = 0.1

    responses = await asyncio.gather(
        *(client.post("/api/v1/regions", json=BBOX_BODY) for _ in range(5))
    )

    codes = [response.status_code for response in responses]
    assert codes.count(201) == 1, codes
    assert codes.count(200) == 4, codes
    region_ids = {response.json()["region_id"] for response in responses}
    assert len(region_ids) == 1

    assert await mongo_db["regions"].count_documents({}) == 1
    running = await mongo_db["ingest_runs"].count_documents({"status": "running"})
    assert running <= 1, f"{running} concurrent runs — single-flight broken"

    await settle()
    assert await mongo_db["ingest_runs"].count_documents({}) == 1


async def test_hazards_registry_marks_the_default(client) -> None:
    """TC-I-03: /hazards carries heatwave with ``default: true`` (FR-03)."""
    response = await client.get("/api/v1/hazards")
    assert response.status_code == 200
    payload = response.json()
    heatwave = next(item for item in payload["items"] if item["id"] == "heatwave")
    assert heatwave["default"] is True
    assert heatwave["min_days"] == 3
    assert payload["default"] == "heatwave"
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_presets_list_and_filter(client) -> None:
    """§7.3: 25 cities + 12 states, filterable by type."""
    all_presets = await client.get("/api/v1/regions/presets")
    assert all_presets.status_code == 200
    assert all_presets.json()["total"] == 37

    cities = await client.get("/api/v1/regions/presets", params={"type": "city"})
    states = await client.get("/api/v1/regions/presets", params={"type": "state"})
    assert cities.json()["total"] == 25
    assert states.json()["total"] == 12
    assert all(item["kind"] == "city" for item in cities.json()["items"])


async def test_post_regions_from_preset_triggers_backfill(client, mongo_db) -> None:
    """Preset reference path: unknown preset → 422, known → 201 + backfill."""
    unknown = await client.post("/api/v1/regions", json={"preset_id": "city_atlantis"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["field"] == "preset_id"

    created = await client.post("/api/v1/regions", json={"preset_id": "city_delhi"})
    assert created.status_code == 201, created.text
    assert created.json()["region_id"].startswith("r_")
    await settle()
    region = await mongo_db["regions"].find_one({"_id": created.json()["region_id"]})
    assert region["status"] == "ready"


@respx.mock
async def test_geocode_search_caches_upstream_per_query(client) -> None:
    """TC-I-17: 2 identical searches → 1 upstream call (24 h L2, §10)."""
    route = respx.get(GEOCODE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Hyderabad",
                        "admin1": "Telangana",
                        "country": "India",
                        "country_code": "IN",
                        "latitude": 17.385,
                        "longitude": 78.4867,
                    }
                ]
            },
        )
    )
    first = await client.get("/api/v1/regions/search", params={"q": "Hyderabad"})
    second = await client.get("/api/v1/regions/search", params={"q": "  HYDERABAD "})

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert route.call_count == 1
    item = first.json()["items"][0]
    assert item["lat"] == 17.385
    assert item["kind"] == "city"
    assert item["spec"] == HYDERABAD.model_dump(exclude_none=True)


@respx.mock
async def test_geocode_search_upstream_failure_is_503(client) -> None:
    """§7.3: upstream trouble → retryable PROVIDER_ERROR, FE falls back."""
    respx.get(GEOCODE_URL).mock(return_value=httpx.Response(503))
    response = await client.get("/api/v1/regions/search", params={"q": "Patna"})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_ERROR"
    assert error["retryable"] is True


async def test_region_status_404_for_unknown_id(client) -> None:
    unknown = "r_" + "f" * 16
    response = await client.get(f"/api/v1/regions/{unknown}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REGION_NOT_FOUND"


async def test_seed_helper_produces_ready_region(mongo_db) -> None:  # smoke for helpers
    region_id = await seed_region(mongo_db, years=range(1990, 1995), events=3)
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    assert await mongo_db["hazard_annual_stats"].count_documents({"region_id": region_id}) == 5
    assert await mongo_db["hazard_events"].count_documents({"region_id": region_id}) == 3
