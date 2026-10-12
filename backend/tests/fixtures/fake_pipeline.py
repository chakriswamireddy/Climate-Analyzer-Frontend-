"""Test double for the P2 hazard engine.

Proves the ingest transaction persists thresholds/events/annual-stats exactly
as docs/02 §13 specifies, without depending on the (P2) real detector.
"""

from __future__ import annotations

from collections import defaultdict

from app.hazard.pipeline import PipelineContext, PipelineOutput
from app.models.weather import Quality, QualityRow
from app.services.quality import coverage_pct

_ASSESSABLE = (Quality.VALID, Quality.DEGRADED_GRID)
_EVENT_DURATION = 3
_EVENT_EXCEEDANCE = 1.0


class FakeHazardPipeline:
    """Emits one 3-day event per year that has ≥3 assessable days."""

    hazard = "heatwave"

    def build(self, rows: list[QualityRow], ctx: PipelineContext) -> PipelineOutput:
        by_year: dict[int, list[QualityRow]] = defaultdict(list)
        for row in rows:
            by_year[row.year].append(row)

        events: list[dict] = []
        annual_stats: list[dict] = []
        for year in sorted(by_year):
            year_rows = sorted(by_year[year], key=lambda r: r.date)
            assessable = [r for r in year_rows if r.quality in _ASSESSABLE]
            coverage = coverage_pct(year, rows)

            if len(assessable) >= _EVENT_DURATION:
                run = assessable[:_EVENT_DURATION]
                events.append(
                    {
                        "region_id": ctx.region_id,
                        "hazard": ctx.hazard,
                        "start_date": run[0].date,
                        "end_date": run[-1].date,
                        "year": year,
                        "duration_days": _EVENT_DURATION,
                        "mean_exceedance": _EVENT_EXCEEDANCE,
                        "peak_exceedance": _EVENT_EXCEEDANCE + 1.0,
                        "algo_version": ctx.algo_version,
                        "data_version": ctx.data_version,
                    }
                )

            annual_stats.append(
                {
                    "region_id": ctx.region_id,
                    "hazard": ctx.hazard,
                    "year": year,
                    "event_count": 1 if len(assessable) >= _EVENT_DURATION else 0,
                    "heatwave_days": _EVENT_DURATION if len(assessable) >= _EVENT_DURATION else 0,
                    "mean_duration": float(_EVENT_DURATION)
                    if len(assessable) >= _EVENT_DURATION
                    else 0.0,
                    "max_duration": _EVENT_DURATION if len(assessable) >= _EVENT_DURATION else 0,
                    "mean_exceedance": _EVENT_EXCEEDANCE
                    if len(assessable) >= _EVENT_DURATION
                    else 0.0,
                    "coverage_pct": coverage,
                    "algo_version": ctx.algo_version,
                    "data_version": ctx.data_version,
                }
            )

        return PipelineOutput(events=events, annual_stats=annual_stats)
