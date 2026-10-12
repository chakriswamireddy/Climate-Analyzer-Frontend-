"""Grid combination — region spec → one representative series (docs/02 §5.2).

A point region is a 1-cell "grid"; a state/bbox region is the 3×3 grid and its
daily value is the mean of the available cells (≥5 of 9 required).
"""

from __future__ import annotations

from app.models.region import RegionSpec
from app.models.weather import CombinedReading, DailyReading


def grid_cell_count(spec: RegionSpec) -> int:
    """Number of points fetched for this region (9 for grid, 1 for point)."""
    return len(spec.points)


def group_readings(
    readings: list[DailyReading],
) -> tuple[dict[str, dict[int, DailyReading]], int]:
    """Group provider rows by date → cell, counting duplicates (EC-03).

    Duplicate ``(date, cell)`` pairs are last-write-wins — matching the
    unique ``{region_id, date}`` upsert semantics used at persistence time.
    """
    grouped: dict[str, dict[int, DailyReading]] = {}
    duplicates = 0
    for reading in readings:
        by_cell = grouped.setdefault(reading.date, {})
        if reading.cell in by_cell:
            duplicates += 1
        by_cell[reading.cell] = reading
    return grouped, duplicates


def combine_cells(
    grouped: dict[str, dict[int, DailyReading]], *, grid_cells: int
) -> list[CombinedReading]:
    """Reduce cell readings to one region-day per date (docs/02 §5.2).

    * point region: pass-through (``cells_ok`` 0 or 1);
    * grid region: mean over cells with a usable Tmax; fewer than
      ``MIN_GRID_CELLS`` (5) usable cells ⇒ ``tmax_c=None`` so the day is
      marked ``missing`` (the threshold-quality decision itself happens in
      the quality pipeline).
    """
    from app.services.quality import MIN_GRID_CELLS  # local import: no cycle

    combined: list[CombinedReading] = []
    for date in sorted(grouped):
        by_cell = grouped[date]
        cell_readings = list(by_cell.values())

        tmax_values = [r.tmax_c for r in cell_readings if r.tmax_c is not None]
        tmin_values = [r.tmin_c for r in cell_readings if r.tmin_c is not None]
        precip_values = [r.precip_mm for r in cell_readings if r.precip_mm is not None]

        if grid_cells <= 1:
            only = cell_readings[0] if cell_readings else None
            combined.append(
                CombinedReading(
                    date=date,
                    tmax_c=only.tmax_c if only else None,
                    tmin_c=only.tmin_c if only else None,
                    precip_mm=only.precip_mm if only else None,
                    cells_ok=1 if only and only.tmax_c is not None else 0,
                )
            )
            continue

        cells_ok = len(tmax_values)
        usable = cells_ok >= MIN_GRID_CELLS
        combined.append(
            CombinedReading(
                date=date,
                tmax_c=_mean(tmax_values) if usable else None,
                tmin_c=_mean(tmin_values) if tmin_values else None,
                precip_mm=_mean(precip_values) if precip_values else None,
                cells_ok=cells_ok,
            )
        )
    return combined


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2)
