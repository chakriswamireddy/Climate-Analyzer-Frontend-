"""Ingest orchestration (docs/02 §5.3, §12, §13).

Pipeline: provider fetch → schema-ratio guard → grid combination → quality
pipeline → hazard pipeline (thresholds + detector + annual, §5.1/§5.4) →
transactional persist → region ``ready`` + ``data_version`` bump. One
``running`` run per region is enforced by the partial unique index (durable)
*and* an in-process lock (fast fail).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

from pymongo.errors import OperationFailure

from app.core.cache import response_cache
from app.core.config import Settings, get_settings
from app.db.repositories import (
    DailyWeatherRepository,
    HazardAnnualRepository,
    HazardEventRepository,
    IngestAlreadyRunning,
    IngestRunRepository,
    RegionRepository,
    ThresholdRepository,
    utcnow,
)
from app.hazard.heatwave import HeatwavePipeline
from app.hazard.pipeline import (
    HazardPipeline,
    PipelineContext,
    PipelineOutput,
)
from app.hazard.registry import pipeline_for
from app.models.ingest import IngestStatus, IngestTrigger, Window
from app.models.region import RegionSpec
from app.models.weather import QualityRow
from app.providers.base import (
    FatalProviderError,
    FetchResult,
    ProviderError,
    RetryableProviderError,
    SchemaDriftError,
    WeatherProvider,
)
from app.services.grid import combine_cells, group_readings
from app.services.quality import quality_pipeline

log = logging.getLogger(__name__)

_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _region_lock(region_id: str) -> asyncio.Lock:
    """Short-lived: held only while reserving the run slot, never during fetch.

    Cached per (region, running loop): an ``asyncio.Lock`` is bound to the loop
    that first *waited* on it, so reusing one from another loop raises
    ``"bound to a different event loop"`` for every later waiter. Production
    runs a single loop (identity holds → one lock per region, as before); tests
    and any multi-loop embedder get a fresh lock per loop instead of a 500.
    """
    loop = asyncio.get_running_loop()
    cached = _locks.get(region_id)
    if cached is None or cached[0] is not loop:
        cached = (loop, asyncio.Lock())
        _locks[region_id] = cached
    return cached[1]


class IngestService:
    def __init__(
        self,
        db: Any,
        provider: WeatherProvider,
        *,
        settings: Settings | None = None,
        pipeline: HazardPipeline | None = None,
        hazard: str = "heatwave",
    ) -> None:
        self.db = db
        self.provider = provider
        self.settings = settings or get_settings()
        self.pipeline = pipeline or HeatwavePipeline()
        self.hazard = hazard
        self.regions = RegionRepository(db)
        self.daily = DailyWeatherRepository(db)
        self.thresholds = ThresholdRepository(db)
        self.events = HazardEventRepository(db)
        self.annual = HazardAnnualRepository(db)
        self.runs = IngestRunRepository(db)
        self._client = db.client

    # -- public API --------------------------------------------------------

    async def run(
        self,
        region_id: str,
        start: date,
        end: date,
        trigger: IngestTrigger,
        *,
        run_id: Any = None,
    ) -> dict[str, Any]:
        """Ingest ``[start, end]`` for ``region_id``; returns the run summary.

        Raises :class:`IngestAlreadyRunning` (single-flight) and the original
        provider error on failure — callers map those to 409/503 (03 §6).
        ``run_id`` lets a caller that already reserved the single-flight slot
        (admin recompute returns the id up front) skip re-reservation.
        """
        region = await self.regions.get(region_id)
        if region is None:
            raise LookupError(f"unknown region {region_id}")

        window = Window(start=start.isoformat(), end=end.isoformat())
        if run_id is None:
            async with _region_lock(region_id):
                run_id = await self.runs.start(region_id, window, trigger)

        started = time.monotonic()
        try:
            # EC-35: the archive's calendar stops at "today", so a window past
            # it (a current-year on-demand range is Jan 1 .. Dec 31) is refused
            # with 400 — a ProviderClientError, i.e. fatal — and CF-05 answers
            # 502 "unusable data" instead of preparing. Clamp to the years that
            # can exist; when the whole window is still ahead of us, record an
            # honest no-op success so the caller serves the EC-12 partial for
            # years the source genuinely does not have yet.
            window_start, window_end = start, min(end, datetime.now(UTC).date())
            if window_start > window_end:
                duration_ms = int((time.monotonic() - started) * 1000)
                await self.runs.finish(run_id, status=IngestStatus.SUCCESS, duration_ms=duration_ms)
                return {
                    "run_id": str(run_id),
                    "region_id": region_id,
                    "status": IngestStatus.SUCCESS.value,
                    "counts": {},
                    "attempts": 1,
                    "data_version": region.get("data_version"),
                    "duration_ms": duration_ms,
                }
            window = Window(start=window_start.isoformat(), end=window_end.isoformat())

            spec = RegionSpec.model_validate(region["spec"])
            fetch = await self.provider.fetch_daily(spec, window_start, window_end)
            check_schema_ratio(
                fetch.rows_in, fetch.rows_rejected, self.settings.bad_row_ratio_limit
            )
            if not fetch.readings:
                raise FatalProviderError("provider returned no rows for the window")

            rows, counts = self._prepare(fetch, spec)
            counts["cell_rows_in"] = fetch.rows_in

            algo_version = self.settings.algo_version
            data_version = RegionRepository.next_data_version(region.get("data_version"))
            threshold_cache = await self.thresholds.get(region_id, algo_version)
            baseline_rows = (
                []
                if threshold_cache is not None
                else await self._baseline_rows(region_id, rows, window)
            )
            output = self.pipeline.build(
                rows,
                PipelineContext(
                    region_id=region_id,
                    hazard=self.hazard,
                    algo_version=algo_version,
                    data_version=data_version,
                    baseline=self.settings.baseline_period,
                    window=window,
                    threshold_cache=threshold_cache,
                    baseline_rows=baseline_rows,
                ),
            )
            counts["events"] = len(output.events)
            counts["annual_stats"] = len(output.annual_stats)
            counts["years"] = len({row.year for row in rows})

            await self._persist(
                region_id=region_id,
                rows=rows,
                output=output,
                window=window,
                data_version=data_version,
                counts=counts,
                attempts=fetch.attempts,
                source=fetch.source,
                run_id=run_id,
                started=started,
            )
            # L2 invalidation: the next GET must re-assemble from the fresh
            # data_version (docs/02 §10 rule 2, TC-I-13).
            response_cache.purge_region(region_id)
        except FatalProviderError as exc:
            await self._fail(run_id, region_id, started, IngestStatus.FAILED_FATAL, exc)
            raise
        except RetryableProviderError as exc:
            await self._fail(run_id, region_id, started, IngestStatus.FAILED_RETRYABLE, exc)
            raise
        except IngestAlreadyRunning:
            raise
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 — must never crash the caller
            log.exception("ingest failed for %s", region_id)
            wrapped = ProviderError(str(exc), code="INGEST_ERROR")
            wrapped.attempts = getattr(exc, "attempts", 1)
            await self._fail(run_id, region_id, started, IngestStatus.FAILED_RETRYABLE, wrapped)
            raise

        return {
            "run_id": str(run_id),
            "region_id": region_id,
            "status": IngestStatus.SUCCESS.value,
            "counts": counts,
            "attempts": fetch.attempts,
            "data_version": data_version,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    async def wait_for_idle(self, region_id: str, *, timeout_s: float = 900.0) -> bool:
        """Block until no run is in flight for the region (CLI convenience)."""
        deadline = time.monotonic() + timeout_s
        while await self.runs.get_running(region_id) is not None:
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(2.0)
        return True

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _prepare(fetch: FetchResult, spec: RegionSpec) -> tuple[list[QualityRow], dict[str, int]]:
        grid_cells = len(spec.points)
        grouped, duplicates = group_readings(fetch.readings)
        combined = combine_cells(grouped, grid_cells=grid_cells)
        result = quality_pipeline(
            combined,
            grid_cells=grid_cells,
            rows_rejected=fetch.rows_rejected,
            duplicates=duplicates,
        )
        return result.rows, dict(result.counts)

    async def _baseline_rows(
        self, region_id: str, rows: list[QualityRow], window: Window
    ) -> list[QualityRow]:
        """Baseline (1991–2020) rows for threshold computation (docs/02 §5.3).

        Initial backfill windows cover the baseline, so the just-fetched rows
        suffice. A later partial window (algo bump, EC-… §14) falls back to
        previously persisted daily rows; in-window values win on date conflicts.
        """
        base_start, base_end = (int(part) for part in self.settings.baseline_period.split("-"))
        in_window = [row for row in rows if base_start <= row.year <= base_end]
        covers_baseline = (
            window.start <= f"{base_start}-01-01" and window.end >= f"{base_end}-12-31"
        )
        if covers_baseline:
            return in_window
        merged = {
            row.date: row for row in await self.daily.baseline_rows(region_id, base_start, base_end)
        }
        merged.update({row.date: row for row in in_window})
        return [merged[date] for date in sorted(merged)]

    async def _persist(
        self,
        *,
        region_id: str,
        rows: list[QualityRow],
        output: PipelineOutput,
        window: Window,
        data_version: str,
        counts: dict[str, int],
        attempts: int,
        source: str,
        run_id: Any,
        started: float,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        coverage = {
            "start_year": rows[0].year,
            "end_year": rows[-1].year,
            "years": len({row.year for row in rows}),
        }

        if output.thresholds is not None:
            # Own transaction *before* the window recompute (docs/02 §13)
            await self.thresholds.upsert(output.thresholds)

        session = await self._client.start_session()
        try:
            async with session.start_transaction():
                await self._write(
                    region_id=region_id,
                    rows=rows,
                    output=output,
                    window=window,
                    data_version=data_version,
                    coverage=coverage,
                    counts=counts,
                    attempts=attempts,
                    source=source,
                    run_id=run_id,
                    duration_ms=duration_ms,
                    session=session,
                )
        except OperationFailure as exc:
            if not _transactions_unavailable(exc):
                raise
            # Degraded mode (docs/02 §13): ordered writes, region bump LAST so
            # stats are only visible once their inputs are fully written.
            log.warning("transactions unavailable (%s); using ordered fallback", exc)
            await self._write(
                region_id=region_id,
                rows=rows,
                output=output,
                window=window,
                data_version=data_version,
                coverage=coverage,
                counts=counts,
                attempts=attempts,
                source=source,
                run_id=run_id,
                duration_ms=duration_ms,
                session=None,
            )
        finally:
            await session.end_session()

    async def _write(
        self,
        *,
        region_id: str,
        rows: list[QualityRow],
        output: PipelineOutput,
        window: Window,
        data_version: str,
        coverage: dict[str, int],
        counts: dict[str, int],
        attempts: int,
        source: str,
        run_id: Any,
        duration_ms: int,
        session: Any,
    ) -> None:
        await self.daily.bulk_upsert(
            region_id, rows, source=source, fetched_at=utcnow(), session=session
        )
        if output.events or output.annual_stats:
            await self.events.replace_window(
                region_id, self.hazard, window, output.events, session=session
            )
            if output.annual_stats:
                await self.annual.bulk_upsert(output.annual_stats, session=session)
        await self.regions.mark_ready(
            region_id, data_version=data_version, coverage=coverage, session=session
        )
        await self.runs.finish(
            run_id,
            status=IngestStatus.SUCCESS,
            attempts=attempts,
            counts=counts,
            duration_ms=duration_ms,
            session=session,
        )

    async def _fail(
        self,
        run_id: Any,
        region_id: str,
        started: float,
        status: IngestStatus,
        error: ProviderError,
    ) -> None:
        """Run → terminal status; region → ``failed`` only while it never
        succeeded (docs/02 §14.2: a fatal nightly leaves a ``ready`` region
        honestly stale rather than hiding good data)."""
        payload = {"code": error.code, "message": str(error)[:500]}
        try:
            await self.runs.finish(
                run_id,
                status=status,
                attempts=getattr(error, "attempts", 1),
                error=payload,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            region = await self.regions.get(region_id)
            if (
                region is not None
                and region.get("status") == "backfilling"
                and status is IngestStatus.FAILED_FATAL
            ):
                await self.regions.set_status(region_id, "failed", error=payload)
        except Exception:  # noqa: BLE001 — must not mask the original error
            log.exception("could not record ingest failure for %s", region_id)


class IngestRunner:
    """Provider lifecycle for API/scheduler-triggered runs (docs/02 §4).

    Every run owns its provider instance (and closes it when done), so
    background tasks never share an ``httpx`` client with the request path and
    a test can swap the whole provider by replacing ``provider_factory``.
    """

    def __init__(self, db: Any, settings: Settings, provider_factory: Any) -> None:
        self.db = db
        self.settings = settings
        self.provider_factory = provider_factory
        self.runs = IngestRunRepository(db)
        self.regions = RegionRepository(db)

    async def reserve(self, region_id: str, start: date, end: date, trigger: IngestTrigger) -> Any:
        """Take the single-flight slot up front (admin returns the run id)."""
        if await self.regions.get(region_id) is None:
            raise LookupError(f"unknown region {region_id}")
        window = Window(start=start.isoformat(), end=end.isoformat())
        async with _region_lock(region_id):
            return await self.runs.start(region_id, window, trigger)

    async def run(
        self,
        region_id: str,
        start: date,
        end: date,
        trigger: IngestTrigger,
        *,
        run_id: Any = None,
        hazard: str = "heatwave",
    ) -> dict[str, Any]:
        provider = self.provider_factory()
        try:
            pipeline = pipeline_for(hazard)
            service = IngestService(
                self.db,
                provider,
                settings=self.settings,
                pipeline=pipeline,
                hazard=hazard,
            )
            return await service.run(region_id, start, end, trigger, run_id=run_id)
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def check_schema_ratio(rows_in: int, rejected: int, limit: float) -> None:
    """≥5% schema-invalid rows ⇒ the provider is broken (docs/02 §5.6)."""
    if rows_in and rejected / rows_in >= limit:
        raise SchemaDriftError(
            f"{rejected}/{rows_in} rows failed schema validation (limit {limit:.0%})"
        )


def _transactions_unavailable(exc: OperationFailure) -> bool:
    """Topology without transaction support (docs/02 §13 degraded mode)."""
    message = str(exc).lower()
    return exc.code in (20, 251) or "transaction numbers" in message


__all__ = ["IngestAlreadyRunning", "IngestRunner", "IngestService", "check_schema_ratio"]
