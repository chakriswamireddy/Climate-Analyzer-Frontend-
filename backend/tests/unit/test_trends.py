"""TC-U-13/14/15/16/17/19/22/23/24 — trends and headline (docs/02 §5.4, docs/04 §2)."""

from __future__ import annotations

from app.hazard.trends import (
    KIND_DECREASE,
    KIND_INCREASE,
    KIND_NEW_ACTIVITY,
    KIND_NO_CHANGE,
    KIND_SUPPRESSED,
    KIND_TOO_SHORT,
    analyze,
    mann_kendall,
    ols,
    pct_change,
    render_headline,
    rolling_mean,
)


def _annual(
    year: int,
    *,
    days: int,
    events: int = 1,
    coverage: float = 99.5,
    max_duration: int = 4,
) -> dict:
    return {
        "year": year,
        "event_count": events,
        "heatwave_days": days,
        "coverage_pct": coverage,
        "max_duration": max_duration,
    }


def _series(n: int, fn) -> list[dict]:
    return [fn(year) for year in range(1990, 1990 + n)]


def test_rolling_mean_window_and_edges() -> None:
    """TC-U-13: 5y trailing, min_periods=3 → first 2 points null."""
    values = [float(i) for i in range(1, 11)]
    result = rolling_mean(values)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == 2.0
    assert result[3] == 2.5
    assert result[9] == 8.0


def test_percent_change_happy_path() -> None:
    """TC-U-14: first3 mean 5, last3 mean 7 → 40.0%."""
    assert pct_change(5.0, 7.0) == 40.0
    assert pct_change(10.0, 5.0) == -50.0
    assert pct_change(0.0, 0.0) == 0.0
    assert pct_change(0.0, 4.0) is None


def test_zero_baseline_yields_new_activity_not_infinity() -> None:
    """TC-U-15: first3 mean 0, last3 mean 4 → pct None, kind new_activity."""
    series = _series(
        10,
        lambda y: _annual(y, days=0 if y < 1997 else 4, events=0 if y < 1997 else 1),
    )
    summary = analyze(series)
    assert summary.pct_change_days is None
    assert summary.headline_kind == KIND_NEW_ACTIVITY
    assert "new phenomenon" in summary.headline
    assert "∞" not in summary.headline


def test_range_too_short_gate() -> None:
    """TC-U-16: a 5-year range suppresses the % sentence."""
    series = _series(5, lambda y: _annual(y, days=4 + (y - 1990), events=1))
    summary = analyze(series)
    assert summary.headline_kind == KIND_TOO_SHORT
    assert summary.pct_change_days is None
    assert summary.headline == "Record too short for a reliable trend statement"


