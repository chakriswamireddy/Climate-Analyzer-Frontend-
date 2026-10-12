"""TC-U-04/05/06/07/08/12/21 — heatwave detection (docs/02 §5.4, docs/04 §2)."""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.hazard.detector import MIN_DAYS, detect_events
from app.hazard.thresholds import CALENDAR_DOYS, Thresholds
from app.models.weather import Quality, QualityRow, calendar_doy

REGION = "r_0123456789abcdef"
FLAT = 30.0


def _flat_thresholds(value: float = FLAT) -> Thresholds:
    return Thresholds(
        by_doy={d: value for d in range(1, CALENDAR_DOYS + 1)},
        fallback_flags={},
        n_unresolved=0,
    )


def _row(day: date, tmax: float | None, quality: Quality = Quality.VALID) -> QualityRow:
    return QualityRow(
        date=day.isoformat(),
        year=day.year,
        doy=day.timetuple().tm_yday,
        tmax_c=tmax,
        tmin_c=None if tmax is None else tmax - 8.0,
        quality=quality,
    )


def _run(days: list[tuple[date, float | None, Quality]], thr: Thresholds | None = None):
    rows = [_row(day, tmax, quality) for day, tmax, quality in days]
    return detect_events(
        rows,
        thr or _flat_thresholds(),
        region_id=REGION,
        algo_version="1.0.0",
        data_version="dv_1",
    )


def _spell(start: date, n: int, tmax: float = 33.0, quality: Quality = Quality.VALID):
    return [(start + timedelta(days=i), tmax, quality) for i in range(n)]


def test_minimal_three_day_event() -> None:
    """TC-U-04: exactly 3 consecutive days just above thr → 1 event, duration 3."""
    events = _run(_spell(date(2019, 5, 1), 3, 30.1))
    assert len(events) == 1
    event = events[0]
    assert event["duration_days"] == 3
    assert event["start_date"] == "2019-05-01"
    assert event["end_date"] == "2019-05-03"
    assert event["year"] == 2019
    assert event["hazard"] == "heatwave"
    assert event["region_id"] == REGION


def test_two_day_near_miss_is_not_an_event() -> None:
    """TC-U-05: 2 above, 1 below, 2 above → 0 events."""
    days = (
        _spell(date(2019, 5, 1), 2, 33.0)
        + _spell(date(2019, 5, 3), 1, 29.0)
        + _spell(date(2019, 5, 4), 2, 33.0)
    )
    assert _run(days) == []


def test_strictly_above_semantics_equal_threshold_does_not_qualify() -> None:
    """TC-U-06: tmax == thr exactly is *not* above → no event."""
    assert _run(_spell(date(2019, 5, 1), 5, FLAT)) == []


def test_gap_breaks_streak_into_two_events() -> None:
    """TC-U-07: 3 above, 1 missing, 3 above → 2 events, not 1 (DEC-20)."""
    days = (
        _spell(date(2019, 5, 1), 3)
        + [(date(2019, 5, 4), None, Quality.MISSING)]
        + _spell(date(2019, 5, 5), 3)
    )
    events = _run(days)
    assert len(events) == 2
    assert [e["start_date"] for e in events] == ["2019-05-01", "2019-05-05"]


def test_outlier_day_breaks_streak() -> None:
    """TC-U-08: an outlier day is unassessable → streak breaks."""
    days = (
        _spell(date(2019, 5, 1), 3)
        + [(date(2019, 5, 4), 999.0, Quality.OUTLIER)]
        + _spell(date(2019, 5, 5), 3)
    )
    events = _run(days)
    assert len(events) == 2


def test_unassessable_threshold_day_breaks_streak() -> None:
    """A day whose threshold is ``None`` can neither start nor extend a run."""
    flat = _flat_thresholds()
    sparse = Thresholds(
        by_doy={**{d: FLAT for d in range(1, CALENDAR_DOYS + 1)}, 124: None},
        fallback_flags={},
        n_unresolved=1,
    )
    assert sparse.get(124) is None
    assert calendar_doy("2019-05-04") == 124
    days = _spell(date(2019, 5, 1), 3) + _spell(date(2019, 5, 4), 1) + _spell(date(2019, 5, 5), 3)
    assert len(_run(days, flat)) == 1
    assert len(_run(days, sparse)) == 2


def test_year_boundary_event_counts_in_start_year() -> None:
    """TC-U-12: streak 12-28 → 01-03 is 1 event; year = start year (DEC-21)."""
    days = _spell(date(1999, 12, 28), 5)  # 12-28..01-01
    events = _run(days)
    assert len(events) == 1
    event = events[0]
    assert event["start_date"] == "1999-12-28"
    assert event["end_date"] == "2000-01-01"
    assert event["year"] == 1999
    assert event["duration_days"] == 5


def test_exceedance_is_strictly_above_threshold_margin() -> None:
    events = _run(_spell(date(2019, 5, 1), 3, 32.5))
    event = events[0]
    assert event["mean_exceedance"] == 2.5
    assert event["peak_exceedance"] == 2.5


@settings(max_examples=30, deadline=None)
@given(
    tmaxs=st.lists(
        st.one_of(st.none(), st.floats(min_value=20.0, max_value=45.0, allow_nan=False)),
        min_size=40,
        max_size=200,
    ),
    threshold=st.floats(min_value=25.0, max_value=40.0, allow_nan=False),
)
def test_detector_properties(tmaxs: list, threshold: float) -> None:
    """TC-U-21: deterministic, non-overlapping, durations ≥ min_days."""
    start = date(2019, 1, 1)
    thr = _flat_thresholds(threshold)
    rows = [
        _row(
            start + timedelta(days=i),
            tmax,
            Quality.MISSING if tmax is None else Quality.VALID,
        )
        for i, tmax in enumerate(tmaxs)
    ]
    first = detect_events(rows, thr, region_id=REGION, algo_version="1.0.0", data_version="dv_1")
    second = detect_events(rows, thr, region_id=REGION, algo_version="1.0.0", data_version="dv_1")
    assert first == second
    for event in first:
        assert event["duration_days"] >= MIN_DAYS
    for earlier, later in zip(first, first[1:], strict=False):
        assert later["start_date"] > earlier["end_date"]
