"""Hazard registry (docs/02 §5.5, FR-03, DEC-19).

``GET /api/v1/hazards`` is a projection of this table, so the FE never
hard-codes a hazard list (FR-03) and a new hazard is one dict entry plus its
pipeline implementation (OOS-01 extension point).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.hazard.detector import MIN_DAYS
from app.hazard.pipeline import HazardPipeline


@dataclass(frozen=True)
class HazardDefinition:
    id: str
    label: str
    default: bool
    day_metric: str
    min_days: int
    threshold: str
    detector: HazardPipeline
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "default": self.default,
            "day_metric": self.day_metric,
            "min_days": self.min_days,
            "threshold": self.threshold,
            "description": self.description,
        }


def _build() -> dict[str, HazardDefinition]:
    # Imported lazily so the registry stays importable without the engine.
    from app.hazard.heatwave import HeatwavePipeline

    heatwave = HazardDefinition(
        id="heatwave",
        label="Heatwave",
        default=True,
        day_metric="tmax",
        min_days=MIN_DAYS,
        threshold="regional_p95_tmax",
        detector=HeatwavePipeline(),
        description=(
            "≥3 consecutive days above the regional 95th-percentile Tmax "
            "(baseline 1991–2020, ±15-day seasonal window)"
        ),
    )
    # EXTENSION POINT: "heavy_rain", "drought" register here (docs/02 §5.5).
    return {heatwave.id: heatwave}


HAZARDS: dict[str, HazardDefinition] = _build()
DEFAULT_HAZARD = "heatwave"


def hazard_ids() -> list[str]:
    return list(HAZARDS)


def get_hazard(hazard_id: str) -> HazardDefinition | None:
    return HAZARDS.get(hazard_id)


def pipeline_for(hazard_id: str) -> HazardPipeline | None:
    definition = HAZARDS.get(hazard_id)
    return definition.detector if definition else None


__all__ = [
    "DEFAULT_HAZARD",
    "HAZARDS",
    "HazardDefinition",
    "get_hazard",
    "hazard_ids",
    "pipeline_for",
]
