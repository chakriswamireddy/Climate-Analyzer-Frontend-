"""L2 in-process response cache + ETag helpers (docs/02 §10).

The L2 cache holds *assembled* analysis payloads keyed by
``sha256(path + canonical query + data_version + algo_version)`` — so an
ingest that bumps ``data_version`` can never be served from a stale entry,
and an explicit purge on ingest completion (§10 rule 2) makes convergence
immediate for the region that changed.

ETags are strong hashes of the response body **minus** the per-request
``trace_id`` and the ``meta.cache`` marker (both vary without the data
changing), so a repeated GET produces byte-stable content → ``304`` (TC-I-10).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

VOLATILE_KEYS = ("trace_id",)


def cache_key(*parts: Mapping[str, Any]) -> str:
    """Stable sha256 over the given mappings (sorted keys, no whitespace)."""
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def etag_for(payload: Mapping[str, Any]) -> str:
    """Strong ETag of a JSON payload, ignoring fields that vary per request."""
    normalized = {k: v for k, v in payload.items() if k not in VOLATILE_KEYS}
    meta = normalized.get("meta")
    if isinstance(meta, dict) and "cache" in meta:
        meta = {k: v for k, v in meta.items() if k != "cache"}
        normalized["meta"] = meta
    body = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest() + '"'


class ResponseCache:
    """TTL cache with per-entry expiry and region-scoped purging."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 512,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[str, tuple[float, str | None, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, _region, value = entry
        if self._clock() >= expires_at:
            self._entries.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, *, ttl: float, region_id: str | None = None) -> None:
        if len(self._entries) >= self._max_entries:
            self._trim()
        self._entries[key] = (self._clock() + ttl, region_id, value)

    def purge_region(self, region_id: str) -> None:
        stale = [key for key, (_, region, _) in self._entries.items() if region == region_id]
        for key in stale:
            del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def _trim(self) -> None:
        now = self._clock()
        expired = [key for key, (expires_at, _, _) in self._entries.items() if now >= expires_at]
        for key in expired:
            del self._entries[key]
        while len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key][0])
            del self._entries[oldest]


# Process-wide L2 cache (single instance per DEC-13).
response_cache = ResponseCache()


__all__ = ["ResponseCache", "cache_key", "etag_for", "response_cache"]
