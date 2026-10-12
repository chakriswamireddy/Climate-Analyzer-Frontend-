"""Heatwave pipeline wiring (docs/02 §5.1, §5.3, §5.4)."""

from __future__ import annotations

from app.hazard.heatwave import HeatwavePipeline
from app.hazard.pipeline import PipelineContext, PipelineOutput
from app.hazard.thresholds import compute_thresholds
from app.models.ingest import Window
from tests.fixtures.synthetic_region import (
    ALGO_VERSION,
    BASELINE,
    COMPUTED_AT,
    DATA_VERSION,
    REGION_ID,
    build_rows,
)


def _ctx(**overrides) -> PipelineContext:
    fields = dict(
        region_id=REGION_ID,
        hazard="heatwave",
        algo_version=ALGO_VERSION,
        data_version=DATA_VERSION,
        baseline=BASELINE,
        window=Window(start="1991-01-01", end="2020-12-31"),
        baseline_rows=build_rows(),
        computed_at=COMPUTED_AT,
    )
    fields.update(overrides)
    return PipelineContext(**fields)


def test_cache_miss_computes_and_emits_threshold_doc() -> None:
    output = HeatwavePipeline().build(build_rows(), _ctx())
    assert isinstance(output, PipelineOutput)
    assert output.thresholds is not None
    assert output.thresholds["region_id"] == REGION_ID
    assert output.thresholds["baseline"] == BASELINE
    assert output.thresholds["algo_version"] == ALGO_VERSION
    assert output.thresholds["computed_at"] == COMPUTED_AT
    assert output.thresholds["n_unresolved_doys"] == 0
    assert len(output.thresholds["by_doy"]) == 365
    assert len(output.events) == 7
    assert len(output.annual_stats) == 30


def test_cache_hit_reuses_thresholds_without_recomputing() -> None:
    rows = build_rows()
    computed = compute_thresholds(rows, baseline=BASELINE)
    cache = computed.to_doc(
        region_id=REGION_ID,
        baseline=BASELINE,
        algo_version=ALGO_VERSION,
        computed_at=COMPUTED_AT,
    )
    output = HeatwavePipeline().build(rows, _ctx(threshold_cache=cache))
    assert output.thresholds is None
    assert len(output.events) == 7


def test_cache_and_fresh_computation_agree() -> None:
    rows = build_rows()
    computed = compute_thresholds(rows, baseline=BASELINE)
    cache = computed.to_doc(
        region_id=REGION_ID,
        baseline=BASELINE,
        algo_version=ALGO_VERSION,
        computed_at=COMPUTED_AT,
    )
    fresh = HeatwavePipeline().build(rows, _ctx())
    cached = HeatwavePipeline().build(rows, _ctx(threshold_cache=cache))
    assert fresh.events == cached.events
    assert fresh.annual_stats == cached.annual_stats


def test_no_baseline_rows_yields_honest_empty_output() -> None:
    output = HeatwavePipeline().build(build_rows(), _ctx(baseline_rows=[]))
    assert output.thresholds is None
    assert output.events == []
    assert output.annual_stats == []


def test_partial_baseline_still_computes_with_fallbacks() -> None:
    rows = [row for row in build_rows() if row.year >= 2013]
    output = HeatwavePipeline().build(rows, _ctx(baseline_rows=rows, baseline="2013-2020"))
    assert output.thresholds is not None
    assert output.thresholds["n_unresolved_doys"] == 0
    assert output.thresholds["fallback_flags"]
    assert len(output.events) >= 1
    assert all(row["coverage_pct"] >= 80.0 for row in output.annual_stats)
