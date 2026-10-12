"""P2 DoD — golden-file test for the heatwave engine (docs/05 P2, docs/04 §1).

The synthetic region in ``tests.fixtures.synthetic_region`` is run through the
real pipeline (thresholds → detector → annual) and the read-time analyzer; the
resulting events / annual stats / summary JSON must match the reviewed fixture
``tests/fixtures/golden/heatwave_region.json`` byte-for-byte (modulo key order).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.hazard.annual import aggregate_annual
from app.hazard.detector import detect_events
from app.hazard.heatwave import HeatwavePipeline
from app.hazard.pipeline import PipelineContext
from app.hazard.thresholds import compute_thresholds
from app.hazard.trends import analyze
from app.models.ingest import Window
from tests.fixtures.synthetic_region import (
    ALGO_VERSION,
    BASELINE,
    COMPUTED_AT,
    DATA_VERSION,
    REGION_ID,
    build_rows,
)

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "heatwave_region.json"


def _pipeline_output() -> dict:
    rows = build_rows()
    pipeline = HeatwavePipeline()
    output = pipeline.build(
        rows,
        PipelineContext(
            region_id=REGION_ID,
            hazard="heatwave",
            algo_version=ALGO_VERSION,
            data_version=DATA_VERSION,
            baseline=BASELINE,
            window=Window(start=rows[0].date, end=rows[-1].date),
            baseline_rows=rows,
            computed_at=COMPUTED_AT,
        ),
    )
    summary = analyze(output.annual_stats).to_dict()
    return {
        "region_id": REGION_ID,
        "baseline": BASELINE,
        "algo_version": ALGO_VERSION,
        "data_version": DATA_VERSION,
        "n_events": len(output.events),
        "events": output.events,
        "annual_stats": output.annual_stats,
        "summary": summary,
    }


def test_golden_heatwave_region_matches_reviewed_fixture() -> None:
    golden = json.loads(GOLDEN_PATH.read_text())
    actual = _pipeline_output()
    assert actual == golden


def test_golden_fixture_satisfies_review_invariants() -> None:
    """Independent re-derivation of the same record, without the cache doc."""
    rows = build_rows()
    thr = compute_thresholds(rows, baseline=BASELINE)
    events = detect_events(
        rows,
        thr,
        region_id=REGION_ID,
        algo_version=ALGO_VERSION,
        data_version=DATA_VERSION,
    )
    annual = aggregate_annual(
        rows,
        events,
        region_id=REGION_ID,
        hazard="heatwave",
        algo_version=ALGO_VERSION,
        data_version=DATA_VERSION,
        thresholds=thr,
        computed_at=COMPUTED_AT,
    )
    golden = json.loads(GOLDEN_PATH.read_text())
    assert [e["duration_days"] for e in events] == [e["duration_days"] for e in golden["events"]]
    assert len(annual) == 30
    assert all(a["coverage_pct"] >= 80.0 for a in annual)
    assert thr.n_unresolved == 0
    assert thr.get(1) is not None and thr.get(151) > thr.get(1)
    summary = analyze(annual).to_dict()
    assert summary["headline_kind"] == "pct_increase"
    assert summary["stats"]["pct_change_days"] > 0
    assert summary["stats"]["ols"]["p_value"] is not None
    assert summary["stats"]["ols"]["p_value"] >= 0.05
    assert "not statistically significant" in summary["headline"]
    assert summary["stats"]["totals"]["events"] == golden["n_events"]
