"""``GET /hazards`` — the hazard registry projection (docs/02 §5.5, FR-03).

The FE renders its hazard selector from this response, so it is served from
the static registry (no DB) and cached for an hour (§7.1, TC-I-03).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.hazard.registry import DEFAULT_HAZARD, HAZARDS
from app.routers import ok

router = APIRouter(tags=["hazards"])


@router.get("/hazards")
async def hazards() -> object:
    items = [definition.to_dict() for definition in HAZARDS.values()]
    return ok(
        {"items": items, "default": DEFAULT_HAZARD, "total": len(items)},
        "public, max-age=3600",
    )


__all__ = ["router"]
