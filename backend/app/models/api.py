"""API validation models — V-01..V-16 (docs/02 §8, NFR-06).

Two layers:

* **Pydantic** enforces the single-field rules (types, bounds, enums, regexes)
  so FastAPI renders them as ``422 VALIDATION_ERROR`` with a ``details.field``
  taken from the offending location;
* **cross-field rules** (V-01 range order, V-02 dynamic upper bound, V-04
  hazard registry membership, V-06/V-16 bbox geometry) raise
  :class:`~app.core.errors.ApiError` from the dependency that owns them, with
  an explicit ``details.field`` — the server is authoritative, the FE only
  mirrors these rules (docs/02 §8).

Query models are exposed as FastAPI *dependencies* (``Depends``) rather than
body models, so they parse from the query string while still validating as a
single unit.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Self

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ApiError
from app.hazard.registry import HAZARDS
from app.models.weather import DATE_RE

REGION_ID_RE = re.compile(r"^r_[0-9a-f]{16}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MIN_YEAR = 1950
Method = Literal["ols", "mann-kendall", "both"]
Sort = Literal["start_date:asc", "start_date:desc"]
RegionKind = Literal["city", "state", "bbox"]


def current_year() -> int:
    return date.today().year


def last_full_year() -> int:
    """DEC-04/DEC-17: the partial current year is excluded by default."""
    return current_year() - 1


def _field_error(field: str, reason: str, *, code: str = "VALIDATION_ERROR") -> ApiError:
    return ApiError(code, reason, details={"field": field, "reason": reason})


DEFAULT_START_YEAR = 1990
DEFAULT_END_YEAR = 2020


class AnalysisQuery(BaseModel):
    """Common query schema shared by every ``/analysis/*`` GET (§7.4)."""

    model_config = ConfigDict(extra="ignore")

    region_id: str
    hazard: str = "heatwave"
    start_year: int = Field(default=DEFAULT_START_YEAR, ge=MIN_YEAR, le=2200)
    end_year: int = Field(default=DEFAULT_END_YEAR, ge=MIN_YEAR, le=2200)
    include_current_year: bool = False
    method: Method = "ols"

    @field_validator("region_id")
    @classmethod
    def _check_region_id(cls, value: str) -> str:
        if not REGION_ID_RE.match(value):
            raise ValueError("region_id must match ^r_[0-9a-f]{16}$")
        return value

    @property
    def range(self) -> dict:
        return {
            "start_year": self.start_year,
            "end_year": self.end_year,
            "include_current_year": self.include_current_year,
        }

    def validate_rules(self) -> Self:
        """V-01, V-02, V-04 — cross-field rules with explicit fields."""
        if self.start_year > self.end_year:
            raise _field_error("start_year", "start_year must be <= end_year")
        if self.end_year > last_full_year() and not (
            self.include_current_year and self.end_year == current_year()
        ):
            raise _field_error(
                "end_year",
                f"end_year must be <= {last_full_year()} "
                f"(set include_current_year=true to include {current_year()})",
            )
        if self.hazard not in HAZARDS:
            supported = ", ".join(sorted(HAZARDS))
            raise _field_error(
                "hazard",
                f"unsupported hazard {self.hazard!r}; supported: {supported}",
            )
        return self


class EventsQuery(AnalysisQuery):
    """Analysis params + events-only paging/filter/sort (V-10..V-12)."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    min_duration: int | None = Field(default=None, ge=1)
    sort: Sort = "start_date:desc"

    def sort_key(self) -> tuple[int, int]:
        direction = 1 if self.sort.endswith(":asc") else -1
        return direction, direction  # (start_date, _id) share one direction


class InspectQuery(BaseModel):
    """``GET /analysis/inspect`` params (§7.4)."""

    model_config = ConfigDict(extra="ignore")

    region_id: str
    hazard: str = "heatwave"
    start_date: str

    @field_validator("region_id")
    @classmethod
    def _check_region_id(cls, value: str) -> str:
        if not REGION_ID_RE.match(value):
            raise ValueError("region_id must match ^r_[0-9a-f]{16}$")
        return value

    @field_validator("start_date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError("start_date must be YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    def validate_rules(self) -> Self:
        if self.hazard not in HAZARDS:
            supported = ", ".join(sorted(HAZARDS))
            raise _field_error(
                "hazard",
                f"unsupported hazard {self.hazard!r}; supported: {supported}",
            )
        return self


def analysis_query(
    region_id: str = Query(..., description="r_ + 16 hex chars"),
    hazard: str = Query("heatwave"),
    start_year: int = Query(DEFAULT_START_YEAR, ge=MIN_YEAR, le=2200),
    end_year: int = Query(DEFAULT_END_YEAR, ge=MIN_YEAR, le=2200),
    include_current_year: bool = Query(False),
    method: Method = Query("ols"),
) -> AnalysisQuery:
    return AnalysisQuery(
        region_id=region_id,
        hazard=hazard,
        start_year=start_year,
        end_year=end_year,
        include_current_year=include_current_year,
        method=method,
    ).validate_rules()


def events_query(
    base: AnalysisQuery = Depends(analysis_query),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    min_duration: int | None = Query(None, ge=1),
    sort: Sort = Query("start_date:desc"),
) -> EventsQuery:
    return EventsQuery(
        **base.model_dump(),
        page=page,
        page_size=page_size,
        min_duration=min_duration,
        sort=sort,
    )


def inspect_query(
    region_id: str = Query(...),
    hazard: str = Query("heatwave"),
    start_date: str = Query(...),
) -> InspectQuery:
    return InspectQuery(region_id=region_id, hazard=hazard, start_date=start_date).validate_rules()


# --- region endpoints (docs/02 §7.3) ---------------------------------------


class PointBody(BaseModel):
    """V-07: point coordinates in range."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class BboxBody(BaseModel):
    """V-06/V-16: bbox geometry (raw user input only — presets bypass this)."""

    min_lat: float = Field(ge=-90, le=90)
    min_lon: float = Field(ge=-180, le=180)
    max_lat: float = Field(ge=-90, le=90)
    max_lon: float = Field(ge=-180, le=180)

    def validate_rules(self) -> list[float]:
        if self.min_lon > self.max_lon:
            # V-16: documented limitation — an antimeridian box would need
            # wrapped longitudes the 3×3 grid maths cannot express.
            raise _field_error(
                "bbox",
                "bbox crosses the antimeridian (min_lon > max_lon); "
                "not supported — draw two boxes instead",
            )
        if self.min_lat >= self.max_lat:
            raise _field_error("bbox", "min_lat must be < max_lat")
        if self.min_lon >= self.max_lon:
            raise _field_error("bbox", "min_lon must be < max_lon")
        span_lat = self.max_lat - self.min_lat
        span_lon = self.max_lon - self.min_lon
        if span_lat > 10 or span_lon > 10:
            raise _field_error("bbox", "bbox span must be <= 10 degrees per side")
        if span_lat * span_lon > 25:
            raise _field_error("bbox", "Box too large — reduce to about 250 km per side")
        return [self.min_lat, self.min_lon, self.max_lat, self.max_lon]


class RegionCreateBody(BaseModel):
    """``POST /regions`` payload (docs/02 §7.3): a preset reference *or* a raw
    point/bbox spec. Raw bboxes are capped by V-06; curated preset boxes
    (states larger than 25 deg²) are trusted because they ship in the repo."""

    model_config = ConfigDict(extra="ignore")

    preset_id: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    kind: RegionKind | None = None
    name: str = Field(default="", max_length=80)
    point: PointBody | None = None
    bbox: BboxBody | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        value = value.strip()
        if CONTROL_RE.search(value):
            raise ValueError("name must not contain control characters")
        return value[:80]

    def validate_rules(self) -> Self:
        """V-08 already ran; now the structural rules (kind/point/bbox)."""
        if self.preset_id:
            return self
        if self.kind is None:
            raise _field_error("kind", "kind is required when preset_id is absent")
        if self.kind == "city":
            if self.point is None:
                raise _field_error("point", "point is required when kind=city")
        elif self.bbox is None:
            raise _field_error("bbox", f"bbox is required when kind={self.kind}")
        else:
            self.bbox.validate_rules()
        return self


class SearchQuery(BaseModel):
    """V-09 geocode query."""

    model_config = ConfigDict(extra="ignore")

    q: str

    @field_validator("q")
    @classmethod
    def _check_q(cls, value: str) -> str:
        value = value.strip()
        if not 2 <= len(value) <= 80:
            raise ValueError("q must be 2-80 characters")
        if CONTROL_RE.search(value):
            raise ValueError("q must not contain control characters")
        return value


def search_query(q: str = Query(..., min_length=2, max_length=80)) -> SearchQuery:
    return SearchQuery(q=q)


def presets_query(
    type: str | None = Query(None, pattern="^(city|state)$"),
) -> str | None:
    return type


# --- admin (docs/02 §7.5) --------------------------------------------------


class RecomputeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    region_id: str
    rebuild_thresholds: bool = False

    @field_validator("region_id")
    @classmethod
    def _check_region_id(cls, value: str) -> str:
        if not REGION_ID_RE.match(value):
            raise ValueError("region_id must match ^r_[0-9a-f]{16}$")
        return value


class AdminRunsQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    region_id: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)

    @field_validator("region_id")
    @classmethod
    def _check_region_id(cls, value: str | None) -> str | None:
        if value is not None and not REGION_ID_RE.match(value):
            raise ValueError("region_id must match ^r_[0-9a-f]{16}$")
        return value


def admin_runs_query(
    region_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> AdminRunsQuery:
    return AdminRunsQuery(region_id=region_id, page=page, page_size=page_size)


class ClientErrorBody(BaseModel):
    """FE error beacon (docs/02 §7.1) — logged, never stored with PII."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(default="", max_length=500)
    code: str = Field(default="CLIENT_ERROR", max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=300)


__all__ = [
    "AnalysisQuery",
    "BboxBody",
    "ClientErrorBody",
    "EventsQuery",
    "InspectQuery",
    "PointBody",
    "RecomputeBody",
    "RegionCreateBody",
    "SearchQuery",
    "admin_runs_query",
    "analysis_query",
    "current_year",
    "events_query",
    "inspect_query",
    "last_full_year",
    "presets_query",
    "search_query",
]
