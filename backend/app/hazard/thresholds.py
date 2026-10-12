"""Regional 95th-percentile thresholds (docs/02 §5.1, DEC-03, NFR-02).

For region *R* and calendar day-of-year *d* (1–365):

    thr(R, d) = P95 of valid Tmax over baseline years, d′ ∈ [d−15, d+15]
    (circular), with the documented fallback ladder ±30 → calendar month →
    ``None`` when even those pools hold fewer than ``MIN_SAMPLES`` values.

Pure functions only — no I/O (docs/02 §4). Feb 29 folds onto Feb 28's
calendar index via :func:`~app.models.weather.calendar_doy` (EC-06/TC-U-20),
so leap years never shift a threshold key.

Assessable rows are ``valid`` *and* ``degraded_grid``: a degraded grid day
still carries a real areal mean (docs/02 §5.2 "threshold computed on the
areal-mean series", §5.6, DEC-10), while missing/outlier days never sample
the baseline. ``percentile_nearest_rank`` is
``sorted(samples)[ceil(p/100 * n) - 1]`` — deterministic and dependency-free.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.weather import Quality, QualityRow, calendar_doy

MIN_SAMPLES = 300
NEAR_WINDOW = 15
WIDE_WINDOW = 30
PERCENTILE = 95.0
CALENDAR_DOYS = 365

FALLBACK_WIDE = "wide_window"
FALLBACK_MONTH = "month"

_ASSESSABLE = (Quality.VALID, Quality.DEGRADED_GRID)

_MONTH_STARTS = (1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335)
_MONTH_ENDS = (31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365)


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def percentile_nearest_rank(samples: Sequence[float], percentile: float = PERCENTILE) -> float:
    """Nearest-rank percentile (docs/02 §5.1): ``sorted[n] [ceil(p/100·n) − 1]``."""
    if not samples:
        raise ValueError("percentile_nearest_rank requires at least one sample")
    ordered = sorted(samples)
    rank = math.ceil(percentile / 100.0 * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def circular_doys(center: int, radius: int) -> list[int]:
    """``radius`` calendar doys either side of ``center``, wrapping 365 → 1."""
    return [((center - 1 + offset) % CALENDAR_DOYS) + 1 for offset in range(-radius, radius + 1)]


def month_of_doy(doy: int) -> int:
    """Calendar month (1–12) containing day-of-year ``doy`` on the 365-day calendar."""
    if not 1 <= doy <= CALENDAR_DOYS:
        raise ValueError(f"doy {doy} outside 1..{CALENDAR_DOYS}")
    return bisect_left(_MONTH_ENDS, doy) + 1


def doys_of_month(month: int) -> list[int]:
    """Every calendar day-of-year inside calendar ``month`` (1–12)."""
    if not 1 <= month <= 12:
        raise ValueError(f"month {month} outside 1..12")
    return list(range(_MONTH_STARTS[month - 1], _MONTH_ENDS[month - 1] + 1))


@dataclass(frozen=True)
class Thresholds:
    """A region's per-doy thresholds for one ``algo_version`` (docs/02 §5.1)."""

    by_doy: dict[int, float | None]
    fallback_flags: dict[int, str]
    n_unresolved: int

    def get(self, doy: int) -> float | None:
        return self.by_doy.get(doy)

    @classmethod
    def from_cache(cls, doc: Mapping) -> Thresholds:
        """Rehydrate a ``threshold_cache`` document (docs/02 §6.3)."""
        by_doy = {
            int(key): (None if value is None else float(value))
            for key, value in doc["by_doy"].items()
        }
        flags = {int(key): str(value) for key, value in (doc.get("fallback_flags") or {}).items()}
        unresolved = doc.get("n_unresolved_doys")
        if unresolved is None:
            unresolved = sum(1 for value in by_doy.values() if value is None)
        return cls(by_doy=by_doy, fallback_flags=flags, n_unresolved=int(unresolved))

    def to_doc(
        self,
        *,
        region_id: str,
        baseline: str,
        algo_version: str,
        computed_at: str | None = None,
    ) -> dict:
        """Persistable ``threshold_cache`` document (docs/02 §6.3)."""
        return {
            "region_id": region_id,
            "baseline": baseline,
            "algo_version": algo_version,
            "by_doy": {str(doy): value for doy, value in self.by_doy.items()},
            "fallback_flags": {str(doy): flag for doy, flag in self.fallback_flags.items()},
            "n_unresolved_doys": self.n_unresolved,
            "computed_at": computed_at or utcnow(),
        }


def compute_thresholds(rows: Sequence[QualityRow], *, baseline: str) -> Thresholds:
    """Baseline P95 per calendar doy (docs/02 §5.1 algorithm).

    ``rows`` may cover any window; only days inside ``baseline`` (``YYYY-YYYY``)
    sample the distribution, and gaps simply reduce the sample — never
    interpolated (EC-08).
    """
    start_year, end_year = _parse_baseline(baseline)
    by_doy: dict[int, list[float]] = {doy: [] for doy in range(1, CALENDAR_DOYS + 1)}
    for row in rows:
        if not start_year <= row.year <= end_year:
            continue
        if row.quality not in _ASSESSABLE or row.tmax_c is None:
            continue
        by_doy[calendar_doy(row.date)].append(row.tmax_c)

    thresholds: dict[int, float | None] = {}
    flags: dict[int, str] = {}
    unresolved = 0
    for doy in range(1, CALENDAR_DOYS + 1):
        samples, flag = _resolve_doy(by_doy, doy)
        if samples is None:
            thresholds[doy] = None
            unresolved += 1
            continue
        thresholds[doy] = percentile_nearest_rank(samples)
        if flag is not None:
            flags[doy] = flag
    return Thresholds(by_doy=thresholds, fallback_flags=flags, n_unresolved=unresolved)


def _resolve_doy(
    by_doy: Mapping[int, Sequence[float]], doy: int
) -> tuple[list[float] | None, str | None]:
    """±15 → ±30 → calendar month → ``None`` (docs/02 §5.1 fallback ladder).

    On the 365-day calendar the month pool is a subset of the ±30 window, so
    the month step only resolves when ±30 already did; it is kept verbatim from
    the spec ladder and is what a widened ``MIN_SAMPLES`` policy would consult.
    """
    samples = _gather(by_doy, circular_doys(doy, NEAR_WINDOW))
    if len(samples) >= MIN_SAMPLES:
        return samples, None
    samples = _gather(by_doy, circular_doys(doy, WIDE_WINDOW))
    if len(samples) >= MIN_SAMPLES:
        return samples, FALLBACK_WIDE
    samples = _gather(by_doy, doys_of_month(month_of_doy(doy)))
    if len(samples) >= MIN_SAMPLES:
        return samples, FALLBACK_MONTH
    return None, None


def _gather(by_doy: Mapping[int, Sequence[float]], doys: Sequence[int]) -> list[float]:
    samples: list[float] = []
    for doy in doys:
        samples.extend(by_doy.get(doy, ()))
    return samples


def _parse_baseline(baseline: str) -> tuple[int, int]:
    try:
        start_text, end_text = baseline.split("-", 1)
        start_year, end_year = int(start_text), int(end_text)
    except ValueError as exc:
        raise ValueError(f"baseline must look like '1991-2020', got {baseline!r}") from exc
    if start_year > end_year:
        raise ValueError(f"baseline start {start_year} is after end {end_year}")
    return start_year, end_year