def test_significance_damping() -> None:
    """TC-U-17: weak trend (p≥0.05) → damped headline; strong → plain."""
    weak = _series(
        31,
        lambda y: _annual(y, days=5 + ((y - 1990) % 3) + (y - 1990) // 25, events=1),
    )
    weak_summary = analyze(weak)
    assert weak_summary.ols.p_value is not None and weak_summary.ols.p_value >= 0.05
    assert "not statistically significant" in weak_summary.headline
    assert "~" in weak_summary.headline

    strong = _series(31, lambda y: _annual(y, days=2 + (y - 1990) // 2, events=1))
    strong_summary = analyze(strong)
    assert strong_summary.ols.p_value is not None and strong_summary.ols.p_value < 0.01
    assert "not statistically significant" not in strong_summary.headline
    assert strong_summary.headline_kind == KIND_INCREASE


def test_ols_and_mann_kendall_against_known_values() -> None:
    """TC-U-19: y = 2x+1 → slope≈2, r≈1, p≈0; MK τ=1, monotonic."""
    xs = [float(x) for x in range(1, 11)]
    ys = [2.0 * x + 1.0 for x in xs]
    result = ols(xs, ys)
    assert abs(result.slope - 2.0) < 1e-9
    assert abs(result.intercept - 1.0) < 1e-9
    assert abs(result.r - 1.0) < 1e-9
    assert result.p_value is not None and result.p_value < 1e-6

    mk = mann_kendall(ys)
    assert mk.tau == 1.0
    assert mk.p_value is not None and mk.p_value < 0.05
    assert mk.s == 45
    assert mk.n == 10


def test_headline_grammar_matches_fr07_template() -> None:
    """TC-U-22: pct=40.0, start 1990 → exact FR-07 sentence."""
    caveats: list[str] = []
    kind, text = render_headline(
        pct_days=40.0,
        p_value=0.01,
        start_year=1990,
        recent_days=12,
        first_year=1990,
        caveats=caveats,
    )
    assert kind == KIND_INCREASE
    assert text == "Heatwaves have increased by 40% in this region since 1990."
    assert caveats == ["trend statistically significant (OLS p=0.01)"]

    kind, text = render_headline(
        pct_days=-25.0,
        p_value=0.02,
        start_year=1990,
        recent_days=3,
        first_year=1990,
    )
    assert kind == KIND_DECREASE
    assert text == "Heatwaves have decreased by 25% in this region since 1990."


def test_no_change_sentence() -> None:
    kind, text = render_headline(
        pct_days=0.0,
        p_value=0.5,
        start_year=1990,
        recent_days=4,
        first_year=1990,
    )
    assert kind == KIND_NO_CHANGE
    assert text == "Heatwave days in this region are unchanged since 1990."


def test_suppressed_low_coverage_emits_no_trend_stats() -> None:
    """TC-U-23: all years coverage <80 → suppressed, no trend stats."""
    series = _series(31, lambda y: _annual(y, days=4, events=1, coverage=45.0))
    summary = analyze(series)
    assert summary.headline_kind == KIND_SUPPRESSED
    assert summary.headline == ""
    assert summary.pct_change_days is None
    assert summary.ols.slope is None
    assert summary.ols.p_value is None
    assert summary.mann_kendall is None
    assert summary.rolling == []
    assert summary.low_coverage_years == list(range(1990, 2021))
    assert summary.valid_years == []


def test_low_coverage_years_excluded_from_trend_but_reported() -> None:
    series = _series(
        10,
        lambda y: _annual(
            y, days=3 + (y - 1990) // 3, events=1, coverage=50.0 if y == 1995 else 99.0
        ),
    )
    summary = analyze(series)
    assert summary.valid_years == [y for y in range(1990, 2000) if y != 1995]
    assert summary.low_coverage_years == [1995]
    assert summary.headline_kind == KIND_INCREASE
    assert any("1995" in caveat for caveat in summary.caveats)


def test_rounding_headline_whole_stats_one_decimal() -> None:
    """TC-U-24/EC-28: 39.6 → headline 40%; stats keep 1 decimal."""
    assert pct_change(50.0, 69.8) == 39.6
    kind, text = render_headline(
        pct_days=39.6,
        p_value=0.01,
        start_year=1990,
        recent_days=20,
        first_year=1990,
    )
    assert kind == KIND_INCREASE
    assert "by 40%" in text
    assert "39.6" not in text


def test_zero_events_across_full_coverage_is_no_change() -> None:
    series = _series(31, lambda y: _annual(y, days=0, events=0, max_duration=0))
    summary = analyze(series)
    assert summary.headline_kind == KIND_NO_CHANGE
    assert summary.headline == "No heatwave events detected in this region between 1990 and 2020."
    assert summary.totals == {"events": 0, "days": 0, "longest_event_days": 0}


def test_mann_kendall_only_when_method_requests_it() -> None:
    series = _series(10, lambda y: _annual(y, days=3 + (y - 1990) // 3, events=1))
    assert analyze(series).mann_kendall is None
    assert analyze(series, method="both").mann_kendall is not None
    assert analyze(series, method="mann-kendall").mann_kendall is not None


def test_coverage_caveat_for_included_years_below_90() -> None:
    series = _series(
        10,
        lambda y: _annual(
            y, days=3 + (y - 1990) // 3, events=1, coverage=85.0 if y == 1996 else 99.0
        ),
    )
    summary = analyze(series)
    assert any("Low data coverage" in caveat and "1996" in caveat for caveat in summary.caveats)
