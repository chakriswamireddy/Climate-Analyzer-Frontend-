"""Region registration, status, and backfill scheduling (docs/02 §6.1,
§7.3, §14.2 state machine).

P1 exposed :class:`RegionBuilder` (idempotent create) for the CLI; this
module adds the HTTP-facing :class:`RegionService` the routers delegate to:

* ``POST /regions`` is idempotent by hash (DEC-23) and *reserves* the
  single-flight run slot before responding, so a burst of double-submits can
  never queue more than one backfill (docs/02 §12, TC-I-02);
* the backfill itself runs as a tracked background task (02 §14 — never
  inline in the request handler) and answers with ``poll_after_seconds``.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.repositories import RegionRepository, utcnow
from app.models.api import RegionCreateBody
from app.models.ingest import IngestTrigger
from app.models.region import RegionKind, RegionSpec, bbox_spec, point_spec, region_id_from_spec
from app.providers.geocoding import GeocodeClient
from app.services.ingest import IngestAlreadyRunning, IngestRunner
from app.services.presets import get_preset
from app.services.tasks import spawn

log = logging.getLogger(__name__)

HAZARD = "heatwave"


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


class RegionService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        settings: Settings,
        *,
        provider_factory: Any,
    ) -> None:
        self.db = db
        self.settings = settings
        self.regions = RegionRepository(db)
        self.builder = RegionBuilder(db)
        self.runner = IngestRunner(db, settings, provider_factory)

    # -- read paths --------------------------------------------------------

    async def status(self, region_id: str) -> dict[str, Any]:
        doc = await self.regions.get(region_id)
        if doc is None:
            raise ApiError("REGION_NOT_FOUND", f"unknown region {region_id}")
        payload: dict[str, Any] = {
            "region_id": doc["_id"],
            "name": doc.get("name", ""),
            "kind": doc.get("kind"),
            "status": doc.get("status"),
            "data_version": doc.get("data_version"),
            "coverage": doc.get("coverage"),
            "last_error": doc.get("last_error"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
        if doc.get("status") == "backfilling":
            payload["poll_after_seconds"] = self.settings.poll_after_seconds
        return payload

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Proxied geocode search (docs/02 §7.3). Raises ProviderError → 503."""
        client = GeocodeClient(self.settings)
        try:
            return await client.search(query)
        finally:
            await client.aclose()

    # -- write path --------------------------------------------------------

    async def create(self, body: RegionCreateBody) -> tuple[str, bool]:
        """Idempotent region create + single-flight backfill enqueue (§12).

        Returns ``(region_id, created)`` — ``created`` decides 201 vs 200.
        """
        body.validate_rules()
        kind, name, spec, provider_meta = _resolve(body)
        region_id, created = await self.builder.ensure(
            kind=kind, name=name, spec=spec, provider_meta=provider_meta
        )

        doc = await self.regions.get(region_id)
        if doc is not None and doc.get("status") == "backfilling":
            await self._enqueue(region_id, trigger=IngestTrigger.ON_DEMAND)
        return region_id, created

    async def _enqueue(self, region_id: str, *, trigger: IngestTrigger) -> bool:
        """Reserve the run slot now, execute in the background (docs/02 §12)."""
        start, end = self.initial_window()
        try:
            run_id = await self.runner.reserve(region_id, start, end, trigger)
        except IngestAlreadyRunning as exc:
            # Single-flight index did its job (EC-02) — but a *stale* running
            # doc (process restart, deploy) also lands here with no job
            # started; the sweeper fails it out and requeues. Log so ops can
            # tell "in progress" from "nothing running" (CF-02).
            log.warning(
                "backfill for %s already running (run_id=%s); not re-queued",
                region_id,
                exc.run_id,
            )
            return False
        spawn(
            self.runner.run(region_id, start, end, trigger, run_id=run_id, hazard=HAZARD),
            name=f"backfill:{region_id}",
        )
        return True

    async def backfill_now(self, region_id: str, *, trigger: IngestTrigger) -> Any:
        """Admin recompute: reserve + spawn, returns the run id (§7.5)."""
        start, end = self.initial_window()
        run_id = await self.runner.reserve(region_id, start, end, trigger)
        spawn(
            self.runner.run(region_id, start, end, trigger, run_id=run_id, hazard=HAZARD),
            name=f"recompute:{region_id}",
        )
        return run_id

    async def background_backfill(
        self, region_id: str, *, trigger: IngestTrigger = IngestTrigger.SCHEDULER
    ) -> None:
        """Sweeper/admin requeue of a region with no live run (§14.1)."""
        start, end = self.initial_window()
        try:
            await self.runner.run(region_id, start, end, trigger, hazard=HAZARD)
        except IngestAlreadyRunning:
            log.info("backfill for %s already running", region_id)

    def initial_window(self) -> tuple[date, date]:
        """DEC-04: 1950-01-01 → 31 Dec of the last *full* year."""
        return (
            date.fromisoformat(self.settings.initial_backfill_start),
            date(date.today().year - 1, 12, 31),
        )


def _resolve(body: RegionCreateBody) -> tuple[RegionKind, str, RegionSpec, dict[str, Any]]:
    """Preset reference or raw spec → canonical (kind, name, spec, meta)."""
    if body.preset_id:
        preset = get_preset(body.preset_id)
        if preset is None:
            raise ApiError(
                "VALIDATION_ERROR",
                f"unknown preset_id {body.preset_id!r}",
                details={
                    "field": "preset_id",
                    "reason": "preset_id must reference a curated preset",
                },
            )
        return (
            preset["kind"],
            body.name or preset["name"],
            RegionSpec.model_validate(preset["spec"]),
            dict(preset.get("provider_meta", {})),
        )

    kind = body.kind or "bbox"
    if kind == "city":
        if body.point is None:  # pragma: no cover — validate_rules guarantees
            raise ApiError("VALIDATION_ERROR", "point is required when kind=city")
        spec = point_spec(body.point.lat, body.point.lon)
    else:
        if body.bbox is None:  # pragma: no cover — validate_rules guarantees
            raise ApiError("VALIDATION_ERROR", f"bbox is required when kind={kind}")
        spec = bbox_spec(body.bbox.validate_rules())
    return kind, body.name, spec, {"source": "user_input", "country_code": ""}


__all__ = ["HAZARD", "RegionBuilder", "RegionService", "utcnow"]
