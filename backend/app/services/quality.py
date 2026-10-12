"""Data-quality pipeline (docs/02 §5.6) and coverage computation (DEC-20).

Pure functions only — no I/O (docs/02 §4). Every rejection is counted so the
run audit can show exactly what was dropped and why (NFR-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.weather import (
    CombinedReading,
    Quality,
    QualityRow,
    days_in_year,
    doy_of,
    year_of,
)

# Physical bounds (docs/02 §5.6 / DEC-20)
TMAX_MIN_C, TMAX_MAX_C = -90.0, 70.0
TMIN_MIN_C, TMIN_MAX_C = -95.0, 70.0

# A grid day needs ≥5 of 9 cells (docs/02 §5.2, EC-11)
MIN_GRID_CELLS = 5

# Honesty gate (docs/02 §5.4)
TREND_MIN_COVERAGE_PCT = 80.0


@dataclass
class QualityResult:
    rows: list[QualityRow]
    counts: dict[str, int] = field(default_factory=dict)


def quality_pipeline(
    readings: list[CombinedReading],
    *,
    grid_cells: int = 1,
    rows_rejected: int = 0,
    duplicates: int = 0,
) -> QualityResult:
    """Classify each region-day: ``valid`` / ``missing`` / ``outlier`` /
    ``degraded_grid`` and count every rejection (docs/02 §5.6).

    ``rows_rejected`` is the schema-reject count produced by the provider
    while parsing (the ≥5% fatal threshold is enforced by IngestService, which
    owns run outcome).
    """
    rows: list[QualityRow] = []
    counts = {
        "rows_in": 0,
        "rows_valid": 0,
        "missing": 0,
        "outliers": 0,
        "degraded_grid": 0,
        "duplicates": duplicates,
        "rejected": rows_rejected,
    }

    for reading in readings:
        counts["rows_in"] += 1
        row = _classify(reading, grid_cells=grid_cells)
        rows.append(row)
        if row.quality is Quality.VALID:
            counts["rows_valid"] += 1
        elif row.quality is Quality.MISSING:
            counts["missing"] += 1
        elif row.quality is Quality.OUTLIER:
            counts["outliers"] += 1
        else:
            counts["degraded_grid"] += 1

    return QualityResult(rows=rows, counts=counts)


def _classify(reading: CombinedReading, *, grid_cells: int) -> QualityRow:
    quality = _quality_of(reading, grid_cells=grid_cells)
    return QualityRow(
        date=reading.date,
        year=year_of(reading.date),
        doy=doy_of(reading.date),
        tmax_c=reading.tmax_c,
        tmin_c=reading.tmin_c,
        precip_mm=reading.precip_mm,
        quality=quality,
        grid_cells_ok=reading.cells_ok,
    )


def _quality_of(reading: CombinedReading, *, grid_cells: int) -> Quality:
    # Insufficient cells → the day simply has no usable Tmax (docs/02 §5.2)
    if reading.tmax_c is None:
        return Quality.MISSING
    if grid_cells > 1 and reading.cells_ok < MIN_GRID_CELLS:
        return Quality.MISSING

    if _is_outlier(reading):
        return Quality.OUTLIER

    if grid_cells > 1 and reading.cells_ok < grid_cells:
        return Quality.DEGRADED_GRID
    return Quality.VALID


def _is_outlier(reading: CombinedReading) -> bool:
    """Outlier/sensor-glitch rules (docs/02 §5.6, DEC-20, EC-04)."""
    tmax, tmin, precip = reading.tmax_c, reading.tmin_c, reading.precip_mm
    if tmax is not None and not (TMAX_MIN_C <= tmax <= TMAX_MAX_C):
        return True
    if tmin is not None and not (TMIN_MIN_C <= tmin <= TMIN_MAX_C):
        return True
    if precip is not None and precip < 0:
        return True
    return tmax is not None and tmin is not None and tmin > tmax


def coverage_pct(year: int, rows: list[QualityRow]) -> float:
    """Valid assessable days / total days in ``year`` (docs/02 §5.6, TC-U-10).

    ``degraded_grid`` days are assessable (a partial grid still yields a real
    areal mean), ``missing`` and ``outlier`` days are not. Absent rows count
    against the denominator — the denominator is always the calendar length.
    """
    total = days_in_year(year)
    if total <= 0:
        return 0.0
    valid = sum(
        1
        for row in rows
        if row.year == year and row.quality in (Quality.VALID, Quality.DEGRADED_GRID)
    )
    return round(100.0 * valid / total, 1)


def is_trend_eligible(coverage: float) -> bool:
    """Honesty gate: rows below 80% coverage never feed trend math (§5.4)."""
    return coverage >= TREND_MIN_COVERAGE_PCT
