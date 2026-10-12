"""Scripted backfill CLI (docs/05 §2 P1 DoD).

Usage::

    python -m app.cli backfill --bbox 17.1,78.3,17.7,78.9 --name "My box"
    python -m app.cli backfill --point 17.385,78.4867 --name Hyderabad
    python -m app.cli indexes

Prints a JSON run summary on stdout; exit code 0 = ingested, 1 = failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from typing import Any

from app.core.config import Settings, get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close, get_db
from app.db.repositories import IngestAlreadyRunning
from app.models.ingest import IngestStatus, IngestTrigger
from app.models.region import RegionSpec, bbox_spec, point_spec
from app.providers.base import ProviderError
from app.providers.open_meteo import OpenMeteoProvider
from app.services.ingest import IngestService
from app.services.region import RegionBuilder


def _pair(raw: str) -> tuple[float, float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected lat,lon")
    return float(parts[0]), float(parts[1])


def _bbox(raw: str) -> list[float]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected min_lat,min_lon,max_lat,max_lon")
    return [float(p) for p in parts]


def default_end() -> date:
    """Last full year (DEC-04/DEC-17: the partial current year is excluded)."""
    return date(date.today().year - 1, 12, 31)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="ingest a window for one region")
    target = backfill.add_mutually_exclusive_group(required=True)
    target.add_argument("--bbox", type=_bbox, help="min_lat,min_lon,max_lat,max_lon")
    target.add_argument("--point", type=_pair, help="lat,lon (city)")
    backfill.add_argument("--name", default="", help="display name (≤80 chars)")
    backfill.add_argument("--kind", choices=["city", "state", "bbox"], default=None)
    backfill.add_argument("--start", default="1950-01-01", help="YYYY-MM-DD")
    backfill.add_argument("--end", default=None, help="YYYY-MM-DD (default: last full year)")

    sub.add_parser("indexes", help="create/refresh validators and indexes")
    return parser


async def _indexes(settings: Settings) -> int:
    db = get_db()
    await ensure_indexes(db, daily_ttl_days=settings.daily_weather_ttl_days)
    print(json.dumps({"status": "ok", "db": settings.mongodb_db}))
    return 0


async def _backfill(args: argparse.Namespace, settings: Settings) -> int:
    db = get_db()
    await ensure_indexes(db, daily_ttl_days=settings.daily_weather_ttl_days)

    if args.bbox is not None:
        spec: RegionSpec = bbox_spec(args.bbox)
        kind = args.kind or "bbox"
    else:
        lat, lon = args.point
        spec = point_spec(lat, lon)
        kind = args.kind or "city"

    region_id, created = await RegionBuilder(db).ensure(kind=kind, name=args.name, spec=spec)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else default_end()
    if start > end:
        raise SystemExit(f"--start {start} is after --end {end}")

    provider = OpenMeteoProvider(settings)
    service = IngestService(db, provider, settings=settings)
    summary: dict[str, Any]
    try:
        try:
            result = await service.run(region_id, start, end, IngestTrigger.CLI)
        except IngestAlreadyRunning:
            # Single-flight: join the run already in flight (docs/02 §12)
            waited = await service.wait_for_idle(region_id)
            runs = await service.runs.recent(region_id, limit=1)
            latest = runs[0] if runs else {}
            result = {
                "run_id": str(latest.get("_id", "")),
                "region_id": region_id,
                "status": latest.get("status", "unknown"),
                "counts": latest.get("counts", {}),
                "joined_existing_run": True,
                "waited": waited,
            }
        summary = {"region_id": region_id, "created": created, **result}
    except ProviderError as exc:
        summary = {
            "region_id": region_id,
            "created": created,
            "status": "failed",
            "error": {"code": exc.code, "message": str(exc)[:500]},
        }
    finally:
        await provider.aclose()

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == IngestStatus.SUCCESS.value else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        if args.command == "indexes":
            code = asyncio.run(_indexes(settings))
        else:
            code = asyncio.run(_backfill(args, settings))
    finally:
        close()
    return code


if __name__ == "__main__":
    sys.exit(main())
