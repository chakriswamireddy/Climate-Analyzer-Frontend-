"""Analysis read service — the logic behind ``GET /analysis/*`` (docs/02 §4,
§7.4, §10; flows CF-04/CF-05, DEC-24, EC-12/EC-21).

Responsibilities:

1. resolve the region (404) and gate on its status (409 while backfilling);
2. **CF-05 on-demand backfill**: when the requested years are missing from
   ``hazard_annual_stats`` the service starts a single-flight ingest and
   waits up to ``ondemand_wait_seconds``:
   * run finished inside the budget → assemble from the fresh rows (200);
   * we started it and the budget elapsed → ``504 TIMEOUT`` while the run
     continues (TC-I-16; §7.2);
   * another caller's run owns the window and outlives the budget →
     ``409 REGION_NOT_READY`` (poll /regions/{id});
   * another caller's run *failed* → its failure is adopted here, so a gap
     below the 80% floor still answers ``504`` instead of a degraded ``200``
     (CF-05 failure paths; TC-I-21);
   * provider failure → ≥80% of requested years present ⇒ honest partial
     (EC-12), otherwise mapped onto the §7.2 catalog (503 circuit open,
     502 fatal, 504 retryable);
3. assemble the documented payloads from *precomputed* annual stats/events —
   a GET never touches the weather provider (DEC-24, TC-I-08);
4. serve assembled bodies from the L2 response cache keyed with
   ``data_version`` + ``algo_version`` (§10); ETags/trace ids are the
   middleware's job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.cache import cache_key, response_cache
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.repositories import (
    DailyWeatherRepository,
    HazardAnnualRepository,
    HazardEventRepository,
    RegionRepository,
    ThresholdRepository,
    utcnow,
)
from app.hazard.trends import ROLLING_MIN_PERIODS, ROLLING_WINDOW, analyze, rolling_mean
from app.models.api import AnalysisQuery, EventsQuery, InspectQuery
from app.models.ingest import IngestStatus, IngestTrigger
from app.models.weather import Quality, calendar_doy
from app.providers.base import (
    CircuitOpenError,
    FatalProviderError,
    ProviderError,
    RetryableProviderError,
)
from app.services.ingest import IngestAlreadyRunning, IngestRunner
from app.services.tasks import spawn

log = logging.getLogger(__name__)

SERIES_FIELDS = (
    "year",
    "event_count",
    "heatwave_days",
    "mean_duration",
    "max_duration",
    "mean_exceedance",
    "coverage_pct",
)
ASSESSABLE = (Quality.VALID, Quality.DEGRADED_GRID)


class AnalysisService:
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
        self.annual = HazardAnnualRepository(db)
        self.events = HazardEventRepository(db)
        self.daily = DailyWeatherRepository(db)
        self.thresholds = ThresholdRepository(db)
        self.runner = IngestRunner(db, settings, provider_factory)

    # -- endpoint entry points ---------------------------------------------

    async def timeseries(self, q: AnalysisQuery) -> dict[str, Any]:
        q.validate_rules()
        region = await self._prepare(q)
        key = self._key("timeseries", q, region)
        cached = response_cache.get(key)
        if cached is not None:
            return self._marked(cached, "hit")
        payload = await self._build_timeseries(region, q)
        self._store(key, payload, region)
        return self._marked(payload, "miss")

    async def summary(self, q: AnalysisQuery) -> dict[str, Any]:
        q.validate_rules()
        region = await self._prepare(q)
        key = self._key("summary", q, region)
        cached = response_cache.get(key)
        if cached is not None:
            return self._marked(cached, "hit")
        payload = await self._build_summary(region, q)
        self._store(key, payload, region)
        return self._marked(payload, "miss")

    async def events_page(self, q: EventsQuery) -> dict[str, Any]:
        q.validate_rules()
        region = await self._prepare(q)
        key = self._key("events", q, region)
        cached = response_cache.get(key)
        if cached is not None:
            return self._marked(cached, "hit")
        payload = await self._build_events(region, q)
        self._store(key, payload, region)
        return self._marked(payload, "miss")

    async def inspect(self, q: InspectQuery) -> dict[str, Any]:
        """Daily rows for one event (drill-down). Not L2-cached (§10)."""
        q.validate_rules()
        region = await self.region(q.region_id)
        self._status_gate(region)
        algo = self.settings.algo_version
        event = await self.events.find_by_start(region["_id"], q.hazard, algo, q.start_date)
        if event is None:
            raise ApiError(
                "EVENT_NOT_FOUND",
                f"no {q.hazard} event starts on {q.start_date} in this region",
                details={"start_date": q.start_date, "hazard": q.hazard},
            )
        threshold_doc = await self.thresholds.get(region["_id"], algo)
        by_doy = {
            int(doy): (None if value is None else float(value))
            for doy, value in (threshold_doc or {}).get("by_doy", {}).items()
        }
        daily_rows = await self.daily.rows_for_window(
            region["_id"], event["start_date"], event["end_date"]
        )
        rows = [self._day(doc, by_doy) for doc in daily_rows]
        return {
            "event": {
                "event_id": str(event["_id"]),
                "region_id": region["_id"],
                "hazard": q.hazard,
                "start_date": event["start_date"],
                "end_date": event["end_date"],
                "duration_days": event["duration_days"],
                "mean_exceedance": event["mean_exceedance"],
                "peak_exceedance": event["peak_exceedance"],
            },
            "rows": rows,
            "meta": self._meta(region, caveats=self._inspect_caveats(rows)),
        }

    # -- region + readiness ------------------------------------------------

    async def region(self, region_id: str) -> dict[str, Any]:
        doc = await self.regions.get(region_id)
        if doc is None:
            raise ApiError("REGION_NOT_FOUND", f"unknown region {region_id}")
        return doc

    def _status_gate(self, region: dict[str, Any]) -> None:
        status = region.get("status")
        if status == "backfilling":
            raise ApiError(
                "REGION_NOT_READY",
                "Region data is still being prepared — poll and retry",
                retry_after_seconds=self.settings.poll_after_seconds,
                details={"status": status},
            )
        if status == "failed":
            raise ApiError(
                "REGION_NOT_READY",
                "Region backfill failed — re-submit the region to retry",
                retry_after_seconds=30,
                details={"status": status},
            )

    async def _prepare(self, q: AnalysisQuery) -> dict[str, Any]:
        """Resolve the region and make sure the requested years exist."""
        region = await self.region(q.region_id)
        return await self.ensure_ready(region, q.start_year, q.end_year, q.hazard)

    async def ensure_ready(
        self, region: dict[str, Any], start_year: int, end_year: int, hazard: str
    ) -> dict[str, Any]:
        """CF-05: gap detection, single-flight on-demand ingest, bounded wait."""
        self._status_gate(region)
        missing = await self._missing_years(region["_id"], hazard, start_year, end_year)
        if not missing:
            return region

        started = time.monotonic()
        task = spawn(
            self.runner.run(
                region["_id"],
                _year_start(start_year),
                _year_end(end_year),
                IngestTrigger.ON_DEMAND,
                hazard=hazard,
            ),
            name=f"analysis:{region['_id']}",
        )
        done, _ = await asyncio.wait({task}, timeout=float(self.settings.ondemand_wait_seconds))
        if task not in done:
            # We started the run and the budget elapsed: answer honestly and
            # let the background run finish (§7.2 504, TC-I-16).
            raise ApiError(
                "TIMEOUT",
                "Data preparation is still running — please retry shortly",
                retry_after_seconds=5,
                details={"reason": "on-demand backfill exceeded the wait budget"},
            )

        exc = task.exception()
        if isinstance(exc, IngestAlreadyRunning):
            # Lost the single-flight race — join the winner's run for whatever
            # budget is left, then judge by the data that exists (CF-05 step 4).
            remaining = max(0.0, self.settings.ondemand_wait_seconds - (time.monotonic() - started))
            if not await self._wait_idle(region["_id"], remaining):
                raise ApiError(
                    "REGION_NOT_READY",
                    "Another analysis is already preparing this data",
                    retry_after_seconds=self.settings.poll_after_seconds,
                )
            # The winner's outcome is ours: only a run that *succeeded* may
            # downgrade to an honest partial. A failed one keeps the failure so
            # the ≥80% rule below answers 504 instead of a degraded 200 built
            # from the gap (CF-05 failure paths / EC-12; TC-I-21).
            exc = await self._joined_failure(region["_id"])

        refreshed = await self.regions.get(region["_id"]) or region
        still = await self._missing_years(refreshed["_id"], hazard, start_year, end_year)
        if not still:
            return refreshed
        if exc is None:
            # Run completed (ours or the joined one) — the source simply lacks
            # some years: serve an honest partial, caveats flag the gaps (EC-12).
            return refreshed
        total = end_year - start_year + 1
        if (total - len(still)) / total >= 0.8:
            return refreshed  # ≥80% present → partial body + caveats (EC-12)
        raise self._map_failure(exc)

    async def _joined_failure(self, region_id: str) -> BaseException | None:
        """Outcome of the run we lost the single-flight race to.

        ``None`` only when that run ended ``success`` (its missing years are
        then the source's, and the caller gets an honest partial). Anything
        else — ``failed_retryable``, ``failed_fatal``, or a run that never
        reached a terminal state — is returned as a provider error so
        :meth:`ensure_ready` maps it through the ≥80%/504 rule below.
        """
        runs = await self.runner.runs.recent(region_id, limit=1)
        if not runs:
            return RetryableProviderError("on-demand backfill did not run")
        run = runs[0]
        if run.get("status") == IngestStatus.SUCCESS.value:
            return None
        error = run.get("error") or {}
        message = error.get("message") or "on-demand backfill failed"
        if run.get("status") == IngestStatus.FAILED_FATAL.value:
            return FatalProviderError(message)
        return RetryableProviderError(message)

    async def _missing_years(
        self, region_id: str, hazard: str, start_year: int, end_year: int
    ) -> set[int]:
        present = await self.annual.years(
            region_id, hazard, self.settings.algo_version, start_year, end_year
        )
        return set(range(start_year, end_year + 1)) - present

    async def _wait_idle(self, region_id: str, budget: float) -> bool:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if await self.runner.runs.get_running(region_id) is None:
                return True
            await asyncio.sleep(0.05)
        return await self.runner.runs.get_running(region_id) is None

    def _map_failure(self, exc: BaseException) -> ApiError:
        """Provider failures → the §7.2 catalog (CF-05 failure paths)."""
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, CircuitOpenError):
            return ApiError(
                "SERVICE_UNAVAILABLE",
                "Data provider is unavailable — please retry shortly",
                retry_after_seconds=10,
            )
        if isinstance(exc, FatalProviderError):
            return ApiError(
                "PROVIDER_ERROR",
                "Data provider returned unusable data",
                retry_after_seconds=30,
            )
        if isinstance(exc, (RetryableProviderError, ProviderError)):
            return ApiError(
                "TIMEOUT",
                "Data preparation failed — please retry",
                retry_after_seconds=5,
            )
        if isinstance(exc, LookupError):
            return ApiError("REGION_NOT_FOUND", str(exc))
        log.exception("on-demand backfill failed unexpectedly", exc_info=exc)
        return ApiError(
            "INTERNAL_ERROR",
            "Data preparation failed — please retry",
            retry_after_seconds=5,
        )

    # -- payload builders ---------------------------------------------------

    async def _series(self, region: dict[str, Any], q: AnalysisQuery) -> tuple[list, list[int]]:
        rows = await self.annual.rows(
            region["_id"], q.hazard, self.settings.algo_version, q.start_year, q.end_year
        )
        by_year = {int(row["year"]): row for row in rows}
        series: list[dict[str, Any]] = []
        missing: list[int] = []
        for year in range(q.start_year, q.end_year + 1):
            row = by_year.get(year)
            if row is None:
                missing.append(year)
                series.append(_zero_row(year))
            else:
                series.append({field: row.get(field) for field in SERIES_FIELDS})
        return series, missing

    async def _build_timeseries(self, region: dict[str, Any], q: AnalysisQuery) -> dict[str, Any]:
        series, missing = await self._series(region, q)
        values = rolling_mean(
            [float(row["heatwave_days"]) for row in series],
            window=ROLLING_WINDOW,
            min_periods=ROLLING_MIN_PERIODS,
        )
        return {
            "region": {
                "region_id": region["_id"],
                "name": region.get("name", ""),
                "kind": region.get("kind"),
            },
            "hazard": q.hazard,
            "range": q.range,
            "series": series,
            "rolling_mean": {
                "window": ROLLING_WINDOW,
                "align": "trailing",
                "metric": "heatwave_days",
                "values": [
                    {"year": row["year"], "value": value}
                    for row, value in zip(series, values, strict=True)
                    if value is not None
                ],
            },
            "meta": self._meta(region, caveats=self._series_caveats(series, missing)),
        }

    async def _build_summary(self, region: dict[str, Any], q: AnalysisQuery) -> dict[str, Any]:
        series, missing = await self._series(region, q)
        summary = analyze(series, method=q.method)
        payload = summary.to_dict()
        if missing:
            payload["caveats"].append(
                f"no usable data for year(s) {_years_text(missing)}; shown as gaps"
            )
        payload["meta"] = self._meta(region, caveats=list(payload["caveats"]))
        return payload

    async def _build_events(self, region: dict[str, Any], q: EventsQuery) -> dict[str, Any]:
        sort_dir, _ = q.sort_key()
        items, total = await self.events.page(
            region["_id"],
            q.hazard,
            self.settings.algo_version,
            q.start_year,
            q.end_year,
            min_duration=q.min_duration,
            sort_dir=sort_dir,
            skip=(q.page - 1) * q.page_size,
            limit=q.page_size,
        )
        return {
            "items": [_event_out(doc) for doc in items],
            "page": q.page,
            "page_size": q.page_size,
            "total": total,
            "meta": self._meta(region, caveats=self._events_caveats(region, q)),
        }

    def _day(self, doc: dict[str, Any], by_doy: dict[int, float | None]) -> dict[str, Any]:
        tmax = doc.get("tmax_c")
        quality = doc.get("quality", Quality.MISSING.value)
        threshold = by_doy.get(calendar_doy(doc["date"]))
        qualifies = (
            quality in {member.value for member in ASSESSABLE}
            and threshold is not None
            and tmax is not None
            and float(tmax) > threshold
        )
        return {
            "date": doc["date"],
            "tmax_c": tmax,
            "threshold": threshold,
            "qualifies": qualifies,
            "quality": quality,
        }

    # -- caveats / meta -----------------------------------------------------

    def _series_caveats(self, series: list[dict], missing: list[int]) -> list[str]:
        caveats: list[str] = []
        if series:
            mean_cov = round(sum(float(row["coverage_pct"]) for row in series) / len(series), 1)
            caveats.append(f"coverage: {mean_cov}% of days valid")
        low = [int(row["year"]) for row in series if float(row["coverage_pct"]) < 90.0]
        if low:
            caveats.append(f"Low data coverage in year(s) {_years_text(low)}; interpret with care")
        if missing:
            caveats.append(f"no usable data for year(s) {_years_text(missing)}; shown as gaps")
        return caveats

    def _events_caveats(self, region: dict[str, Any], q: EventsQuery) -> list[str]:
        coverage = region.get("coverage") or {}
        caveats: list[str] = []
        if coverage.get("end_year", q.end_year) < q.end_year:
            caveats.append(
                f"data coverage ends {coverage.get('end_year')}; later years are unavailable"
            )
        return caveats

    def _inspect_caveats(self, rows: list[dict[str, Any]]) -> list[str]:
        flagged = [row["date"] for row in rows if row["quality"] != Quality.VALID.value]
        if flagged:
            return ["some days have reduced quality; interpret thresholds with care"]
        return []

    def _meta(self, region: dict[str, Any], *, caveats: list[str]) -> dict[str, Any]:
        return {
            "algo_version": self.settings.algo_version,
            "data_version": region.get("data_version", "dv_0"),
            "generated_at": utcnow(),
            "source": "open-meteo-archive",
            "baseline": self.settings.baseline_period,
            "cache": "miss",  # overwritten by _marked() when served
            "caveats": caveats,
        }

    # -- L2 cache -----------------------------------------------------------

    def _key(self, endpoint: str, q: Any, region: dict[str, Any]) -> str:
        return cache_key(
            {
                "endpoint": endpoint,
                "query": q.model_dump(),
                "data_version": region.get("data_version", "dv_0"),
                "algo_version": self.settings.algo_version,
            }
        )

    def _store(self, key: str, payload: dict[str, Any], region: dict[str, Any]) -> None:
        response_cache.set(
            key,
            payload,
            ttl=self.settings.response_cache_ttl_s,
            region_id=region["_id"],
        )

    @staticmethod
    def _marked(payload: dict[str, Any], state: str) -> dict[str, Any]:
        meta = payload.get("meta")
        if isinstance(meta, dict):
            meta["cache"] = state
        return payload


# -- helpers -----------------------------------------------------------------


def _zero_row(year: int) -> dict[str, Any]:
    """Missing year → zeros + zero coverage, flagged for the FE (docs/02 §7.4)."""
    return {
        "year": year,
        "event_count": 0,
        "heatwave_days": 0,
        "mean_duration": 0.0,
        "max_duration": 0,
        "mean_exceedance": 0.0,
        "coverage_pct": 0.0,
    }


def _event_out(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(doc["_id"]),
        "start_date": doc["start_date"],
        "end_date": doc["end_date"],
        "year": doc["year"],
        "duration_days": doc["duration_days"],
        "mean_exceedance": doc["mean_exceedance"],
        "peak_exceedance": doc["peak_exceedance"],
    }


def _years_text(years: list[int]) -> str:
    text = ", ".join(str(year) for year in years[:12])
    if len(years) > 12:
        text += ", …"
    return text


def _year_start(year: int) -> date:
    return date(year, 1, 1)


def _year_end(year: int) -> date:
    return date(year, 12, 31)


__all__ = ["AnalysisService"]
