"""TC-U-01/02/03/20 — threshold computation (docs/02 §5.1, docs/04 §2)."""

from __future__ import annotations

import math
from datetime import date, timedelta

from app.hazard.thresholds import (
    CALENDAR_DOYS,
    FALLBACK_WIDE,
    MIN_SAMPLES,
    circular_doys,
    compute_thresholds,
    month_of_doy,
    percentile_nearest_rank,
)
from app.models.weather import Quality, QualityRow, calendar_doy

REGION = "r_0123456789abcdef"


def _row(day: date, tmax: float | None, quality: Quality = Quality.VALID) -> QualityRow:
    return QualityRow(
        date=day.isoformat(),
        year=day.year,
        doy=day.timetuple().tm_yday,
        tmax_c=tmax,
        tmin_c=None if tmax is None else tmax - 8.0,
        quality=quality,
    )


def _baseline_years(start: int, end: int, tmax_fn) -> list[QualityRow]:
    rows: list[QualityRow] = []
    for year in range(start, end + 1):
        day = date(year, 1, 1)
        while day.year == year:
            rows.append(_row(day, tmax_fn(day)))
            day += timedelta(days=1)
    return rows


def test_percentile_nearest_rank_matches_documented_formula() -> None:
    """TC-U-01: thr[d] == sorted(samples)[ceil(0.95n) − 1], exact."""
    samples = list(range(1, 101))
    assert percentile_nearest_rank(samples, 95) == 95
    assert percentile_nearest_rank(samples, 50) == 50
    assert percentile_nearest_rank([2.0, 1.0, 3.0], 95) == 3.0


def test_thresholds_equal_hand_computed_nearest_rank() -> None:
    """TC-U-01 end-to-end: per-doy P95 over a ±15 circular window."""
    rows = _baseline_years(1991, 2020, lambda day: 30.0 + (day.timetuple().tm_yday % 7))
    thr = compute_thresholds(rows, baseline="1991-2020")

    by_doy: dict[int, list[float]] = {d: [] for d in range(1, CALENDAR_DOYS + 1)}
    for row in rows:
        by_doy[calendar_doy(row.date)].append(row.tmax_c)

    for doy in (1, 42, 151, 280, 365):
        window = circular_doys(doy, 15)
        samples = [v for w in window for v in by_doy[w]]
        assert thr.get(doy) == percentile_nearest_rank(samples, 95)


def test_seasonal_conditioning_winter_thresholds_far_below_summer() -> None:
    """TC-U-02: winter thresholds << summer; a mild winter spell only
    qualifies via its own (low) seasonal threshold, never via summer's."""

    def tmax(day: date) -> float:
        doy = day.timetuple().tm_yday
        return 38.0 + (doy % 5) * 0.4 if 144 <= doy <= 243 else 22.0 + (doy % 3) * 0.5

    rows = _baseline_years(1991, 2020, tmax)
    thr = compute_thresholds(rows, baseline="1991-2020")

    summer = [thr.get(d) for d in range(172, 203)]
    winter = [thr.get(d) for d in range(10, 40)]
    assert min(summer) - max(winter) > 10.0

    # One 3-day winter spell at 25 °C: above the winter P95, below every summer P95.
    spell = [date(2019, 1, 10) + timedelta(days=i) for i in range(3)]
    for day in spell:
        row = next(r for r in rows if r.date == day.isoformat())
        row.tmax_c = 25.0
    winter_events = detect(rows, thr)
    assert [e["start_date"] for e in winter_events] == ["2019-01-10"]

    from app.hazard.thresholds import Thresholds

    summer_only = Thresholds(
        by_doy={d: thr.get(180) for d in range(1, CALENDAR_DOYS + 1)},
        fallback_flags={},
        n_unresolved=0,
    )
    assert detect(rows, summer_only) == []


def detect(rows, thr_values):
    from app.hazard.detector import detect_events
    from app.hazard.thresholds import Thresholds

    if isinstance(thr_values, dict):
        thr_values = Thresholds(by_doy=thr_values, fallback_flags={}, n_unresolved=0)
    return detect_events(
        rows,
        thr_values,
        region_id=REGION,
        algo_version="1.0.0",
        data_version="dv_1",
    )


