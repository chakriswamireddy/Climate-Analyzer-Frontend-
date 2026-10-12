"""TC-U-08 — outlier handling (docs/04 §2, docs/02 §5.6, EC-04)."""

from __future__ import annotations

from datetime import date, timedelta

from app.models.weather import CombinedReading, Quality
from app.services.quality import coverage_pct, quality_pipeline
from tests.fixtures.series_factory import make_readings


def _combine(readings) -> list[CombinedReading]:
    return [
        CombinedReading(
            date=r.date,
            tmax_c=r.tmax_c,
            tmin_c=r.tmin_c,
            precip_mm=r.precip_mm,
            cells_ok=1,
        )
        for r in readings
    ]


def test_spike_is_flagged_outlier_and_counted() -> None:
    """tmax = 999 °C: flagged, excluded from valid days, counters increment."""
    readings = make_readings(
        date(2020, 6, 1),
        date(2020, 6, 10),
        overrides={"2020-06-05": {"tmax_c": 999.0, "tmin_c": 40.0}},
    )
    result = quality_pipeline(_combine(readings))

    outlier = next(r for r in result.rows if r.date == "2020-06-05")
    assert outlier.quality is Quality.OUTLIER
    assert outlier.tmax_c == 999.0  # raw value kept for audit honesty
    assert result.counts["outliers"] == 1
    assert result.counts["rows_valid"] == 9
    assert result.counts["rows_in"] == 10


def test_outlier_days_are_not_assessable_for_coverage() -> None:
    """A spike is a *lost* day, not a heatwave day: coverage drops (DEC-20).

    The detector-side clause ("streak breaks") is asserted by TC-U-07 once the
    P2 hazard engine lands; here we prove the input the detector consumes.
    """
    start, end = date(2020, 1, 1), date(2020, 12, 31)
    spike_days = {(start + timedelta(days=i)).isoformat() for i in range(36)}
    readings = make_readings(start, end, overrides={d: {"tmax_c": 999.0} for d in spike_days})
    result = quality_pipeline(_combine(readings))

    assert result.counts["outliers"] == 36
    assert coverage_pct(2020, result.rows) == round(100 * (366 - 36) / 366, 1)


def test_physical_bounds_rules() -> None:
    """tmin > tmax, negative precipitation and absurd Tmax are outliers."""
    result = quality_pipeline(
        [
            CombinedReading(date="2020-03-01", tmax_c=30.0, tmin_c=35.0),
            CombinedReading(date="2020-03-02", tmax_c=30.0, tmin_c=20.0, precip_mm=-1.0),
            CombinedReading(date="2020-03-03", tmax_c=-999.0, tmin_c=-20.0),
        ]
    )
    assert [r.quality for r in result.rows] == [
        Quality.OUTLIER,
        Quality.OUTLIER,
        Quality.OUTLIER,
    ]
    assert result.counts["rows_valid"] == 0


def test_missing_tmax_is_missing_not_valid() -> None:
    result = quality_pipeline(
        [
            CombinedReading(date="2020-03-01", tmax_c=None, tmin_c=None),
            CombinedReading(date="2020-03-02", tmax_c=31.5, tmin_c=22.0),
        ]
    )
    assert [r.quality for r in result.rows] == [Quality.MISSING, Quality.VALID]
    assert result.counts["missing"] == 1


def test_duplicate_region_days_are_counted() -> None:
    """EC-03: duplicate rows collapse last-write-wins and are counted."""
    result = quality_pipeline(
        [CombinedReading(date="2020-03-01", tmax_c=30.0)],
        duplicates=2,
    )
    assert result.counts["duplicates"] == 2
    assert result.counts["rows_in"] == 1
