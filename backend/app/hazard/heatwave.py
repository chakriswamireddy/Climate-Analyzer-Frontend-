"""Heatwave hazard pipeline (docs/02 §5.1, §5.3, §5.4).

Implements the :class:`~app.hazard.pipeline.HazardPipeline` seam the P1
ingest transaction persists: thresholds (own transaction, before the window
recompute), then events + annual stats for the fetched window.

Threshold resolution follows §5.3: the ``threshold_cache`` for this region +
``algo_version`` wins; a cache miss computes from the baseline rows the
ingest service gathered (initial backfill window always covers the baseline;
later windows fall back to previously persisted daily rows). No baseline at
all → honest empty output: no events/stats are written rather than fabricating
thresholds from a partial window.
"""

from __future__ import annotations

import logging

from app.hazard.annual import aggregate_annual
from app.hazard.detector import detect_events
from app.hazard.pipeline import PipelineContext, PipelineOutput
from app.hazard.thresholds import Thresholds, compute_thresholds
from app.models.weather import QualityRow

log = logging.getLogger(__name__)


class HeatwavePipeline:
    """Thresholds + detector + annual aggregation for the ``heatwave`` hazard."""

    hazard = "heatwave"

    def build(self, rows: list[QualityRow], ctx: PipelineContext) -> PipelineOutput:
        thresholds, thresholds_doc = self._resolve_thresholds(ctx)
        if thresholds is None:
            log.warning(
                "no thresholds for %s (no cache, no baseline rows); skipping events/stats",
                ctx.region_id,
            )
            return PipelineOutput()
        events = detect_events(
            rows,
            thresholds,
            region_id=ctx.region_id,
            hazard=ctx.hazard,
            algo_version=ctx.algo_version,
            data_version=ctx.data_version,
        )
        annual = aggregate_annual(
            rows,
            events,
            region_id=ctx.region_id,
            hazard=ctx.hazard,
            algo_version=ctx.algo_version,
            data_version=ctx.data_version,
            thresholds=thresholds,
            computed_at=ctx.computed_at,
        )
        return PipelineOutput(thresholds=thresholds_doc, events=events, annual_stats=annual)

    def _resolve_thresholds(self, ctx: PipelineContext) -> tuple[Thresholds | None, dict | None]:
        if ctx.threshold_cache is not None:
            return Thresholds.from_cache(ctx.threshold_cache), None
        if not ctx.baseline_rows:
            return None, None
        computed = compute_thresholds(ctx.baseline_rows, baseline=ctx.baseline)
        doc = computed.to_doc(
            region_id=ctx.region_id,
            baseline=ctx.baseline,
            algo_version=ctx.algo_version,
            computed_at=ctx.computed_at,
        )
        return computed, doc
