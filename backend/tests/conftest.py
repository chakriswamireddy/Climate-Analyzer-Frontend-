"""Shared test fixtures.

Unit tests never touch Mongo. Integration tests need a real single-node
replica set (transactions, docs/02 §13):

* local: ``docker compose up`` → ``mongodb://localhost:27017/?directConnection=true``
* CI: sets ``REQUIRE_MONGO=1`` so an unreachable Mongo fails the run instead
  of silently skipping (docs/04 §9 wants real-Mongo integration gates).
"""

from __future__ import annotations

import os
import uuid

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import Settings
from app.db.indexes import ensure_indexes

TEST_MONGODB_URI = os.getenv("TEST_MONGODB_URI", "mongodb://localhost:27017/?directConnection=true")
REQUIRE_MONGO = os.getenv("REQUIRE_MONGO") == "1"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@pytest.fixture
def settings() -> Settings:
    """Test settings: fast retries, no throttling, dev TTL off."""
    return Settings(
        mongodb_uri=TEST_MONGODB_URI,
        provider_backoff_base_s=0.01,
        provider_min_interval_s=0.0,
        provider_max_retries=3,
        provider_chunk_budget_s=30.0,
        daily_weather_ttl_days=0,
    )


@pytest.fixture
async def mongo_db(settings: Settings):
    client = AsyncIOMotorClient(settings.mongodb_uri, serverSelectionTimeoutMS=3000)
    try:
        await client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001 — probe must not raise
        client.close()
        if REQUIRE_MONGO:
            pytest.fail(f"MongoDB required but unreachable: {exc}")
        pytest.skip(f"MongoDB not reachable at {TEST_MONGODB_URI}")

    name = f"climate_analyzer_test_{uuid.uuid4().hex[:12]}"
    db = client[name]
    await ensure_indexes(db, daily_ttl_days=0)
    yield db
    await client.drop_database(name)
    client.close()
