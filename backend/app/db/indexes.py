"""Collection validators and indexes (docs/02 §6.1–6.6, §12).

Idempotent: safe to call on every boot/CLI invocation. Validation level is
``moderate`` so hand-edited dev documents are reported rather than rejected
outright, while new writes still have to satisfy the schema (DEC-01).
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

log = logging.getLogger(__name__)

# --- $jsonSchema validators (docs/02 §6) -----------------------------------

_REGION_SPEC = {
    "bsonType": "object",
    "required": ["mode", "points"],
    "properties": {
        "mode": {"enum": ["point", "grid"]},
        "points": {"bsonType": "array", "minItems": 1, "maxItems": 9},
        "bbox": {"bsonType": ["array", "null"]},
    },
}

VALIDATORS: dict[str, dict] = {
    "regions": {
        "bsonType": "object",
        "required": ["kind", "name", "spec", "status", "data_version"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "kind": {"enum": ["city", "state", "bbox"]},
            "name": {"bsonType": "string", "maxLength": 80},
            "spec": _REGION_SPEC,
            "status": {"enum": ["backfilling", "ready", "failed"]},
            "data_version": {"bsonType": "string", "pattern": "^dv_[0-9]+$"},
            "coverage": {"bsonType": ["object", "null"]},
            "created_at": {"bsonType": "string"},
            "updated_at": {"bsonType": "string"},
        },
    },
    "daily_weather": {
        "bsonType": "object",
        "required": ["region_id", "date", "year", "doy", "quality", "source", "fetched_at"],
        "properties": {
            "region_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "date": {"bsonType": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "year": {"bsonType": "int", "minimum": 1900, "maximum": 2200},
            "doy": {"bsonType": "int", "minimum": 1, "maximum": 366},
            "tmax_c": {"bsonType": ["double", "int", "null"]},
            "tmin_c": {"bsonType": ["double", "int", "null"]},
            "precip_mm": {"bsonType": ["double", "int", "null"]},
            "quality": {"enum": ["valid", "missing", "outlier", "degraded_grid"]},
            "grid_cells_ok": {"bsonType": "int", "minimum": 0, "maximum": 9},
            "source": {"bsonType": "string"},
            "fetched_at": {"bsonType": "string"},
        },
    },
    "threshold_cache": {
        "bsonType": "object",
        "required": ["region_id", "baseline", "algo_version", "by_doy", "computed_at"],
        "properties": {
            "region_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "baseline": {"bsonType": "string"},
            "algo_version": {"bsonType": "string"},
            "by_doy": {"bsonType": "object"},
            "fallback_flags": {"bsonType": "object"},
            "n_unresolved_doys": {"bsonType": "int"},
            "computed_at": {"bsonType": "string"},
        },
    },
    "hazard_events": {
        "bsonType": "object",
        "required": [
            "region_id",
            "hazard",
            "start_date",
            "end_date",
            "year",
            "duration_days",
            "algo_version",
            "data_version",
        ],
        "properties": {
            "region_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "hazard": {"bsonType": "string"},
            "start_date": {"bsonType": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "end_date": {"bsonType": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "year": {"bsonType": "int"},
            "duration_days": {"bsonType": "int", "minimum": 1},
            "mean_exceedance": {"bsonType": ["double", "int", "null"]},
            "peak_exceedance": {"bsonType": ["double", "int", "null"]},
            "algo_version": {"bsonType": "string"},
            "data_version": {"bsonType": "string"},
        },
    },
    "hazard_annual_stats": {
        "bsonType": "object",
        "required": [
            "region_id",
            "hazard",
            "year",
            "event_count",
            "heatwave_days",
            "coverage_pct",
            "algo_version",
            "data_version",
        ],
        "properties": {
            "region_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "hazard": {"bsonType": "string"},
            "year": {"bsonType": "int", "minimum": 1900, "maximum": 2200},
            "event_count": {"bsonType": "int", "minimum": 0},
            "heatwave_days": {"bsonType": "int", "minimum": 0},
            "coverage_pct": {"bsonType": ["double", "int"], "minimum": 0, "maximum": 100},
            "algo_version": {"bsonType": "string"},
            "data_version": {"bsonType": "string"},
            "computed_at": {"bsonType": "string"},
        },
    },
    "ingest_runs": {
        "bsonType": "object",
        "required": ["region_id", "window", "status", "trigger", "started_at"],
        "properties": {
            "region_id": {"bsonType": "string", "pattern": "^r_[0-9a-f]{16}$"},
            "window": {
                "bsonType": "object",
                "required": ["start", "end"],
                "properties": {
                    "start": {"bsonType": "string"},
                    "end": {"bsonType": "string"},
                },
            },
            "status": {"enum": ["running", "success", "failed_retryable", "failed_fatal"]},
            "attempts": {"bsonType": "int", "minimum": 1},
            "trigger": {"enum": ["scheduler", "on_demand", "admin", "cli"]},
            "counts": {"bsonType": "object"},
            "error": {"bsonType": ["object", "null"]},
            "duration_ms": {"bsonType": ["int", "null"]},
            "started_at": {"bsonType": "string"},
            "finished_at": {"bsonType": ["string", "null"]},
        },
    },
}

# --- Indexes (docs/02 §6.1–6.6, §12) ---------------------------------------

# TTL marker: resolved at runtime from settings (0 = keep rows in dev).
_TTL = "__ttl__"

INDEXES: dict[str, list[tuple[dict, dict]]] = {
    "regions": [
        ({"status": 1, "updated_at": 1}, {}),  # scheduler scan (§6.1)
    ],
    "daily_weather": [
        # Idempotent upserts — duplicate ingestion cannot double-write (§6.2)
        ({"region_id": 1, "date": 1}, {"unique": True}),
        ({"region_id": 1, "year": 1}, {}),  # annual recompute scans
        ({"fetched_at": 1}, {_TTL: True}),
    ],
    "threshold_cache": [
        ({"region_id": 1, "algo_version": 1}, {"unique": True}),
    ],
    "hazard_events": [
        ({"region_id": 1, "hazard": 1, "start_date": 1, "algo_version": 1}, {"unique": True}),
        # events listing by year, longest first
        ({"region_id": 1, "hazard": 1, "year": 1, "duration_days": -1}, {}),
    ],
    # The unique key below also serves {region_id, hazard, year} prefix queries,
    # so docs/02 §6.5's extra non-unique index would be redundant.
    "hazard_annual_stats": [
        ({"region_id": 1, "hazard": 1, "year": 1, "algo_version": 1}, {"unique": True}),
    ],
    "ingest_runs": [
        ({"region_id": 1, "started_at": -1}, {}),
        # Single-flight: at most ONE running run per region (docs/02 §12)
        (
            {"region_id": 1, "status": 1},
            {"unique": True, "partialFilterExpression": {"status": "running"}},
        ),
    ],
}


async def ensure_indexes(db: AsyncIOMotorDatabase, *, daily_ttl_days: int | None = None) -> None:
    """Create/refresh validators + indexes for every collection in docs/02 §6."""
    for name, validator in VALIDATORS.items():
        await _ensure_collection(db, name, validator)

    for name, specs in INDEXES.items():
        for keys, options in specs:
            options = dict(options)
            ttl = options.pop(_TTL, False)
            if ttl:
                # docs/02 §6.2: ≥5 years in prod; 0/None in dev keeps fixtures
                ttl_days = daily_ttl_days
                if ttl_days is None:
                    from app.core.config import get_settings

                    ttl_days = get_settings().daily_weather_ttl_days
                if not ttl_days:
                    continue
                options["expireAfterSeconds"] = int(ttl_days) * 86400
            try:
                await db[name].create_index(list(keys.items()), **options)
            except OperationFailure as exc:  # pragma: no cover — index conflicts
                log.warning("index creation failed on %s: %s", name, exc)


async def _ensure_collection(db: AsyncIOMotorDatabase, name: str, validator: dict) -> None:
    # $jsonSchema must be wrapped: `validator` is a query document, not a schema
    body = {"validator": {"$jsonSchema": validator}, "validationLevel": "moderate"}
    try:
        await db.command({"create": name, **body})
    except OperationFailure as exc:
        # 48 = NamespaceExists → refresh the validator on the existing collection
        if exc.code not in (48, 26):
            raise
        await db.command({"collMod": name, **body})
