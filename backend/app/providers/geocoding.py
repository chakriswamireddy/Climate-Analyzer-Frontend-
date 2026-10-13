"""Open-Meteo Geocoding proxy client (docs/02 §7.3, DEC-05, S-04).

Server-side proxy for three reasons: the browser never talks to a third
party directly (CORS/quota), the 10/min IP limit and the 24 h response cache
protect the upstream quota, and the SSRF surface stays zero — the client only
ever calls the fixed ``GEOCODING_BASE_URL`` with numeric parameters.

Failures (timeout, 429, 5xx, malformed body) surface as
:class:`~app.providers.base.RetryableProviderError`; the router renders them
as ``503 PROVIDER_ERROR`` with ``retryable: true`` so the FE can fall back to
manual coordinates (docs/02 §7.3).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.cache import ResponseCache, cache_key
from app.core.config import Settings, get_settings
from app.models.region import point_spec
from app.providers.base import (
    MalformedPayloadError,
    ProviderError,
    RetryableProviderError,
)

log = logging.getLogger(__name__)

GEOCODE_TTL_S = 86_400  # docs/02 §10: geocode cached 24 h per normalized query

# Process-wide so every request shares the 24 h cache (single instance, DEC-13).
geocode_cache = ResponseCache(max_entries=1024)


def normalize_query(q: str) -> str:
    return " ".join(q.split()).lower()


class GeocodeClient:
    def __init__(
        self, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def search(self, q: str) -> list[dict]:
        """Bounded search results (≤5) in our region-spec shape, L2-cached."""
        key = cache_key({"geocode": normalize_query(q)})
        cached = geocode_cache.get(key)
        if cached is not None:
            return cached
        results = await self._fetch(q)
        geocode_cache.set(key, results, ttl=GEOCODE_TTL_S)
        return results

    async def _fetch(self, q: str) -> list[dict]:
        params = {
            "name": q,
            "count": str(self.settings.geocode_count),
            "language": "en",
            "format": "json",
        }
        timeout = httpx.Timeout(
            connect=self.settings.provider_connect_timeout_s,
            read=self.settings.geocode_timeout_s,
            write=self.settings.geocode_timeout_s,
            pool=self.settings.provider_connect_timeout_s,
        )
        last_error: ProviderError | None = None
        for attempt in range(2):  # one retry for transient failures (03 §8)
            try:
                client = await self._get_client()
                response = await client.get(
                    f"{self.settings.geocoding_base_url.rstrip('/')}/v1/search",
                    params=params,
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                last_error = RetryableProviderError("geocoding request timed out")
            except httpx.HTTPError as exc:
                last_error = RetryableProviderError(f"geocoding request failed: {exc}")
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = RetryableProviderError(
                        f"geocoding upstream returned {response.status_code}"
                    )
                elif response.status_code >= 400:
                    # Our request was rejected — still a search-path failure,
                    # never surfaced raw (S-15).
                    break
                else:
                    return self._parse(response)
            if attempt == 0:
                await asyncio.sleep(0.2)
        raise last_error or RetryableProviderError("geocoding search failed")

    def _parse(self, response: httpx.Response) -> list[dict]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MalformedPayloadError("geocoding response is not JSON") from exc
        results = payload.get("results")
        if not isinstance(results, list):
            raise MalformedPayloadError("geocoding response missing 'results'")
        items: list[dict] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            lat, lon = entry.get("latitude"), entry.get("longitude")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            name = str(entry.get("name", ""))[:80]
            admin1 = str(entry.get("admin1", ""))[:80]
            country = str(entry.get("country", ""))[:80]
            country_code = str(entry.get("country_code", ""))[:8]
            items.append(
                {
                    "name": name,
                    "admin1": admin1,
                    "country": country,
                    "country_code": country_code,
                    "lat": lat,
                    "lon": lon,
                    "kind": "city",
                    "spec": point_spec(lat, lon).model_dump(exclude_none=True),
                    "provider_meta": {
                        "source": "open-meteo-geocoding",
                        "admin1": admin1,
                        "country_code": country_code,
                    },
                }
            )
        return items

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client


__all__ = ["GeocodeClient", "geocode_cache", "normalize_query"]
