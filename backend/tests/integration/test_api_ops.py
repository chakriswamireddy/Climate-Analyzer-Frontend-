"""Ops endpoints — TC-I-11, TC-I-12, TC-I-14, TC-I-18 (docs/04 §3; §11 rate
limits, §7.5 admin auth, §7.1 probes, §14.3 sweeper)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorClient

from app.main import create_app
from app.services.scheduler import SchedulerService
from tests.integration.conftest import ADMIN_TOKEN, seed_region, settle

# -- TC-I-11 rate limiting ---------------------------------------------------


async def test_analysis_bucket_60_per_min_then_429(client) -> None:
    """TC-I-11: analysis class (60/min) → 429 RATE_LIMITED with envelope."""
    allowed = [
        await client.get("/api/v1/hazards", headers={"Origin": "http://localhost:5173"})
        for _ in range(60)
    ]
    assert [response.status_code for response in allowed] == [200] * 60
    assert allowed[0].headers["x-ratelimit-limit"] == "60"
    assert allowed[0].headers["x-ratelimit-remaining"] == "59"

    limited = await client.get("/api/v1/hazards", headers={"Origin": "http://localhost:5173"})
    assert limited.status_code == 429
    error = limited.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["retryable"] is True
    assert error["retry_after_seconds"] >= 1
    assert limited.headers["retry-after"]
    assert limited.headers["x-ratelimit-remaining"] == "0"
    # S-06: CORS is outside the limiter, so 429s still carry the origin header
    assert limited.headers["access-control-allow-origin"] == "http://localhost:5173"

    # a different forwarded IP gets its own bucket (§11 trust boundary)
    other_ip = await client.get("/api/v1/hazards", headers={"X-Forwarded-For": "203.0.113.7"})
    assert other_ip.status_code == 200


async def test_region_and_admin_buckets_have_their_own_caps(client, admin_headers) -> None:
    """region 10/min and admin 5/min are separate buckets (§7.1, §11)."""
    region_responses = [await client.post("/api/v1/regions", json={}) for _ in range(10)]
    assert [response.status_code for response in region_responses] == [422] * 10
    region_limited = await client.post("/api/v1/regions", json={})
    assert region_limited.status_code == 429
    assert region_limited.json()["error"]["retry_after_seconds"] >= 1

    for _ in range(5):
        allowed = await client.get("/api/v1/admin/ingest-runs", headers=admin_headers)
        assert allowed.status_code == 200
    admin_limited = await client.get("/api/v1/admin/ingest-runs", headers=admin_headers)
    assert admin_limited.status_code == 429
    assert "5/min" in admin_limited.json()["error"]["message"]


async def test_probes_are_never_rate_limited(client) -> None:
    responses = [await client.get("/healthz") for _ in range(70)]
    assert {response.status_code for response in responses} == {200}
    assert "x-ratelimit-remaining" not in responses[0].headers


# -- TC-I-12 admin auth (V-14) ------------------------------------------------


async def test_admin_requires_exactly_one_valid_token(client, admin_headers) -> None:
    """TC-I-12: missing/wrong/empty/multi-value token → stable 401, no echo."""
    missing = await client.get(
        "/api/v1/admin/ingest-runs", headers={"X-Forwarded-For": "198.51.100.1"}
    )
    assert missing.status_code == 401
    error = missing.json()["error"]
    assert error["code"] == "UNAUTHORIZED"
    assert error["retryable"] is False
    assert error.get("retry_after_seconds") is None
    assert ADMIN_TOKEN not in missing.text

    wrong = await client.get(
        "/api/v1/admin/ingest-runs",
        headers={"X-Admin-Token": "not-the-token", "X-Forwarded-For": "198.51.100.2"},
    )
    assert wrong.status_code == 401

    empty = await client.get(
        "/api/v1/admin/ingest-runs",
        headers={"X-Admin-Token": "", "X-Forwarded-For": "198.51.100.3"},
    )
    assert empty.status_code == 401

    # exactly one value: two headers (even with the right one) → 401 (V-14)
    multi = await client.get(
        "/api/v1/admin/ingest-runs",
        headers=[
            ("X-Admin-Token", ADMIN_TOKEN),
            ("X-Admin-Token", ADMIN_TOKEN),
            ("X-Forwarded-For", "198.51.100.4"),
        ],
    )
    assert multi.status_code == 401

    valid = await client.get("/api/v1/admin/ingest-runs", headers=admin_headers)
    assert valid.status_code == 200, valid.text
    assert valid.json()["items"] == []
    assert valid.headers["cache-control"] == "no-store"


async def test_admin_mutations_check_auth_before_work(client, mongo_db) -> None:
    recompute = await client.post("/api/v1/admin/recompute", json={})
    assert recompute.status_code == 401
    assert recompute.json()["error"]["code"] == "UNAUTHORIZED"
    assert await mongo_db["ingest_runs"].count_documents({}) == 0


# -- TC-I-14 DB down (CF-14) -------------------------------------------------


async def test_db_down_still_answers_healthz_and_503s_everything_else(
    api_settings, provider_factory
) -> None:
    """TC-I-14: Mongo unreachable → /healthz 200, /readyz + API → 503 envelope."""
    settings = api_settings.model_copy(update={"mongodb_timeout_ms": 250})
    dead = AsyncIOMotorClient(
        "mongodb://127.0.0.1:1/?directConnection=true", serverSelectionTimeoutMS=250
    )
    application = create_app(
        settings=settings,
        db_provider=lambda: dead["climate_test"],
        provider_factory=provider_factory,
    )
    import httpx

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30.0
        ) as async_client:
            health = await async_client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            assert health.headers["x-trace-id"].startswith("tr_")

            ready = await async_client.get("/readyz")
            assert ready.status_code == 503
            error = ready.json()["error"]
            assert error["code"] == "SERVICE_UNAVAILABLE"
            assert error["retryable"] is True
            assert error["retry_after_seconds"] == 10

            create = await async_client.post(
                "/api/v1/regions",
                json={
                    "kind": "bbox",
                    "name": "x",
                    "bbox": {"min_lat": 1, "min_lon": 1, "max_lat": 2, "max_lon": 2},
                },
            )
            assert create.status_code == 503, create.text
            assert create.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
            assert create.json()["trace_id"].startswith("tr_")


async def test_readyz_is_200_when_dependencies_are_healthy(client) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["mongo"] == "ok"
    assert payload["scheduler"] == "disabled"
    assert response.headers["cache-control"] == "no-store"


async def test_client_error_beacon_is_accepted_not_stored(client) -> None:
    accepted = await client.post(
        "/api/v1/client-errors",
        json={"code": "FE_CRASH", "message": "boom", "url": "/trends"},
    )
    assert accepted.status_code == 202
    assert accepted.json()["accepted"] is True
    assert accepted.headers["cache-control"] == "no-store"

    too_long = await client.post("/api/v1/client-errors", json={"code": "c" * 65})
    assert too_long.status_code == 422
    assert too_long.json()["error"]["details"]["field"] == "code"


# -- TC-I-18 stale-run sweeper (§14.3) --------------------------------------


def _iso_ago(minutes: int) -> str:
    return (
        (datetime.now(UTC) - timedelta(minutes=minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


async def test_sweeper_fails_stale_run_and_requeues_region(
    mongo_db, api_settings, provider_factory
) -> None:
    """TC-I-18: orphaned run → failed_retryable; region re-enqueued → ready."""
    scheduler = SchedulerService(mongo_db, api_settings, provider_factory=provider_factory)
    region_id = await seed_region(mongo_db, years=range(2016, 2021), status="backfilling")
    stale = await mongo_db["ingest_runs"].insert_one(
        {
            "region_id": region_id,
            "window": {"start": "2016-01-01", "end": "2020-12-31"},
            "status": "running",
            "attempts": 1,
            "trigger": "on_demand",
            "counts": {},
            "error": None,
            "started_at": _iso_ago(api_settings.stale_run_minutes + 5),
            "finished_at": None,
        }
    )

    result = await scheduler.sweep_stale_runs()

    assert result["stale_runs"] == [str(stale.inserted_id)]
    assert result["requeued_regions"] == [region_id]
    swept = await mongo_db["ingest_runs"].find_one({"_id": stale.inserted_id})
    assert swept["status"] == "failed_retryable"
    assert swept["error"]["code"] == "RUN_STALE"

    await settle()
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    total = await mongo_db["ingest_runs"].count_documents({"region_id": region_id})
    assert total == 2, "requeued run must have been recorded and finished"


async def _failed_run(
    mongo_db,
    region_id: str,
    *,
    minutes_ago: int,
    status: str = "failed_retryable",
    attempts: int = 1,
) -> None:
    await mongo_db["ingest_runs"].insert_one(
        {
            "region_id": region_id,
            "window": {"start": "2016-01-01", "end": "2020-12-31"},
            "status": status,
            "attempts": attempts,
            "trigger": "on_demand",
            "counts": {},
            "error": {"code": "PROVIDER_5XX", "message": "upstream 500"},
            "started_at": _iso_ago(minutes_ago),
            "finished_at": _iso_ago(minutes_ago - 1),
        }
    )


async def test_sweeper_leaves_live_run_alone(mongo_db, api_settings, provider_factory) -> None:
    """A live run must not be failed or requeued by the sweeper (EC-18)."""
    scheduler = SchedulerService(mongo_db, api_settings, provider_factory=provider_factory)
    region_id = await seed_region(mongo_db, years=range(2016, 2021), status="backfilling")
    await mongo_db["ingest_runs"].insert_one(
        {
            "region_id": region_id,
            "window": {"start": "2016-01-01", "end": "2020-12-31"},
            "status": "running",
            "attempts": 1,
            "trigger": "on_demand",
            "counts": {},
            "error": None,
            "started_at": _iso_ago(0),
            "finished_at": None,
        }
    )

    result = await scheduler.sweep_stale_runs()

    assert result["stale_runs"] == []
    assert result["requeued_regions"] == []
    running = await mongo_db["ingest_runs"].find_one({"region_id": region_id, "status": "running"})
    assert running is not None, "a live run must not be failed by the sweeper"
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "backfilling"


async def test_sweeper_marks_exhausted_region_failed(
    mongo_db, api_settings, provider_factory
) -> None:
    """TC-I-18: ``max_ingest_attempts`` failed runs in one backfilling episode
    → region ``failed`` + honest ``last_error``, no further requeue (02 §14.2,
    03 CF-06 "region honestly failed")."""
    scheduler = SchedulerService(mongo_db, api_settings, provider_factory=provider_factory)
    region_id = await seed_region(mongo_db, years=range(2016, 2021), status="backfilling")
    episode_start = _iso_ago(30)
    await mongo_db["regions"].update_one(
        {"_id": region_id}, {"$set": {"updated_at": episode_start}}
    )
    for offset in range(api_settings.max_ingest_attempts):
        await _failed_run(mongo_db, region_id, minutes_ago=20 - 5 * offset)

    result = await scheduler.sweep_stale_runs()

    assert result["requeued_regions"] == []
    assert result["failed_regions"] == [region_id]
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "failed"
    assert region["last_error"]["code"] == "INGEST_RETRY_EXHAUSTED"
    assert "re-submit" in region["last_error"]["message"]
    await settle()
    running = await mongo_db["ingest_runs"].find_one({"region_id": region_id, "status": "running"})
    assert running is None, "an exhausted region must not be requeued"


async def test_sweeper_budget_resets_when_region_resubmitted(
    mongo_db, api_settings, provider_factory
) -> None:
    """Re-POSTing a failed region starts a fresh backfilling episode: failures
    from the previous lifecycle must not count against the new budget (02 §14.2
    ``failed → POST → backfilling``)."""
    scheduler = SchedulerService(mongo_db, api_settings, provider_factory=provider_factory)
    region_id = await seed_region(mongo_db, years=range(2016, 2021), status="backfilling")
    old_episode = _iso_ago(60)
    await mongo_db["regions"].update_one({"_id": region_id}, {"$set": {"updated_at": old_episode}})
    for offset in range(api_settings.max_ingest_attempts):
        await _failed_run(mongo_db, region_id, minutes_ago=50 - 5 * offset)

    # user re-submits the same spec → status backfilling bumps updated_at
    await mongo_db["regions"].update_one({"_id": region_id}, {"$set": {"updated_at": _iso_ago(1)}})

    result = await scheduler.sweep_stale_runs()

    assert result["failed_regions"] == []
    assert result["requeued_regions"] == [region_id]
    await settle()
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready", "fresh episode must be requeued, not starved"


async def test_sweeper_requeues_backfilling_region_with_no_run(
    mongo_db, api_settings, provider_factory
) -> None:
    """Region stuck backfilling with no runs at all → initial window (DEC-04)."""
    scheduler = SchedulerService(mongo_db, api_settings, provider_factory=provider_factory)
    region_id = await seed_region(mongo_db, years=range(2016, 2021), status="backfilling")

    result = await scheduler.sweep_stale_runs()

    assert result["requeued_regions"] == [region_id]
    await settle()
    region = await mongo_db["regions"].find_one({"_id": region_id})
    assert region["status"] == "ready"
    runs = await mongo_db["ingest_runs"].find({"region_id": region_id}).to_list(None)
    assert runs and runs[0]["window"]["start"] == api_settings.initial_backfill_start
