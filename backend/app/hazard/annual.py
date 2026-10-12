"""Annual hazard metrics (docs/02 §5.4, DEC-20/21).

Per region × year × ``algo_version`` (``hazard_annual_stats``, docs/02 §6.5):

* ``event_count`` / ``mean_duration`` / ``max_duration`` — over events whose
  *start* year is the year (DEC-21).
* ``heatwave_days`` / ``mean_exceedance`` — every event day *inside* the year,
  so a Dec→Jan event contributes days to both years while counting once in the
  start year (EC-15/CF-15).
* ``coverage_pct`` — valid assessable days / calendar days of the year
  (DEC-20). Days whose threshold is ``None`` are unassessable (docs/02 §5.1)
  and therefore count against the denominator.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.hazard.thresholds import Thresholds
from app.models.weather import Quality, QualityRow, add_days, calendar_doy, days_in_year, year_of

_ASSESSABLE = (Quality.VALID, Quality.DEGRADED_GRID)


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def aggregate_annual(
    rows: Sequence[QualityRow],
    events: Sequence[dict[str, Any]],
    *,
    region_id: str,
    hazard: str,
    algo_version: str,
    data_version: str,
    thresholds: Thresholds,
    computed_at: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate events + daily rows into per-year stats (docs/02 §5.4)."""
    stamp = computed_at or utcnow()
    rows_by_date = {row.date: row for row in rows}
    exceedance_by_date = _event_day_exceedances(events, thresholds, rows_by_date)
    days_by_year: dict[int, list[str]] = {}
    for date in exceedance_by_date:
        days_by_year.setdefault(year_of(date), []).append(date)

    stats: list[dict[str, Any]] = []
    for year in sorted({row.year for row in rows}):
        year_rows = [row for row in rows if row.year == year]
        start_events = [event for event in events if event["year"] == year]
        durations = [int(event["duration_days"]) for event in start_events]
        event_days = days_by_year.get(year, [])
        stats.append(
            {
                "region_id": region_id,
                "hazard": hazard,
                "year": year,
                "event_count": len(start_events),
                "heatwave_days": len(event_days),
                "mean_duration": round(sum(durations) / len(durations), 1) if durations else 0.0,
                "max_duration": max(durations) if durations else 0,
                "mean_exceedance": (
                    round(sum(exceedance_by_date[d] for d in event_days) / len(event_days), 1)
                    if event_days
                    else 0.0
                ),
                "coverage_pct": _coverage_pct(year, year_rows, thresholds),
                "algo_version": algo_version,
                "data_version": data_version,
                "computed_at": stamp,
            }
        )
    return stats


def coverage_for_year(year: int, rows: Sequence[QualityRow], thresholds: Thresholds) -> float:
    """Valid assessable days / calendar days of ``year`` (DEC-20)."""
    return _coverage_pct(year, [row for row in rows if row.year == year], thresholds)


def _coverage_pct(year: int, year_rows: Sequence[QualityRow], thresholds: Thresholds) -> float:
    total = days_in_year(year)
    if total <= 0:
        return 0.0
    assessable = sum(1 for row in year_rows if _is_assessable(row, thresholds))
    return round(100.0 * assessable / total, 1)


def _is_assessable(row: QualityRow, thresholds: Thresholds) -> bool:
    return (
        row.quality in _ASSESSABLE
        and row.tmax_c is not None
        and thresholds.get(calendar_doy(row.date)) is not None
    )


def _event_day_exceedances(
    events: Sequence[dict[str, Any]],
    thresholds: Thresholds,
    rows_by_date: dict[str, QualityRow],
) -> dict[str, float]:
    """Tmax − threshold for every event day that has a daily row."""
    exceedances: dict[str, float] = {}
    for event in events:
        date = str(event["start_date"])
        end = str(event["end_date"])
        while date <= end:
            row = rows_by_date.get(date)
            thr = thresholds.get(calendar_doy(date))
            if row is not None and row.tmax_c is not None and thr is not None:
                exceedances[date] = row.tmax_c - thr
            date = add_days(date, 1)
    return exceedances
