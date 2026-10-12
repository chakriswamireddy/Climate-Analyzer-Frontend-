"""Mongo connection helpers (Motor, DEC-01).

The client is created lazily so tests can build their own client against an
ephemeral database without fighting a process-wide singleton.
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings

_client: AsyncIOMotorClient | None = None


def get_client(uri: str | None = None) -> AsyncIOMotorClient:
    global _client
    if _client is None or uri is not None:
        settings = get_settings()
        client = AsyncIOMotorClient(
            uri or settings.mongodb_uri,
            serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
        )
        if uri is None:
            _client = client
        return client
    return _client


def get_db() -> AsyncIOMotorDatabase:
    settings = get_settings()
    return get_client()[settings.mongodb_db]


async def ping(client: AsyncIOMotorClient | None = None) -> bool:
    """Readiness probe primitive (docs/02 §7.1) — 2 s budget at the caller."""
    target = client or get_client()
    try:
        await target.admin.command("ping")
        return True
    except Exception:  # noqa: BLE001 — readiness must never raise
        return False


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
