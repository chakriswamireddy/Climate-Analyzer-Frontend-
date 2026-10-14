"""Background jobs (docs/02 §14.1): nightly incremental + stale-run sweeper.

Runs on APScheduler's ``AsyncIOScheduler`` inside the app's event loop
(DEC-16). Both jobs are plain coroutines so tests can drive them directly
without waiting for cron/interval triggers.

* ``nightly_incremental`` (02:00 UTC): re-ingests the last
  ``nightly_lookback_days`` days for every ``ready`` region — late-arriving
  source data — bumping ``data_version`` (§14.2: a fatal nightly leaves the
  region ``ready``/valid-stale, never ``failed``).
* ``sweep_stale_runs`` (every ``sweeper_interval_minutes``): runs orphaned
  for > ``stale_run_minutes`` → ``failed_retryable``; regions stuck
  ``backfilling`` with no live run and attempts budget left are re-enqueued
  (EC-18, TC-I-18); a region whose backfilling episode has burned through
  ``max_ingest_attempts`` consecutive failed runs is marked ``failed`` with an
  honest ``last_error`` so the user can re-submit it (02 §14.2,
  03 CF-06 postcondition "region honestly failed").

``last_heartbeat`` feeds ``GET /readyz`` (§7.1 Mongo ping + heartbeat).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.db.repositories import IngestRunRepository, RegionRepository, utcnow
from app.models.ingest import IngestStatus, IngestTrigger
from app.services.ingest import IngestAlreadyRunning, IngestRunner
from app.services.tasks import spawn

log = logging.getLogger(__name__)


class SchedulerService:
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
        self.runs = IngestRunRepository(db)
        self.runner = IngestRunner(db, settings, provider_factory)
        self.last_heartbeat: str | None = None
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self.nightly_incremental,
            trigger=CronTrigger(
                hour=self.settings.scheduler_nightly_hour_utc, minute=0, timezone="UTC"
            ),
            id="nightly_incremental",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self._scheduler.add_job(
            self.sweep_stale_runs,
            trigger=IntervalTrigger(minutes=self.settings.sweeper_interval_minutes),
            id="stale_run_sweeper",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.start()
        self.last_heartbeat = utcnow()
        log.info(
            "scheduler started (nightly %02d:00 UTC, sweeper every %d min)",
            self.settings.scheduler_nightly_hour_utc,
            self.settings.sweeper_interval_minutes,
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def heartbeat_age_s(self) -> float | None:
        if self.last_heartbeat is None:
            return None
        stamp = self.last_heartbeat.replace("Z", "+00:00")
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()

    # -- jobs ---------------------------------------------------------------

    async def nightly_incremental(self) -> list[str]:
        """Re-ingest recent days for every ``ready`` region (§14.1)."""
        self.last_heartbeat = utcnow()
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.settings.nightly_lookback_days)
        queued: list[str] = []
        for region in await self._regions_with_status("ready"):
            try:
                run_id = await self.runner.reserve(
                    region["_id"], start, end, IngestTrigger.SCHEDULER
                )
            except IngestAlreadyRunning:
                continue  # single-flight: on-demand or admin owns it (EC-26)
            spawn(
                self.runner.run(region["_id"], start, end, IngestTrigger.SCHEDULER, run_id=run_id),
                name=f"nightly:{region['_id']}",
            )
            queued.append(region["_id"])
        log.info("nightly incremental queued for %d region(s)", len(queued))
        return queued

    async def sweep_stale_runs(self) -> dict[str, Any]:
        """Orphaned runs → ``failed_retryable``; stuck regions re-enqueued."""
        self.last_heartbeat = utcnow()
        cutoff = _iso_before(self.settings.stale_run_minutes)
        stale = await self.runs.fail_stale(cutoff)
        requeued: list[str] = []
        failed: list[str] = []
        for region in await self._regions_with_status("backfilling"):
            region_id = region["_id"]
            if await self.runs.get_running(region_id) is not None:
                continue  # live run — nothing to sweep
            recent = await self.runs.recent(region_id, limit=self.settings.max_ingest_attempts + 1)
            if self._budget_exhausted(recent, region):
                log.warning("region %s exhausted its retry budget", region_id)
                await self.regions.set_status(
                    region_id,
                    "failed",
                    error={
                        "code": "INGEST_RETRY_EXHAUSTED",
                        "message": (
                            "backfill failed after "
                            f"{self.settings.max_ingest_attempts} attempts; "
                            "re-submit the region to retry"
                        ),
                    },
                )
                failed.append(region_id)
                continue
            start, end = self._requeue_window(recent)
            spawn(
                self.runner.run(region_id, start, end, IngestTrigger.SCHEDULER),
                name=f"sweep:{region_id}",
            )
            requeued.append(region_id)
        if stale or requeued or failed:
            log.info(
                "sweeper: %d stale run(s) failed, %d region(s) requeued, "
                "%d region(s) marked failed",
                len(stale),
                len(requeued),
                len(failed),
            )
        return {
            "stale_runs": [str(doc["_id"]) for doc in stale],
            "requeued_regions": requeued,
            "failed_regions": failed,
        }

    def _budget_exhausted(self, recent: list[dict[str, Any]], region: dict[str, Any]) -> bool:
        """True when this backfilling episode has no retry budget left (§14.2).

        Counts consecutive finished ``failed_*`` runs started since the region
        entered ``backfilling`` (``updated_at`` bumps exactly on that
        transition — ``ensure``/``set_status`` — so an earlier failed lifecycle
        or a nightly failure on a previously-ready region cannot starve a
        fresh re-submitted backfill). ``max_ingest_attempts`` (3) failures in
        the current episode → the region must honestly become ``failed``.
        """
        episode_start = (
            region.get("updated_at") or region.get("created_at") or "0000-00-00T00:00:00Z"
        )
        failures = 0
        for run in recent:
            if str(run.get("started_at", "")) < episode_start:
                break  # belongs to an earlier lifecycle of this region
            status = run.get("status")
            if status == IngestStatus.SUCCESS.value:
                break
            if status in (IngestStatus.FAILED_RETRYABLE.value, IngestStatus.FAILED_FATAL.value):
                failures += 1
        return failures >= self.settings.max_ingest_attempts

    # -- helpers ------------------------------------------------------------

    async def _regions_with_status(self, status: str) -> list[dict[str, Any]]:
        cursor = self.db["regions"].find(
            {"status": status}, {"_id": 1, "spec": 1, "updated_at": 1, "created_at": 1}
        )
        return await cursor.to_list(length=None)

    def _requeue_window(self, recent: list[dict[str, Any]]) -> tuple[date, date]:
        """Retry the window the orphaned run attempted (EC-18); a region that
        never got a run re-queues the full initial backfill (DEC-04)."""
        if recent:
            window = recent[0].get("window") or {}
            try:
                return date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
            except (KeyError, TypeError, ValueError):
                pass
        return (
            date.fromisoformat(self.settings.initial_backfill_start),
            date(datetime.now(UTC).year - 1, 12, 31),
        )


def _iso_before(minutes: int) -> str:
    """Same format as ``utcnow()`` so the ``$lt`` string compare is valid."""
    stamp = datetime.now(UTC) - timedelta(minutes=minutes)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["SchedulerService"]
