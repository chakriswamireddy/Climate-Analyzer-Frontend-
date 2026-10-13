"""Curated preset regions (docs/02 §7.3, DEC-05).

``backend/app/data/presets.json`` ships 25 Indian cities (points) and 12
states (bboxes → 3×3 grid, DEC-10). The file is data, not code: adding a
region means appending a JSON entry — no code change (§7.3).

Judgment call (documented): raw ``POST /regions`` bboxes are capped by V-06
(≤ 25 deg²), but a real state cannot fit in 25 deg². Presets are therefore
referenced by ``preset_id`` and validated against *this* file rather than the
user-input cap — the repo, not the caller, is the trust boundary for their
geometry (S-02 still applies to raw input).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models.region import RegionSpec, bbox_spec, point_spec

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "presets.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, Any]]:
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    presets: dict[str, dict[str, Any]] = {}
    for entry in raw["presets"]:
        if entry["kind"] == "city":
            spec: RegionSpec = point_spec(entry["point"]["lat"], entry["point"]["lon"])
        else:
            spec = bbox_spec(entry["bbox"])
        presets[entry["id"]] = {
            "id": entry["id"],
            "kind": entry["kind"],
            "name": entry["name"],
            "spec": spec.model_dump(exclude_none=True),
            "provider_meta": dict(entry.get("provider_meta", {})),
        }
    return presets


def all_presets() -> list[dict[str, Any]]:
    return list(_load().values())


def get_preset(preset_id: str) -> dict[str, Any] | None:
    return _load().get(preset_id)


def list_presets(kind: str | None = None) -> list[dict[str, Any]]:
    presets = all_presets()
    if kind:
        presets = [item for item in presets if item["kind"] == kind]
    return presets


def clear_cache() -> None:
    """Tests reload the file after edits."""
    _load.cache_clear()


__all__ = ["all_presets", "clear_cache", "get_preset", "list_presets"]
