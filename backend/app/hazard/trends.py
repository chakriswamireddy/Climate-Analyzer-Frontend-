"""Read-time trends + plain-language headline (docs/02 §5.4, §7.4, DEC-07/08/22).

Consumes ``hazard_annual_stats`` rows (small, O(years)) — never daily data.
Honesty gates (NFR-05):

* years below 80% coverage never feed trend math; below 60% of the range
  having ≥ 80% coverage suppresses the headline entirely
  (``suppressed_low_coverage``);
* % change needs a ≥ 6-year range of valid years, else ``range_too_short``;
* a zero first-3-year baseline with recent activity is reported as
  ``new_activity``, never "∞% increase" (EC-13);
* p ≥ 0.05 damps the % sentence; rounding happens at render only — headline
  whole %, stats 1 decimal (EC-28).

OLS uses numpy (slope/intercept/r/stderr) with a pure-Python Student-t
p-value (DEC-22: no scipy); Mann-Kendall is a pure-Python tie-corrected
normal approximation (bonus per DEC-22).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.services.quality import is_trend_eligible

ROLLING_WINDOW = 5
ROLLING_MIN_PERIODS = 3
MIN_RANGE_YEARS = 6
SIGNIFICANCE_LEVEL = 0.05
SUPPRESSION_FRACTION = 0.6

KIND_INCREASE = "pct_increase"
KIND_DECREASE = "pct_decrease"
KIND_NO_CHANGE = "no_change"
KIND_NEW_ACTIVITY = "new_activity"
KIND_TOO_SHORT = "range_too_short"
KIND_SUPPRESSED = "suppressed_low_coverage"

TEXT_TOO_SHORT = "Record too short for a reliable trend statement"


@dataclass(frozen=True)
class OlsResult:
    slope: float | None = None
    intercept: float | None = None
    r: float | None = None
    p_value: float | None = None
    stderr: float | None = None


@dataclass(frozen=True)
class MannKendallResult:
    tau: float | None = None
    p_value: float | None = None
    s: int | None = None
    n: int = 0


@dataclass
class Summary:
    """Everything ``GET /analysis/summary`` needs (docs/02 §7.4)."""

    headline: str = ""
    headline_kind: str = KIND_SUPPRESSED
    pct_change_days: float | None = None
    pct_change_events: float | None = None
    baseline_window: dict | None = None
    recent_window: dict | None = None
    ols: OlsResult = field(default_factory=OlsResult)
    mann_kendall: MannKendallResult | None = None
    totals: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    rolling: list[dict] = field(default_factory=list)
    valid_years: list[int] = field(default_factory=list)
    low_coverage_years: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "headline_kind": self.headline_kind,
            "stats": {
                "pct_change_days": self.pct_change_days,
                "pct_change_events": self.pct_change_events,
                "baseline_window": self.baseline_window,
                "recent_window": self.recent_window,
                "ols": _ols_dict(self.ols),
                "mann_kendall": _mk_dict(self.mann_kendall),
                "totals": self.totals,
            },
            "caveats": list(self.caveats),
        }


def rolling_mean(
    values: Sequence[float],
    *,
    window: int = ROLLING_WINDOW,
    min_periods: int = ROLLING_MIN_PERIODS,
) -> list[float | None]:
    """Trailing rolling mean: value at index *i* = mean of up-to-``window``
    values ending at *i*; fewer than ``min_periods`` → ``None`` (docs/02 §5.4)."""
    out: list[float | None] = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        chunk = list(values[start : index + 1])
        if len(chunk) < min_periods:
            out.append(None)
        else:
            out.append(round(sum(chunk) / len(chunk), 1))
    return out


def ols(xs: Sequence[float], ys: Sequence[float]) -> OlsResult:
    """Ordinary least squares with Pearson r and two-sided t-test p-value."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return OlsResult()
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    x_mean = float(x.mean())
    y_mean = float(y.mean())
    sxx = float(((x - x_mean) ** 2).sum())
    sxy = float(((x - x_mean) * (y - y_mean)).sum())
    syy = float(((y - y_mean) ** 2).sum())
    if sxx == 0.0:
        return OlsResult()
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    if n < 3:
        return OlsResult(slope=slope, intercept=intercept, r=r)
    dof = n - 2
    sse = max(syy - slope * sxy, 0.0)
    stderr = math.sqrt(sse / dof / sxx)
    if stderr == 0.0:
        p_value = 0.0
    else:
        t_stat = abs(slope / stderr)
        p_value = 2.0 * (1.0 - _student_t_cdf(t_stat, dof))
    return OlsResult(slope=slope, intercept=intercept, r=r, p_value=p_value, stderr=stderr)