def test_fallback_ladder_wide_window_then_unresolved() -> None:
    """TC-U-03: 8 baseline years → ±15 pool (248) < 300 → ±30 pool (488) ≥ 300
    flags every doy ``wide_window``; a sparse baseline falls through to
    ``None`` (unassessable, detection skips those days)."""
    rows = _baseline_years(2013, 2020, lambda day: 35.0 + (day.timetuple().tm_yday % 5))
    thr = compute_thresholds(rows, baseline="2013-2020")
    assert thr.n_unresolved == 0
    assert set(thr.fallback_flags.values()) == {FALLBACK_WIDE}
    assert len(thr.fallback_flags) == CALENDAR_DOYS
    assert all(thr.get(d) is not None for d in range(1, CALENDAR_DOYS + 1))

    sparse: list[QualityRow] = []
    for year in range(2013, 2021):
        for doy in range(1, 11):
            sparse.append(_row(date(year, 1, 1) + timedelta(days=doy - 1), 40.0))
    sparse_thr = compute_thresholds(sparse, baseline="2013-2020")
    assert sparse_thr.n_unresolved == CALENDAR_DOYS
    assert all(sparse_thr.get(d) is None for d in range(1, CALENDAR_DOYS + 1))
    assert detect(sparse, sparse_thr) == []


def test_fallback_never_uses_fewer_than_min_samples() -> None:
    """A resolved threshold always rests on ≥ MIN_SAMPLES baseline values."""
    rows = _baseline_years(2015, 2020, lambda day: 33.0 + (day.timetuple().tm_yday % 3))
    thr = compute_thresholds(rows, baseline="2015-2020")
    by_doy: dict[int, list[float]] = {d: [] for d in range(1, CALENDAR_DOYS + 1)}
    for row in rows:
        by_doy[calendar_doy(row.date)].append(row.tmax_c)
    doy = 200
    near = [v for w in circular_doys(doy, 15) for v in by_doy[w]]
    wide = [v for w in circular_doys(doy, 30) for v in by_doy[w]]
    used = near if len(near) >= MIN_SAMPLES else wide
    assert len(used) >= MIN_SAMPLES
    assert thr.get(doy) == percentile_nearest_rank(used, 95)


def test_feb_29_folds_to_feb_28_window() -> None:
    """TC-U-20: leap-year Feb 29 keys the same threshold as Feb 28."""
    rows = _baseline_years(1992, 2020, lambda day: 31.0 + (day.timetuple().tm_yday % 6))
    thr = compute_thresholds(rows, baseline="1992-2020")
    assert calendar_doy("2020-02-29") == calendar_doy("2020-02-28") == 59
    assert thr.get(59) is not None
    # No crash and no out-of-range key for a leap-year series.
    assert all(1 <= d <= CALENDAR_DOYS for d in thr.by_doy)


def test_month_of_doy_round_trip() -> None:
    assert month_of_doy(1) == 1
    assert month_of_doy(31) == 1
    assert month_of_doy(32) == 2
    assert month_of_doy(59) == 2
    assert month_of_doy(60) == 3
    assert month_of_doy(365) == 12


def test_baseline_gap_reduces_sample_but_never_interpolates() -> None:
    """EC-08: missing baseline years shrink the pool; no values are invented."""
    rows = _baseline_years(1991, 2020, lambda day: 30.0 + (day.timetuple().tm_yday % 5))
    for row in rows:
        if 1991 <= row.year <= 2005:
            row.tmax_c = None
            row.quality = Quality.MISSING
    thr = compute_thresholds(rows, baseline="1991-2020")
    # 15 usable years × 31 doys = 465 ≥ 300 → still resolved at ±15.
    assert thr.n_unresolved == 0
    assert all(thr.get(d) is not None for d in range(1, CALENDAR_DOYS + 1))
    assert math.isfinite(thr.get(151))
