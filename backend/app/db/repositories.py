"""Typed Mongo repositories (docs/02 §6.1–6.6).

Repositories own document shapes and index-aware queries; they never call the
weather provider and never touch HTTP (docs/02 §4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.models.ingest import IngestStatus, IngestTrigger, Window
from app.models.weather import Quality, QualityRow

UPSERT_BATCH = 5000  # docs/02 §9: bulk upserts batched 5k docs


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IngestAlreadyRunning(Exception):
    """Another ingest for this region holds the single-flight slot (§12)."""

    def __init__(self, region_id: str, run_id: ObjectId | None = None) -> None:
        super().__init__(f"ingest already running for {region_id}")
        self.region_id = region_id
        self.run_id = run_id


class RegionRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["regions"]

    async def get(self, region_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"_id": region_id})

    async def insert_if_absent(self, doc: dict[str, Any]) -> bool:
        """Idempotent create (docs/02 §12): second insert hits the unique _id."""
        try:
            await self._col.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    async def set_status(
        self,
        region_id: str,
        status: str,
        *,
        error: dict[str, str] | None = None,
        session: Any = None,
    ) -> None:
        update: dict[str, Any] = {
            "$set": {"status": status, "updated_at": utcnow()},
            "$unset": {"last_error": ""},
        }
        if error is not None:
            update["$set"]["last_error"] = error
            update.pop("$unset")
        await self._col.update_one({"_id": region_id}, update, session=session)

    async def mark_ready(
        self,
        region_id: str,
        *,
        data_version: str,
        coverage: dict[str, int],
        session: Any = None,
    ) -> None:
        """Status + data_version bump land together (docs/02 §13 read-your-writes)."""
        await self._col.update_one(
            {"_id": region_id},
            {
                "$set": {
                    "status": "ready",
                    "data_version": data_version,
                    "coverage": coverage,
                    "updated_at": utcnow(),
                },
                "$unset": {"last_error": ""},
            },
            session=session,
        )

    @staticmethod
    def next_data_version(current: str | None) -> str:
        """``dv_12`` → ``dv_13`` (docs/02 §6.1 example format)."""
        if current and current.startswith("dv_") and current[3:].isdigit():
            return f"dv_{int(current[3:]) + 1}"
        return "dv_1"


class DailyWeatherRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["daily_weather"]

    async def bulk_upsert(
        self,
        region_id: str,
        rows: list[QualityRow],
        *,
        source: str,
        fetched_at: str,
        session: Any = None,
    ) -> int:
        """Last-write-wins upsert keyed ``{region_id, date}`` (docs/02 §6.2, EC-03)."""
        ops = [
            UpdateOne(
                {"region_id": region_id, "date": row.date},
                {
                    "$set": {
                        "region_id": region_id,
                        "date": row.date,
                        "year": row.year,
                        "doy": row.doy,
                        "tmax_c": row.tmax_c,
                        "tmin_c": row.tmin_c,
                        "precip_mm": row.precip_mm,
                        "quality": row.quality.value,
                        "grid_cells_ok": row.grid_cells_ok,
                        "source": source,
                        "fetched_at": fetched_at,
                    }
                },
                upsert=True,
            )
            for row in rows
        ]
        written = 0
        for start in range(0, len(ops), UPSERT_BATCH):
            result = await self._col.bulk_write(
                ops[start : start + UPSERT_BATCH], ordered=False, session=session
            )
            written += result.upserted_count + result.modified_count
        return written

    async def count(self, region_id: str) -> int:
        return await self._col.count_documents({"region_id": region_id})

    async def rows_for_window(
        self, region_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Raw daily docs for the inspect drill-down (docs/02 §7.4)."""
        cursor = self._col.find(
            {"region_id": region_id, "date": {"$gte": start_date, "$lte": end_date}},
            {"_id": 0, "date": 1, "tmax_c": 1, "quality": 1},
        ).sort("date", 1)
        return await cursor.to_list(length=None)

    async def baseline_rows(
        self, region_id: str, start_year: int, end_year: int
    ) -> list[QualityRow]:
        """Persisted daily rows for the threshold baseline years (docs/02 §5.1).

        Used when a window does not cover the baseline and no threshold cache
        exists yet (e.g. right after an ``algo_version`` bump, §14).
        """
        cursor = self._col.find(
            {"region_id": region_id, "year": {"$gte": start_year, "$lte": end_year}},
            {"_id": 0},
        ).sort("date", 1)
        docs = await cursor.to_list(length=None)
        return [
            QualityRow(
                date=doc["date"],
                year=doc["year"],
                doy=doc["doy"],
                tmax_c=doc.get("tmax_c"),
                tmin_c=doc.get("tmin_c"),
                precip_mm=doc.get("precip_mm"),
                quality=Quality(doc["quality"]),
                grid_cells_ok=doc.get("grid_cells_ok", 1),
            )
            for doc in docs
        ]


class ThresholdRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["threshold_cache"]

    async def get(self, region_id: str, algo_version: str) -> dict[str, Any] | None:
        return await self._col.find_one({"region_id": region_id, "algo_version": algo_version})

    async def upsert(self, doc: dict[str, Any], *, session: Any = None) -> None:
        await self._col.replace_one(
            {
                "region_id": doc["region_id"],
                "algo_version": doc["algo_version"],
            },
            doc,
            upsert=True,
            session=session,
        )


class HazardEventRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["hazard_events"]

    async def replace_window(
        self,
        region_id: str,
        hazard: str,
        window: Window,
        events: list[dict[str, Any]],
        *,
        session: Any = None,
    ) -> None:
        """Delete+insert under the same transaction (docs/02 §13).

        The filter matches by *overlap* so a streak starting before the window
        but continuing inside it is recomputed too.
        """
        await self._col.delete_many(
            {
                "region_id": region_id,
                "hazard": hazard,
                "start_date": {"$lte": window.end},
                "end_date": {"$gte": window.start},
            },
            session=session,
        )
        if events:
            await self._col.insert_many(events, session=session)

    async def count(self, region_id: str, hazard: str) -> int:
        return await self._col.count_documents({"region_id": region_id, "hazard": hazard})

    async def page(
        self,
        region_id: str,
        hazard: str,
        algo_version: str,
        start_year: int,
        end_year: int,
        *,
        min_duration: int | None = None,
        sort_dir: int = -1,
        skip: int = 0,
        limit: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paged event list filtered by algo_version + year (EC-21, TC-I-15)."""
        query: dict[str, Any] = {
            "region_id": region_id,
            "hazard": hazard,
            "algo_version": algo_version,
            "year": {"$gte": start_year, "$lte": end_year},
        }
        if min_duration is not None:
            query["duration_days"] = {"$gte": min_duration}
        total = await self._col.count_documents(query)
        cursor = (
            self._col.find(query)
            .sort([("start_date", sort_dir), ("_id", sort_dir)])
            .skip(skip)
            .limit(limit)
        )
        return await cursor.to_list(length=None), total

    async def find_by_start(
        self,
        region_id: str,
        hazard: str,
        algo_version: str,
        start_date: str,
    ) -> dict[str, Any] | None:
        """Event lookup for the inspect drill-down (404 when absent)."""
        return await self._col.find_one(
            {
                "region_id": region_id,
                "hazard": hazard,
                "algo_version": algo_version,
                "start_date": start_date,
            }
        )


class HazardAnnualRepository:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["hazard_annual_stats"]

    async def bulk_upsert(self, rows: list[dict[str, Any]], *, session: Any = None) -> None:
        ops = [
            UpdateOne(
                {
                    "region_id": row["region_id"],
                    "hazard": row["hazard"],
                    "year": row["year"],
                    "algo_version": row["algo_version"],
                },
                {"$set": row},
                upsert=True,
            )
            for row in rows
        ]
        for start in range(0, len(ops), UPSERT_BATCH):
            await self._col.bulk_write(
                ops[start : start + UPSERT_BATCH], ordered=False, session=session
            )

    async def count(self, region_id: str, hazard: str) -> int:
        return await self._col.count_documents({"region_id": region_id, "hazard": hazard})

    async def rows(
        self,
        region_id: str,
        hazard: str,
        algo_version: str,
        start_year: int,
        end_year: int,
    ) -> list[dict[str, Any]]:
        """Annual rows for the requested range (EC-21: filtered by algo_version)."""
        cursor = self._col.find(
            {
                "region_id": region_id,
                "hazard": hazard,
                "algo_version": algo_version,
                "year": {"$gte": start_year, "$lte": end_year},
            },
            {"_id": 0, "region_id": 0, "hazard": 0, "algo_version": 0, "data_version": 0},
        ).sort("year", 1)
        return await cursor.to_list(length=None)

    async def years(
        self,
        region_id: str,
        hazard: str,
        algo_version: str,
        start_year: int,
        end_year: int,
    ) -> set[int]:
        """Years that already have stats — the on-demand gap check (CF-05)."""
        cursor = self._col.find(
            {
                "region_id": region_id,
                "hazard": hazard,
                "algo_version": algo_version,
                "year": {"$gte": start_year, "$lte": end_year},
            },
            {"_id": 0, "year": 1},
        )
        return {int(doc["year"]) async for doc in cursor}


class IngestRunRepository:
    """Also the durable single-flight lock (docs/02 §12)."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db["ingest_runs"]

    async def start(self, region_id: str, window: Window, trigger: IngestTrigger) -> ObjectId:
        doc = {
            "region_id": region_id,
            "window": window.model_dump(),
            "status": IngestStatus.RUNNING.value,
            "attempts": 1,
            "trigger": trigger.value,
            "counts": {},
            "error": None,
            "started_at": utcnow(),
            "finished_at": None,
        }
        try:
            result = await self._col.insert_one(doc)
            return result.inserted_id
        except DuplicateKeyError as exc:
            existing = await self.get_running(region_id)
            raise IngestAlreadyRunning(region_id, existing["_id"] if existing else None) from exc

    async def get_running(self, region_id: str) -> dict[str, Any] | None:
        return await self._col.find_one(
            {"region_id": region_id, "status": IngestStatus.RUNNING.value}
        )

    async def finish(
        self,
        run_id: ObjectId,
        *,
        status: IngestStatus,
        attempts: int = 1,
        counts: dict[str, int] | None = None,
        error: dict[str, str] | None = None,
        duration_ms: int | None = None,
        session: Any = None,
    ) -> None:
        await self._col.update_one(
            {"_id": run_id},
            {
                "$set": {
                    "status": status.value,
                    "attempts": max(attempts, 1),
                    "counts": counts or {},
                    "error": error,
                    "duration_ms": duration_ms,
                    "finished_at": utcnow(),
                }
            },
            session=session,
        )

    async def recent(self, region_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = {"region_id": region_id} if region_id else {}
        cursor = self._col.find(query).sort("started_at", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def count(self, region_id: str | None = None) -> int:
        query = {"region_id": region_id} if region_id else {}
        return await self._col.count_documents(query)

    async def page(
        self,
        *,
        region_id: str | None = None,
        skip: int = 0,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        query = {"region_id": region_id} if region_id else {}
        cursor = self._col.find(query).sort("started_at", -1).skip(skip).limit(limit)
        return await cursor.to_list(length=limit)

    async def fail_stale(self, older_than: str) -> list[dict[str, Any]]:
        """Sweeper: orphaned ``running`` runs → ``failed_retryable`` (§14.3)."""
        stale = await self._col.find(
            {"status": IngestStatus.RUNNING.value, "started_at": {"$lt": older_than}}
        ).to_list(length=None)
        for doc in stale:
            await self._col.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "status": IngestStatus.FAILED_RETRYABLE.value,
                        "error": {
                            "code": "RUN_STALE",
                            "message": "run exceeded the stale threshold; requeued",
                        },
                        "finished_at": utcnow(),
                    }
                },
            )
        return stale