def mann_kendall(values: Sequence[float]) -> MannKendallResult:
    """Mann-Kendall trend test (tie-corrected normal approx + continuity)."""
    n = len(values)
    if n < 2:
        return MannKendallResult(n=n)
    s_stat = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            diff = values[j] - values[i]
            if diff > 0:
                s_stat += 1
            elif diff < 0:
                s_stat -= 1
    counts = Counter(values)
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in counts.values())
    variance = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    tau = s_stat / (0.5 * n * (n - 1))
    if n < 3 or variance <= 0.0:
        return MannKendallResult(tau=round(tau, 3), s=s_stat, n=n)
    if s_stat > 0:
        z = (s_stat - 1.0) / math.sqrt(variance)
    elif s_stat < 0:
        z = (s_stat + 1.0) / math.sqrt(variance)
    else:
        z = 0.0
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    return MannKendallResult(tau=round(tau, 3), p_value=p_value, s=s_stat, n=n)


def pct_change(first_mean: float, last_mean: float) -> float | None:
    """% change first-3 vs last-3 year means (DEC-07).

    ``first_mean == 0`` with later activity → ``None`` (new-activity rule,
    EC-13); both zero → ``0.0``. Result rounded to 1 decimal (EC-28).
    """
    if first_mean > 0:
        return round((last_mean - first_mean) / first_mean * 100.0, 1)
    if last_mean == 0:
        return 0.0
    return None


def analyze(series: Sequence[Mapping[str, Any]], *, method: str = "ols") -> Summary:
    """Full trend summary over annual rows (docs/02 §5.4 ``analyze``).

    ``series`` rows need ``year``, ``event_count``, ``heatwave_days``,
    ``coverage_pct`` (+ ``max_duration`` for totals); gaps should already be
    zero-filled rows (docs/02 §7.4).
    """
    rows = sorted(series, key=lambda row: int(row["year"]))
    summary = Summary()
    if not rows:
        return summary

    summary.low_coverage_years = [
        int(row["year"]) for row in rows if not is_trend_eligible(float(row["coverage_pct"]))
    ]
    eligible = [row for row in rows if is_trend_eligible(float(row["coverage_pct"]))]
    summary.valid_years = [int(row["year"]) for row in eligible]

    totals = {
        "events": sum(int(row["event_count"]) for row in rows),
        "days": sum(int(row["heatwave_days"]) for row in rows),
        "longest_event_days": max((int(row.get("max_duration", 0)) for row in rows), default=0),
    }
    summary.totals = totals
    _add_coverage_caveat(summary, rows)

    if len(eligible) < len(rows) * SUPPRESSION_FRACTION:
        return summary

    if totals["events"] == 0:
        summary.headline_kind = KIND_NO_CHANGE
        summary.headline = (
            f"No heatwave events detected in this region between "
            f"{rows[0]['year']} and {rows[-1]['year']}."
        )
        return summary

    years = [int(row["year"]) for row in eligible]
    days = [float(row["heatwave_days"]) for row in eligible]
    summary.rolling = [
        {"year": year, "value": value}
        for year, value in zip(years, rolling_mean(days), strict=True)
        if value is not None
    ]
    summary.ols = ols(years, days)
    if method in ("mann-kendall", "both"):
        summary.mann_kendall = mann_kendall(days)

    if len(eligible) < MIN_RANGE_YEARS:
        summary.headline_kind = KIND_TOO_SHORT
        summary.headline = TEXT_TOO_SHORT
        return summary

    first, last = eligible[:3], eligible[-3:]
    first_days = sum(float(row["heatwave_days"]) for row in first) / 3.0
    last_days = sum(float(row["heatwave_days"]) for row in last) / 3.0
    first_events = sum(float(row["event_count"]) for row in first) / 3.0
    last_events = sum(float(row["event_count"]) for row in last) / 3.0
    summary.pct_change_days = pct_change(first_days, last_days)
    summary.pct_change_events = pct_change(first_events, last_events)
    summary.baseline_window = {
        "start_year": int(first[0]["year"]),
        "end_year": int(first[-1]["year"]),
        "mean_days": round(first_days, 1),
    }
    summary.recent_window = {
        "start_year": int(last[0]["year"]),
        "end_year": int(last[-1]["year"]),
        "mean_days": round(last_days, 1),
    }

    kind, text = render_headline(
        pct_days=summary.pct_change_days,
        p_value=summary.ols.p_value,
        start_year=int(rows[0]["year"]),
        recent_days=sum(int(row["heatwave_days"]) for row in last),
        first_year=int(first[0]["year"]),
        caveats=summary.caveats,
    )
    summary.headline_kind = kind
    summary.headline = text
    return summary


