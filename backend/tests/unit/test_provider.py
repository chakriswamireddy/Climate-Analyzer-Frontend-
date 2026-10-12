"""Provider hardening tests (docs/02 §5.3) — mocked HTTP, no Mongo.

Covers the unit/schema/rate-failure classifications that TC-I-04..06 rely on:
payload unit assertion, schema drift, 4xx fatality, 5xx retry budget, year
chunking, and the circuit breaker.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import pytest
import respx

from app.models.region import point_spec
from app.models.weather import DailyReading
from app.providers.base import (
    CircuitOpenError,
    ProviderClientError,
    ProviderHttpError,
    SchemaDriftError,
    UnitMismatchError,
)
from app.providers.open_meteo import (
    CircuitBreaker,
    OpenMeteoProvider,
    call_weight,
    year_chunks,
)
from tests.conftest import ARCHIVE_URL
from tests.fixtures.series_factory import make_readings, open_meteo_payload

SPEC = point_spec(17.385, 78.4867)
WINDOW = (date(2020, 1, 1), date(2020, 1, 5))


def _fahrenheit_readings() -> list[DailyReading]:
    celsius = make_readings(*WINDOW)
    return [
        DailyReading(
            date=r.date,
            tmax_c=round(r.tmax_c * 9 / 5 + 32, 1),
            tmin_c=round(r.tmin_c * 9 / 5 + 32, 1),
            precip_mm=r.precip_mm,
        )
        for r in celsius
    ]


@respx.mock
async def test_fahrenheit_payload_is_converted(settings) -> None:
    """EC-05/TC-U-09: an °F source is converted, never misread as °C."""
    expected = make_readings(*WINDOW)
    respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(200, json=open_meteo_payload(_fahrenheit_readings(), unit="°F"))
    )
    provider = OpenMeteoProvider(settings)
    try:
        result = await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()

    assert result.rows_in == 5
    assert result.rows_rejected == 0
    for got, want in zip(result.readings, expected, strict=True):
        assert got.tmax_c == pytest.approx(want.tmax_c, abs=0.1)


@respx.mock
async def test_unknown_unit_fails_the_fetch(settings) -> None:
    respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(
            200, json=open_meteo_payload(make_readings(*WINDOW), unit="kelvin")
        )
    )
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(UnitMismatchError) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()
    assert excinfo.value.retryable is False


@respx.mock
async def test_missing_tmax_series_is_schema_drift(settings) -> None:
    """TC-I-06 classification: shifted schema is fatal, not retried."""
    route = respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(
            200, json=open_meteo_payload(make_readings(*WINDOW), omit={"temperature_2m_max"})
        )
    )
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(SchemaDriftError):
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()
    assert route.call_count == 1


@respx.mock
async def test_4xx_is_fatal_and_not_retried(settings) -> None:
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(400))
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(ProviderClientError) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()
    assert excinfo.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_5xx_retries_within_budget_then_fails(settings) -> None:
    """3 retries ⇒ 4 attempts max (backoff 1s/2s/4s pattern, docs/02 §5.3)."""
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(503))
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(ProviderHttpError) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()
    assert route.call_count == 4
    assert excinfo.value.retryable is True
    assert excinfo.value.attempts == 4


@respx.mock
async def test_non_json_body_is_retried_then_fails(settings) -> None:
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, text="<html>"))
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(Exception) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()
    assert route.call_count == 4
    assert getattr(excinfo.value, "retryable", False) is True


@respx.mock
async def test_long_windows_are_chunked_by_decade(settings) -> None:
    payload = open_meteo_payload(make_readings(date(1950, 1, 1), date(1950, 1, 3)))
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    provider = OpenMeteoProvider(settings)
    try:
        result = await provider.fetch_daily(SPEC, date(1950, 1, 1), date(2025, 12, 31))
    finally:
        await provider.aclose()
    assert route.call_count == 8  # 1950-2025 in 10-year chunks
    assert result.attempts == 8


def test_year_chunks_cover_the_window_without_gaps() -> None:
    chunks = list(year_chunks(date(1950, 1, 1), date(2025, 12, 31), 10))
    assert len(chunks) == 8
    assert chunks[0][0] == date(1950, 1, 1)
    assert chunks[-1][1] == date(2025, 12, 31)
    for (_, prev_end), (next_start, _) in zip(chunks[:-1], chunks[1:], strict=True):
        assert next_start == date(prev_end.year + 1, 1, 1)


def test_circuit_breaker_opens_recovers_and_half_opens() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_s=30.0)
    breaker.before_request(now=1000.0)
    breaker.record_failure(now=1000.0)
    breaker.record_failure(now=1001.0)

    with pytest.raises(CircuitOpenError):
        breaker.before_request(now=1002.0)
    breaker.before_request(now=1032.0)  # half-open probe allowed

    breaker.record_success()
    breaker.before_request(now=1033.0)  # closed again


def test_call_weight_matches_measured_free_tier(settings) -> None:
    """Weight model calibrated against the live archive API (2026-10-12)."""
    refs = {
        "days_ref": settings.provider_weight_days_ref,
        "vars_ref": settings.provider_weight_vars_ref,
    }
    assert call_weight(1, 3653, 3, **refs) == pytest.approx(36.5, abs=0.1)
    assert call_weight(9, 3653, 3, **refs) == pytest.approx(328.8, abs=0.5)
    assert call_weight(1, 5, 3, **refs) == 1.0
    assert call_weight(4, 5, 3, **refs) == 4.0


@respx.mock
async def test_minute_quota_paces_across_window(settings) -> None:
    """Once the minute budget is used up, the next chunk waits for the window."""
    s = settings.model_copy(update={"provider_minute_weight_budget": 50.0})
    payload = open_meteo_payload(make_readings(date(2020, 1, 1), date(2020, 1, 3)))
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    provider = OpenMeteoProvider(s)
    clock = {"now": time.time()}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds + 0.05

    provider._now = lambda: clock["now"]
    provider._sleep = fake_sleep
    try:
        result = await provider.fetch_daily(SPEC, date(2020, 1, 1), date(2049, 12, 31))
    finally:
        await provider.aclose()

    assert route.call_count == 3
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 60
    assert result.attempts == 3


@respx.mock
async def test_daily_quota_fails_before_sending_more(settings) -> None:
    s = settings.model_copy(update={"provider_daily_weight_budget": 10.0})
    payload = open_meteo_payload(make_readings(date(2020, 1, 1), date(2020, 1, 3)))
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, json=payload))
    provider = OpenMeteoProvider(s)
    try:
        with pytest.raises(ProviderHttpError) as excinfo:
            await provider.fetch_daily(SPEC, date(2020, 1, 1), date(2039, 12, 31))
    finally:
        await provider.aclose()

    assert route.call_count == 1
    assert excinfo.value.retryable is True
    assert excinfo.value.attempts == 0
    assert "daily quota" in str(excinfo.value)


@respx.mock
async def test_429_without_retry_after_waits_configured_window(settings) -> None:
    s = settings.model_copy(update={"provider_429_wait_s": 0.5})
    payload = open_meteo_payload(make_readings(*WINDOW))
    route = respx.get(ARCHIVE_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=payload)]
    )
    provider = OpenMeteoProvider(s)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider._sleep = fake_sleep
    try:
        result = await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()

    assert route.call_count == 2
    assert max(sleeps) >= 0.5
    assert result.attempts == 2


@respx.mock
async def test_429_wait_exceeding_chunk_budget_fails_retryable(settings) -> None:
    s = settings.model_copy(update={"provider_429_wait_s": 1000.0, "provider_chunk_budget_s": 0.2})
    route = respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(429))
    provider = OpenMeteoProvider(s)
    try:
        with pytest.raises(ProviderHttpError) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()

    assert route.call_count == 1
    assert excinfo.value.retryable is True
    assert excinfo.value.attempts == 1
    assert "retry after" in str(excinfo.value)


@respx.mock
async def test_hourly_429_fails_fast_with_resume_hint(settings) -> None:
    route = respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "error": True,
                "reason": "Hourly API request limit exceeded. Please try again in the next hour.",
            },
        )
    )
    provider = OpenMeteoProvider(settings)
    try:
        with pytest.raises(ProviderHttpError) as excinfo:
            await provider.fetch_daily(SPEC, *WINDOW)
    finally:
        await provider.aclose()

    assert route.call_count == 1
    assert excinfo.value.retryable is True
    assert "hourly quota" in str(excinfo.value)
    assert "retry after" in str(excinfo.value)
