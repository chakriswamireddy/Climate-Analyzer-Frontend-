"""Grid combination rules (docs/02 §5.2, EC-11) — the bbox/CLI path."""

from __future__ import annotations

from datetime import date

from app.models.region import bbox_spec, grid_points, point_spec
from app.models.weather import CombinedReading, DailyReading, Quality
from app.services.grid import combine_cells, group_readings
from app.services.quality import quality_pipeline

BBOX = [17.1, 78.3, 17.7, 78.9]


def _cell_readings(cells: dict[int, float | None], day: str = "2020-06-01") -> list[DailyReading]:
    return [
        DailyReading(date=day, cell=cell, tmax_c=tmax, tmin_c=None) for cell, tmax in cells.items()
    ]


def test_grid_points_are_nine_and_inside_the_box() -> None:
    points = grid_points(BBOX)
    assert len(points) == 9
    assert all(BBOX[0] <= p.lat <= BBOX[2] for p in points)
    assert all(BBOX[1] <= p.lon <= BBOX[3] for p in points)
    assert len(bbox_spec(BBOX).points) == 9
    assert len(point_spec(17.385, 78.4867).points) == 1


def test_full_grid_is_the_mean_and_valid() -> None:
    grouped, duplicates = group_readings(_cell_readings({i: 30.0 + i for i in range(9)}))
    combined = combine_cells(grouped, grid_cells=9)
    assert combined == [
        CombinedReading(date="2020-06-01", tmax_c=34.0, tmin_c=None, precip_mm=None, cells_ok=9)
    ]
    assert quality_pipeline(combined, grid_cells=9).rows[0].quality is Quality.VALID
    assert duplicates == 0


def test_partial_grid_between_5_and_8_cells_is_degraded() -> None:
    cells = {i: (30.0 if i < 6 else None) for i in range(9)}
    combined = combine_cells(group_readings(_cell_readings(cells))[0], grid_cells=9)
    assert combined[0].cells_ok == 6
    assert combined[0].tmax_c == 30.0
    assert quality_pipeline(combined, grid_cells=9).rows[0].quality is Quality.DEGRADED_GRID


def test_grid_below_five_cells_has_no_tmax() -> None:
    cells = {i: (30.0 if i < 4 else None) for i in range(9)}
    combined = combine_cells(group_readings(_cell_readings(cells))[0], grid_cells=9)
    assert combined[0].cells_ok == 4
    assert combined[0].tmax_c is None
    assert quality_pipeline(combined, grid_cells=9).rows[0].quality is Quality.MISSING


def test_point_region_passes_through() -> None:
    grouped, _ = group_readings([DailyReading(date="2020-06-01", tmax_c=41.0)])
    combined = combine_cells(grouped, grid_cells=1)
    assert combined[0].tmax_c == 41.0
    assert combined[0].cells_ok == 1


def test_duplicate_cell_rows_are_last_write_wins() -> None:
    readings = [
        DailyReading(date="2020-06-01", cell=0, tmax_c=30.0),
        DailyReading(date="2020-06-01", cell=0, tmax_c=33.0),
    ]
    grouped, duplicates = group_readings(readings)
    assert duplicates == 1
    assert grouped["2020-06-01"][0].tmax_c == 33.0


def test_dates_come_back_sorted() -> None:
    readings = [
        DailyReading(date="2020-06-03", tmax_c=30.0),
        DailyReading(date="2020-06-01", tmax_c=30.0),
    ]
    combined = combine_cells(group_readings(readings)[0], grid_cells=1)
    assert [r.date for r in combined] == ["2020-06-01", "2020-06-03"]
    assert date(2020, 6, 1).isoformat() == "2020-06-01"