def render_headline(
    *,
    pct_days: float | None,
    p_value: float | None,
    start_year: int,
    recent_days: int,
    first_year: int,
    caveats: list[str] | None = None,
) -> tuple[str, str]:
    """Headline sentence + kind from a % change and its OLS p-value (DEC-07)."""
    if pct_days is None:
        return (
            KIND_NEW_ACTIVITY,
            "Heatwaves are a new phenomenon in this record for this region "
            f"({recent_days} days in recent 3 years vs none in {first_year}s)",
        )
    if pct_days == 0:
        return KIND_NO_CHANGE, f"Heatwave days in this region are unchanged since {start_year}."

    verb = "increased" if pct_days > 0 else "decreased"
    whole = round(abs(pct_days))
    significant = p_value is not None and p_value < SIGNIFICANCE_LEVEL
    if p_value is not None and not significant:
        return (
            KIND_INCREASE if pct_days > 0 else KIND_DECREASE,
            f"Heatwaves have {verb} by ~{whole}% in this region since {start_year}, "
            f"though the trend is not statistically significant over this period "
            f"(p={p_value:.2f}).",
        )
    if caveats is not None and p_value is not None:
        caveats.append(f"trend statistically significant (OLS p={p_value:.2f})")
    return (
        KIND_INCREASE if pct_days > 0 else KIND_DECREASE,
        f"Heatwaves have {verb} by {whole}% in this region since {start_year}.",
    )


def _add_coverage_caveat(summary: Summary, eligible: Sequence[Mapping[str, Any]]) -> None:
    flagged = [int(row["year"]) for row in eligible if float(row["coverage_pct"]) < 90.0]
    if flagged:
        years = ", ".join(str(year) for year in flagged)
        summary.caveats.append(f"Low data coverage in year(s) {years}; interpret with care")


def _ols_dict(result: OlsResult) -> dict | None:
    if result.slope is None:
        return None
    return {
        "slope_per_year": round(result.slope, 4),
        "slope_per_decade": round(result.slope * 10.0, 3),
        "intercept": round(result.intercept, 3) if result.intercept is not None else None,
        "r": round(result.r, 3) if result.r is not None else None,
        "p_value": round(result.p_value, 4) if result.p_value is not None else None,
        "stderr": round(result.stderr, 4) if result.stderr is not None else None,
    }


def _mk_dict(result: MannKendallResult | None) -> dict | None:
    if result is None or result.tau is None:
        return None
    return {
        "tau": result.tau,
        "p_value": round(result.p_value, 4) if result.p_value is not None else None,
        "s": result.s,
        "n": result.n,
    }


def _student_t_cdf(t: float, df: int) -> float:
    """Student-t CDF via the regularized incomplete beta function."""
    x = df / (df + t * t)
    return 1.0 - 0.5 * _betainc(x, df / 2.0, 0.5)


def _betainc(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(x, a, b) / a
    return 1.0 - front * _betacf(1.0 - x, b, a) / b


def _betacf(x: float, a: float, b: float) -> float:
    max_iterations, epsilon, tiny = 300, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    result = d
    for m in range(1, max_iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        result *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return result
