"""TC-U-10 — coverage computation and the trend honesty gate (docs/02 §5.6, §5.4)."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.weather import Quality, QualityRow, days_in_year
from app.services.quality import coverage_pct, is_trend_eligible


def _year_rows(year: int, *, missing: int = 0, outlier: int = 0, degraded: int = 0):
    rows: list[QualityRow] = []
    start = date(year, 1, 1)
    for i in range(days_in_year(year)):
        day = start + timedelta(days=i)
        if i < missing:
            quality = Quality.MISSING
        elif i < missing + outlier:
            quality = Quality.OUTLIER
        elif i < missing + outlier + degraded:
            quality = Quality.DEGRADED_GRID
        else:
            quality = Quality.VALID
        rows.append(
            QualityRow(
                date=day.isoformat(),
                year=year,
                doy=day.timetuple().tm_yday,
                quality=quality,
                grid_cells_ok=1,
            )
        )
    return rows


def test_year_with_36_missing_days_is_90_1_percent() -> None:
    rows = _year_rows(2019, missing=36)
    assert coverage_pct(2019, rows) == 90.1
    assert is_trend_eligible(90.1)


def test_complete_year_is_100_percent() -> None:
    assert coverage_pct(2019, _year_rows(2019)) == 100.0


def test_leap_year_denominator_is_366() -> None:
    assert coverage_pct(2020, _year_rows(2020, missing=36)) == round(100 * 330 / 366, 1)


def test_absent_rows_still_count_against_the_year() -> None:
    """A year with no data at all is 0% — never an implicit 100%."""
    assert coverage_pct(1997, []) == 0.0
    assert not is_trend_eligible(0.0)


def test_outliers_reduce_coverage_but_degraded_grid_does_not() -> None:
    outliers = coverage_pct(2019, _year_rows(2019, outlier=40))
    degraded = coverage_pct(2019, _year_rows(2019, degraded=40))
    assert outliers == round(100 * 325 / 365, 1)
    assert degraded == 100.0  # partial grid still yields a real areal mean


def test_trend_gate_boundary_is_exactly_80_percent() -> None:
    assert not is_trend_eligible(79.9)
    assert is_trend_eligible(80.0)
