"""Region spec, canonical hashing, and grid construction (docs/02 §6.1, §5.2).

The region ``_id`` is idempotent by construction (DEC-23, TC-U-18): the same
geography always hashes to the same id, so double-submits and renames cannot
create duplicate documents.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field

RegionKind = Literal["city", "state", "bbox"]

GRID_FRACTIONS: tuple[float, ...] = (0.1, 0.5, 0.9)
"""Deterministic 3×3 grid fractions (docs/02 §5.2) — fixed every ingest."""

REGION_STATUSES = ("backfilling", "ready", "failed")


class Point(BaseModel):
    lat: float
    lon: float


class RegionSpec(BaseModel):
    """Canonical, replayable region description (docs/02 §6.1)."""

    mode: Literal["point", "grid"]
    points: list[Point] = Field(min_length=1, max_length=9)
    bbox: list[float] | None = None  # [min_lat, min_lon, max_lat, max_lon]

    def canonical(self) -> dict:
        """Rounded, key-sorted payload — the input to the id hash.

        Coordinates are rounded to 4 dp ROUND_HALF_UP so float noise
        (``17.3850`` vs ``17.385``) cannot mint a second region.
        """
        payload: dict = {
            "mode": self.mode,
            "points": [{"lat": _round4(p.lat), "lon": _round4(p.lon)} for p in self.points],
        }
        if self.bbox is not None:
            payload["bbox"] = [_round4(v) for v in self.bbox]
        return payload


def _round4(value: float) -> float:
    quantized = Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(quantized)


def region_id_from_spec(spec: RegionSpec) -> str:
    """``r_`` + sha256(canonical spec)[:16] — docs/02 §6.1 steps 1–4."""
    raw = json.dumps(spec.canonical(), sort_keys=True, separators=(",", ":"))
    return "r_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def grid_points(bbox: Sequence[float]) -> list[Point]:
    """3×3 lat/lon grid inside ``bbox`` (docs/02 §5.2), row-major."""
    min_lat, min_lon, max_lat, max_lon = bbox
    return [
        Point(
            lat=min_lat + fi * (max_lat - min_lat),
            lon=min_lon + fj * (max_lon - min_lon),
        )
        for fi in GRID_FRACTIONS
        for fj in GRID_FRACTIONS
    ]


def point_spec(lat: float, lon: float) -> RegionSpec:
    return RegionSpec(mode="point", points=[Point(lat=lat, lon=lon)])


def bbox_spec(bbox: Sequence[float]) -> RegionSpec:
    if len(bbox) != 4:
        raise ValueError("bbox must be [min_lat, min_lon, max_lat, max_lon]")
    return RegionSpec(mode="grid", points=grid_points(list(bbox)), bbox=list(bbox))
