"""Domain models for daily weather readings (docs/02 §6.2).

Dates are opaque ``YYYY-MM-DD`` strings (DEC-15) — never parsed into
timestamps, so no DST/timezone arithmetic can creep in (EC-14).
"""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse(value: str) -> _date:
    if not DATE_RE.match(value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    return _date.fromisoformat(value)


class Quality(StrEnum):
    """Row quality classes from the data-quality pipeline (docs/02 §5.6)."""

    VALID = "valid"
    MISSING = "missing"
    OUTLIER = "outlier"
    DEGRADED_GRID = "degraded_grid"


class DailyReading(BaseModel):
    """One provider-normalized daily reading for one grid cell (or point)."""

    date: str
    cell: int = 0
    tmax_c: float | None = None
    tmin_c: float | None = None
    precip_mm: float | None = None

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse(value)
        return value


class CombinedReading(BaseModel):
    """One region-day after grid combination (docs/02 §5.2), pre-quality."""

    date: str
    tmax_c: float | None = None
    tmin_c: float | None = None
    precip_mm: float | None = None
    cells_ok: int = 1

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse(value)
        return value


class QualityRow(BaseModel):
    """A region-day that passed the quality pipeline (persistable, docs/02 §6.2)."""

    date: str
    year: int
    doy: int  # actual day-of-year 1..366
    tmax_c: float | None = None
    tmin_c: float | None = None
    precip_mm: float | None = None
    quality: Quality
    grid_cells_ok: int = 1

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse(value)
        return value


def year_of(value: str) -> int:
    return _parse(value).year


def doy_of(value: str) -> int:
    """Actual day-of-year (1..366) — stored on ``daily_weather.doy``."""
    return _parse(value).timetuple().tm_yday


def calendar_doy(value: str) -> int:
    """Day-of-year on the 365-day calendar used to key thresholds (docs/02 §5.1).

    Feb 29 folds onto Feb 28's index (59) and every date after February in a
    leap year shifts back by one, so 1996-07-12 keys the same threshold as
    1997-07-12.
    """
    d = _parse(value)
    doy = d.timetuple().tm_yday
    if not _is_leap(d.year):
        return doy
    if d.month == 2 and d.day == 29:
        return 59
    if d.month > 2:
        return doy - 1
    return doy


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_year(year: int) -> int:
    return 366 if _is_leap(year) else 365


def add_days(value: str, delta: int) -> str:
    return (_parse(value) + timedelta(days=delta)).isoformat()


class DailyWeatherDoc(BaseModel):
    """Persisted ``daily_weather`` document (docs/02 §6.2)."""

    region_id: str
    date: str
    year: int = Field(ge=1900, le=2200)
    doy: int = Field(ge=1, le=366)
    tmax_c: float | None = None
    tmin_c: float | None = None
    precip_mm: float | None = None
    quality: Quality
    grid_cells_ok: int = Field(ge=0, le=9)
    source: str
    fetched_at: str

    @field_validator("date")
    @classmethod
    def _check_date(cls, value: str) -> str:
        _parse(value)
        return value
