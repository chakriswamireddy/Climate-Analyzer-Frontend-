"""Provider layer: interface, error taxonomy, fetch result (docs/02 §5.3, §4).

Errors are classified once, here, so every caller (ingest, admin recompute,
on-demand backfill) can apply the same retry policy (03 §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from app.models.region import RegionSpec
from app.models.weather import DailyReading


class ProviderError(Exception):
    """Base class for upstream weather-source failures."""

    code: str = "PROVIDER_ERROR"
    retryable: bool = False
    attempts: int = 1  # HTTP attempts that led here (recorded on the run)

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class RetryableProviderError(ProviderError):
    code = "PROVIDER_TIMEOUT"
    retryable = True


class ProviderTimeout(RetryableProviderError):
    code = "PROVIDER_TIMEOUT"


class ProviderHttpError(RetryableProviderError):
    code = "PROVIDER_HTTP_ERROR"


class CircuitOpenError(RetryableProviderError):
    code = "PROVIDER_CIRCUIT_OPEN"


class MalformedPayloadError(RetryableProviderError):
    """Undecodable body — retried, then surfaced as a retryable run failure (03 §8)."""

    code = "PROVIDER_MALFORMED_RESPONSE"


class FatalProviderError(ProviderError):
    code = "PROVIDER_FATAL"
    retryable = False


class ProviderClientError(FatalProviderError):
    """Non-429 4xx: our request was permanently rejected (never retried)."""

    code = "PROVIDER_HTTP_ERROR"


class UnitMismatchError(FatalProviderError):
    """Unknown temperature unit — never guess (docs/02 §5.6, EC-05)."""

    code = "PROVIDER_UNIT_MISMATCH"


class SchemaDriftError(FatalProviderError):
    """Required fields missing/shifted — the source contract broke (TC-I-06)."""

    code = "PROVIDER_SCHEMA_DRIFT"


@dataclass
class FetchResult:
    """Normalized fetch output for one region-window.

    ``readings`` are per-cell daily rows (``cell`` = grid index); grid
    combination happens in IngestService (docs/02 §5.2), not here.
    """

    readings: list[DailyReading] = field(default_factory=list)
    rows_in: int = 0  # provider rows read (cell-days)
    rows_rejected: int = 0  # rows failing Pydantic schema validation
    attempts: int = 0  # HTTP attempts across all chunks (recorded on the run)
    source: str = "open-meteo-archive"


@runtime_checkable
class WeatherProvider(Protocol):
    """``fetch_daily`` contract from docs/02 §4, with counters added so the
    quality pipeline can enforce the ≥5% bad-row rule (docs/02 §5.6)."""

    source: str

    async def fetch_daily(self, spec: RegionSpec, start: date, end: date) -> FetchResult: ...
