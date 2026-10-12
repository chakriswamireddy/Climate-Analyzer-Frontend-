"""Deterministic synthetic region for the P2 golden-file test (docs/04 §1, docs/05 P2 DoD).

Builds 1991–2020 of daily Tmax that follows a smooth seasonal curve plus a
small deterministic warming term and integer-quantized noise, then plants
seven warm spells (May/June, near the seasonal peak) whose duration grows
from 3 to 6 days — a realistic, hand-reviewable heatwave record.

The spells are placed so they stay well under the 5%-of-window budget the
P95 threshold is computed from (7 spells × ≤7 days = 49 days inside the peak
±15-day window of 30 years × 31 days ≈ 930 samples ≈ 5%), so they do not
shift the threshold they are measured against.

Every value the pipeline emits for this region is committed to
``tests/fixtures/golden/heatwave_region.json`` and asserted by
``tests/unit/test_golden_heatwave.py`` — regenerate that file deliberately
(via ``python -m tests.fixtures.synthetic_region``) and review the diff.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from app.models.weather import Quality, QualityRow

REGION_ID = "r_0123456789abcdef"
DATA_VERSION = "dv_1"
ALGO_VERSION = "1.0.0"
COMPUTED_AT = "2026-10-12T00:00:00Z"
BASELINE = "1991-2020"
START_YEAR, END_YEAR = 1991, 2020

# year -> (start day-of-year, duration days, anomaly °C above the smooth curve)
SPELLS: dict[int, tuple[int, int, float]] = {
    1992: (150, 3, 4.0),
    1997: (152, 3, 4.0),
    2002: (149, 4, 4.5),
    2007: (153, 4, 4.5),
    2012: (150, 5, 5.0),
    2017: (151, 6, 5.0),
    2020: (148, 6, 5.5),
}


def seasonal_tmax(day: date) -> float:
    """Smooth seasonal curve + deterministic warming + quantized noise."""
    doy = day.timetuple().tm_yday
    seasonal = 30.0 + 8.0 * math.sin(2 * math.pi * (doy - 60) / 365.25)
    warming = 0.05 * (day.year - START_YEAR)
    noise = ((doy * 7 + day.year * 13) % 5) * 0.1 - 0.2
    return seasonal + warming + noise


def build_rows() -> list[QualityRow]:
    """The golden region's daily rows (every day valid, full baseline)."""
    rows: list[QualityRow] = []
    day = date(START_YEAR, 1, 1)
    end = date(END_YEAR, 12, 31)
    while day <= end:
        tmax = seasonal_tmax(day)
        if day.year in SPELLS:
            start_doy, duration, anomaly = SPELLS[day.year]
            doy = day.timetuple().tm_yday
            if start_doy <= doy < start_doy + duration:
                tmax += anomaly
        rows.append(
            QualityRow(
                date=day.isoformat(),
                year=day.year,
                doy=day.timetuple().tm_yday,
                tmax_c=round(tmax, 1),
                tmin_c=round(tmax - 10.0, 1),
                precip_mm=0.0,
                quality=Quality.VALID,
                grid_cells_ok=1,
            )
        )
        day += timedelta(days=1)
    return rows


if __name__ == "__main__":
    import json
    from pathlib import Path

    from app.hazard.annual import aggregate_annual
    from app.hazard.detector import detect_events
    from app.hazard.thresholds import compute_thresholds
    from app.hazard.trends import analyze

    _rows = build_rows()
    _thr = compute_thresholds(_rows, baseline=BASELINE)
    _events = detect_events(
        _rows,
        _thr,
        region_id=REGION_ID,
        algo_version=ALGO_VERSION,
        data_version=DATA_VERSION,
    )
    _annual = aggregate_annual(
        _rows,
        _events,
        region_id=REGION_ID,
        hazard="heatwave",
        algo_version=ALGO_VERSION,
        data_version=DATA_VERSION,
        thresholds=_thr,
        computed_at=COMPUTED_AT,
    )
    _summary = analyze(_annual).to_dict()
    _golden = {
        "region_id": REGION_ID,
        "baseline": BASELINE,
        "algo_version": ALGO_VERSION,
        "data_version": DATA_VERSION,
        "n_events": len(_events),
        "events": _events,
        "annual_stats": _annual,
        "summary": _summary,
    }
    path = Path(__file__).parent / "golden" / "heatwave_region.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_golden, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")
