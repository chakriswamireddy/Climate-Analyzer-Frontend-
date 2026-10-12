"""Heatwave event detection (docs/02 §5.4, DEC-20/21).

A heatwave is a run of ≥ ``min_days`` consecutive days whose Tmax is
*strictly above* the day's regional P95 threshold. A missing, outlier or
unassessable (threshold ``None``) day breaks the run (DEC-20); the event's
``year`` is the year the run *starts* (DEC-21), even when it crosses New
Year. Assessable rows are ``valid`` *and* ``degraded_grid`` — the areal mean
of a partial grid is still a real reading (docs/02 §5.2, §5.6).

Exceedances are stored at 1 decimal (EC-28 display precision); detection
itself never rounds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.hazard.thresholds import Thresholds
from app.models.weather import Quality, QualityRow, calendar_doy, year_of

MIN_DAYS = 3

_ASSESSABLE = (Quality.VALID, Quality.DEGRADED_GRID)


def detect_events(
    rows: Sequence[QualityRow],
    thresholds: Thresholds,
    *,
    region_id: str,
    algo_version: str,
    data_version: str,
    hazard: str = "heatwave",
    min_days: int = MIN_DAYS,
) -> list[dict[str, Any]]:
    """Detect heatwave events over ``rows`` (docs/02 §5.4 ``detect_heatwaves``)."""
    events: list[dict[str, Any]] = []
    run: list[QualityRow] = []
    for row in sorted(rows, key=lambda item: item.date):
        thr = _threshold_for(row, thresholds)
        tmax = row.tmax_c
        qualifies = thr is not None and tmax is not None and tmax > thr
        if qualifies:
            run.append(row)
            continue
        if len(run) >= min_days:
            events.append(
                _make_event(run, thresholds, region_id, hazard, algo_version, data_version)
            )
        run = []
    if len(run) >= min_days:
        events.append(_make_event(run, thresholds, region_id, hazard, algo_version, data_version))
    return events


def _threshold_for(row: QualityRow, thresholds: Thresholds) -> float | None:
    if row.quality not in _ASSESSABLE:
        return None
    return thresholds.get(calendar_doy(row.date))


def _make_event(
    run: Sequence[QualityRow],
    thresholds: Thresholds,
    region_id: str,
    hazard: str,
    algo_version: str,
    data_version: str,
) -> dict[str, Any]:
    exceedances = [float(row.tmax_c) - float(thresholds.get(calendar_doy(row.date))) for row in run]
    return {
        "region_id": region_id,
        "hazard": hazard,
        "start_date": run[0].date,
        "end_date": run[-1].date,
        "year": year_of(run[0].date),
        "duration_days": len(run),
        "mean_exceedance": round(sum(exceedances) / len(exceedances), 1),
        "peak_exceedance": round(max(exceedances), 1),
        "algo_version": algo_version,
        "data_version": data_version,
    }
