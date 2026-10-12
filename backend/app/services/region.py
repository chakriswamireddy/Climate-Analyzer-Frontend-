"""Region registration (docs/02 §6.1, §14.2 state machine).

P1 needs only idempotent creation for the CLI backfill; the HTTP-facing
``RegionService`` (backfill scheduling, status polling) lands in P3.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.db.repositories import RegionRepository, utcnow
from app.models.region import RegionKind, RegionSpec, region_id_from_spec


class RegionBuilder:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.repo = RegionRepository(db)

    async def ensure(
        self,
        *,
        kind: RegionKind,
        name: str,
        spec: RegionSpec,
        provider_meta: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Create the region if absent; requeue it when a prior run failed.

        Returns ``(region_id, created)``. Idempotency comes from the
        deterministic ``_id`` (docs/02 §6.1, DEC-23): the same geography can
        never produce a second document.
        """
        region_id = region_id_from_spec(spec)
        now = utcnow()
        doc = {
            "_id": region_id,
            "kind": kind,
            "name": name.strip()[:80],
            "spec": spec.model_dump(exclude_none=True),
            "provider_meta": provider_meta or {},
            "status": "backfilling",
            "data_version": "dv_0",
            "coverage": None,
            "created_at": now,
            "updated_at": now,
        }
        created = await self.repo.insert_if_absent(doc)
        if not created:
            existing = await self.repo.get(region_id)
            if existing and existing.get("status") == "failed":
                # failed → backfilling on re-submit (docs/02 §14.2)
                await self.repo.set_status(region_id, "backfilling")
        return region_id, created
