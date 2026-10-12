"""Synthetic daily series + Open-Meteo payload builders (docs/04 §1).

Patterns the full suite needs (isolated 3-day streak, 2-day near-miss,
gap-split streak, year-crossing streak, outlier spike, all-missing year) are
available for P2's detector tests; P1 exercises gaps and outliers here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta

from app.models.weather import DailyReading


def date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def seasonal_tmax(day: date) -> float:
    """Warm-season hump peaking around early May (pre-monsoon, peninsular India)."""
    doy = day.timetuple().tm_yday
    return round(30.0 + 7.0 * math.sin(2 * math.pi * (doy - 60) / 365.25), 1)


def make_readings(
    start: date,
    end: date,
    *,
    tmax_fn: Callable[[date], float | None] = seasonal_tmax,
    tmin_offset: float = -9.0,
    precip: float = 0.0,
    overrides: dict[str, dict[str, float | None]] | None = None,
    cell: int = 0,
) -> list[DailyReading]:
    """Build a daily series; ``overrides`` maps ``YYYY-MM-DD`` → field values.

    Setting ``tmax_c`` to ``None`` simulates a provider gap (EC-01); setting
    it to ``999`` simulates a sensor spike (EC-04).
    """
    overrides = overrides or {}
    readings: list[DailyReading] = []
    for day in date_range(start, end):
        iso = day.isoformat()
        values = overrides.get(iso, {})
        tmax = values.get("tmax_c", tmax_fn(day))
        tmin = values.get("tmin_c", None if tmax is None else round(tmax + tmin_offset, 1))
        readings.append(
            DailyReading(
                date=iso,
                cell=cell,
                tmax_c=tmax,
                tmin_c=tmin,
                precip_mm=values.get("precip_mm", precip),
            )
        )
    return readings


def open_meteo_payload(
    readings: Sequence[DailyReading],
    *,
    unit: str = "°C",
    omit: Iterable[str] = (),
    location: dict | None = None,
) -> dict:
    """Build an Open-Meteo shaped archive payload from readings.

    ``omit`` removes keys from ``daily`` to simulate schema drift (TC-I-06).
    """
    omitted = set(omit)
    daily = {
        "time": [r.date for r in readings],
        "temperature_2m_max": [r.tmax_c for r in readings],
        "temperature_2m_min": [r.tmin_c for r in readings],
        "precipitation_sum": [r.precip_mm for r in readings],
    }
    units = {
        "time": "iso8601",
        "temperature_2m_max": unit,
        "temperature_2m_min": unit,
        "precipitation_sum": "mm",
    }
    for key in omitted:
        daily.pop(key, None)
    payload: dict = {
        "latitude": 0.0,
        "longitude": 0.0,
        "daily_units": units,
        "daily": daily,
    }
    if location:
        payload.update(location)
    return payload


def missing_dates(start: date, end: date, count: int) -> list[str]:
    """First ``count`` ISO dates of a window — used to punch coverage gaps."""
    return [d.isoformat() for d in date_range(start, end)[:count]]
