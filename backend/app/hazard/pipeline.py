"""Hazard pipeline seam (docs/02 §5.4, §5.5).

:class:`~app.hazard.heatwave.HeatwavePipeline` is the real engine IngestService
defaults to; :class:`NullHazardPipeline` remains for callers/tests that want an
inert engine, and the Protocol lets hazards register later (§5.5 extension
point, OOS-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.models.ingest import Window
from app.models.weather import QualityRow


@dataclass(frozen=True)
class PipelineContext:
    """Everything a hazard engine needs that is *not* the daily rows.

    ``threshold_cache`` is the ``threshold_cache`` doc for this region +
    ``algo_version`` when it exists (§5.3 cache hit). On a miss IngestService
    gathers ``baseline_rows`` (the 1991–2020 subset — in-window first, then
    previously persisted daily rows) so the pipeline stays pure (docs/02 §4).
    """

    region_id: str
    hazard: str
    algo_version: str
    data_version: str
    baseline: str
    window: Window
    threshold_cache: dict | None = None
    baseline_rows: list[QualityRow] = field(default_factory=list)
    computed_at: str | None = None


@dataclass
class PipelineOutput:
    """Artifacts the ingest transaction persists (docs/02 §13).

    ``thresholds`` is written in its own transaction *before* the window
    recompute, because thresholds are per-region immutable while
    ``algo_version`` is unchanged.
    """

    thresholds: dict | None = None
    events: list[dict] = field(default_factory=list)
    annual_stats: list[dict] = field(default_factory=list)


@runtime_checkable
class HazardPipeline(Protocol):
    hazard: str

    def build(self, rows: list[QualityRow], ctx: PipelineContext) -> PipelineOutput: ...


class NullHazardPipeline:
    """Inert engine: no thresholds, events or annual stats (testing/CLI-only)."""

    hazard = "heatwave"

    def build(self, rows: list[QualityRow], ctx: PipelineContext) -> PipelineOutput:
        return PipelineOutput()
