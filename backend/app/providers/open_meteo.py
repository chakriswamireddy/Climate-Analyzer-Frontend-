"""Open-Meteo Historical Weather client (docs/02 §5.3, DEC-02).

Hardening implemented here (NFR-03):

* connect 5 s / read 30 s timeouts, 60 s total budget per year-chunk;
* retries on network errors, 429 and 5xx only — exponential backoff with
  jitter; a 429 waits out its window when that fits the chunk budget
  (``Retry-After`` header, else the boundary named in the body, else
  ``provider_429_wait_s``; waits capped 60 s) and otherwise fails fast with
  the resume timestamp; never retried on other 4xx;
* weighted-quota pacing: every archive request costs
  ``locations × max(1, (vars/10) × (days/30))`` "calls" on the free tier
  (600/min, 5k/h, 10k/day — calibrated against the live API 2026-10-12), so
  requests are reserved against minute/hour/day budgets before they are sent;
* circuit breaker: opens after 5 consecutive failures, half-open probe after
  30 s;
* global semaphore (4) + minimum inter-request gap for provider fairness;
* year-chunked fetching (≤10 years per request) and one batched request per
  chunk covering all grid points;
* unit assertion on every payload (``daily_units``), schema drift → fatal.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.models.region import RegionSpec
from app.models.weather import DailyReading
from app.providers.base import (
    CircuitOpenError,
    FetchResult,
    MalformedPayloadError,
    ProviderClientError,
    ProviderError,
    ProviderHttpError,
    ProviderTimeout,
    RetryableProviderError,
    SchemaDriftError,
    UnitMismatchError,
)
from app.providers.units import normalize_temperature_unit, to_celsius

DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum"
DAILY_VAR_COUNT = len(DAILY_VARS.split(","))


class CircuitBreaker:
    """Consecutive-failure breaker (docs/02 §5.3) — pure, clock-injectable."""

    def __init__(self, failure_threshold: int, recovery_s: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self.failures = 0
        self.opened_at: float | None = None

    def before_request(self, now: float | None = None) -> None:
        if self.failures < self.failure_threshold:
            return
        now = time.monotonic() if now is None else now
        if self.opened_at is not None and now - self.opened_at < self.recovery_s:
            raise CircuitOpenError("provider circuit open (too many consecutive failures)")
        # half-open: let one probe through

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic() if now is None else now


def year_chunks(start: date, end: date, years: int = 10) -> Iterable[tuple[date, date]]:
    """Split [start, end] into ≤ ``years``-long chunks (retry granularity)."""
    cursor = start
    while cursor <= end:
        chunk_end = date(cursor.year + years - 1, 12, 31)
        if chunk_end > end:
            chunk_end = end
        yield cursor, chunk_end
        cursor = date(chunk_end.year + 1, 1, 1)


def call_weight(
    locations: int,
    days: int,
    variables: int,
    *,
    days_ref: float,
    vars_ref: float,
) -> float:
    """Cost of one archive request in Open-Meteo "API calls".

    Calibrated against the live free tier (2026-10-12): 1 location × 3 vars ×
    3653 days measured at 35.3–37.5 calls, 9 locations at 300–600 — matching
    ``locations × max(1, (variables / 10) × (days / 30))``. Floor of 1 per
    location: a request always costs at least one call.
    """
    return locations * max(1.0, (variables / vars_ref) * (days / days_ref))


class OpenMeteoProvider:
    source = "open-meteo-archive"

    def __init__(
        self, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._sem = asyncio.Semaphore(self.settings.provider_concurrency)
        self._interval_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._pace_lock = asyncio.Lock()
        self._now = time.time
        self._pace_minute = -1
        self._pace_hour = -1
        self._pace_day = -1
        self._pace_minute_used = 0.0
        self._pace_hour_used = 0.0
        self._pace_day_used = 0.0
        self.breaker = CircuitBreaker(
            self.settings.breaker_failure_threshold, self.settings.breaker_recovery_s
        )

    # -- lifecycle ---------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self.settings.provider_connect_timeout_s,
                read=self.settings.provider_timeout_s,
                write=self.settings.provider_timeout_s,
                pool=self.settings.provider_connect_timeout_s,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- public API --------------------------------------------------------

    async def fetch_daily(self, spec: RegionSpec, start: date, end: date) -> FetchResult:
        if start > end:
            raise ValueError("start date must be <= end date")

        readings: list[DailyReading] = []
        rows_in = rejected = attempts = 0

        for chunk_start, chunk_end in year_chunks(start, end, self.settings.chunk_years):
            await self._pace(
                call_weight(
                    locations=len(spec.points),
                    days=(chunk_end - chunk_start).days + 1,
                    variables=DAILY_VAR_COUNT,
                    days_ref=self.settings.provider_weight_days_ref,
                    vars_ref=self.settings.provider_weight_vars_ref,
                )
            )
            budget = self.settings.provider_chunk_budget_s
            try:
                chunk_result = await asyncio.wait_for(
                    self._fetch_chunk(
                        spec, chunk_start, chunk_end, deadline=time.monotonic() + budget
                    ),
                    timeout=budget,
                )
            except TimeoutError as exc:  # asyncio.wait_for raises TimeoutError
                raise ProviderTimeout(
                    f"chunk {chunk_start}..{chunk_end} exceeded {budget:.0f}s budget"
                ) from exc
            readings.extend(chunk_result.readings)
            rows_in += chunk_result.rows_in
            rejected += chunk_result.rows_rejected
            attempts += chunk_result.attempts

        return FetchResult(
            readings=readings,
            rows_in=rows_in,
            rows_rejected=rejected,
            attempts=max(attempts, 1),
            source=self.source,
        )

    # -- internals ---------------------------------------------------------

    async def _fetch_chunk(
        self, spec: RegionSpec, start: date, end: date, *, deadline: float
    ) -> FetchResult:
        params = self._build_params(spec, start, end)
        attempts = 0
        max_attempts = 1 + max(self.settings.provider_max_retries, 0)

        for attempt in range(max_attempts):
            self.breaker.before_request()
            attempts += 1
            retry_after: float | None = None
            try:
                response = await self._send(params)
            except RetryableProviderError as exc:
                self.breaker.record_failure()
                if attempt + 1 >= max_attempts:
                    raise _with_attempts(exc, attempts) from exc
                await self._backoff_or_raise(
                    attempt, retry_after=None, deadline=deadline, attempts=attempts
                )
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    self.breaker.record_failure()
                    if attempt + 1 >= max_attempts:
                        raise _with_attempts(
                            MalformedPayloadError("provider returned a non-JSON body"),
                            attempts,
                        ) from exc
                    await self._backoff_or_raise(
                        attempt, retry_after=None, deadline=deadline, attempts=attempts
                    )
                    continue
                try:
                    readings, rows_in, rejected = self._parse_payload(payload)
                except (SchemaDriftError, UnitMismatchError) as exc:
                    self.breaker.record_success()  # transport was healthy
                    raise _with_attempts(exc, attempts) from exc
                self.breaker.record_success()
                return FetchResult(
                    readings=readings,
                    rows_in=rows_in,
                    rows_rejected=rejected,
                    attempts=attempts,
                    source=self.source,
                )

            if response.status_code == 429:
                retry_after = self._rate_limit_wait(response, deadline=deadline, attempts=attempts)
            elif response.status_code < 500:
                raise _with_attempts(
                    ProviderClientError(f"provider rejected request with {response.status_code}"),
                    attempts,
                )
            self.breaker.record_failure()
            if attempt + 1 >= max_attempts:
                raise _with_attempts(
                    ProviderHttpError(
                        f"provider returned {response.status_code} after {attempts} attempts"
                    ),
                    attempts,
                )
            await self._backoff_or_raise(
                attempt, retry_after=retry_after, deadline=deadline, attempts=attempts
            )

        raise _with_attempts(  # pragma: no cover
            ProviderHttpError("provider retries exhausted"), attempts
        )

    async def _send(self, params: dict[str, Any]) -> httpx.Response:
        client = await self._get_client()
        await self._throttle()
        url = f"{self.settings.openmeteo_base_url.rstrip('/')}/v1/archive"
        async with self._sem:
            try:
                return await client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise ProviderTimeout(f"provider request failed: {exc}") from exc

    def _build_params(self, spec: RegionSpec, start: date, end: date) -> dict[str, Any]:
        return {
            "latitude": ",".join(str(p.lat) for p in spec.points),
            "longitude": ",".join(str(p.lon) for p in spec.points),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": DAILY_VARS,
            "timezone": "auto",
        }

    async def _throttle(self) -> None:
        """Global ≤1 request / interval (docs/02 §11 outbound budget)."""
        interval = self.settings.provider_min_interval_s
        if interval <= 0:
            return
        async with self._interval_lock:
            now = time.monotonic()
            wait = self._last_request_at + interval - now
            if wait > 0:
                await self._sleep(wait)
            self._last_request_at = time.monotonic()

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def _backoff_or_raise(
        self, attempt: int, *, retry_after: float | None, deadline: float, attempts: int
    ) -> None:
        base = self.settings.provider_backoff_base_s * (2**attempt)
        delay = base + random.uniform(0, base * 0.25)  # jitter
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.settings.provider_retry_after_cap_s))
        remaining = deadline - time.monotonic()
        if delay > remaining:
            raise _with_attempts(
                ProviderHttpError(
                    f"retry wait of {delay:.0f}s exceeds the remaining chunk budget "
                    f"({remaining:.0f}s left)"
                ),
                attempts,
            )
        await self._sleep(delay)

    def _rate_limit_wait(
        self, response: httpx.Response, *, deadline: float, attempts: int
    ) -> float:
        """Honour a 429: sleep when the window clears in time, else fail with
        the resume timestamp (docs/02 §5.3 caps any wait at 60 s)."""
        window = _quota_window(response)
        now = self._now()
        resume_at = _window_resume(now, window)
        header_wait = self._retry_after_seconds(response)
        if header_wait is not None:
            wait = header_wait
        elif resume_at is not None:
            wait = resume_at - now
        else:
            wait = self.settings.provider_429_wait_s
        wait = max(wait, 0.0)
        remaining = deadline - time.monotonic()
        if wait > remaining:
            raise _with_attempts(
                self._quota_error(window, resume_at if resume_at is not None else now + wait),
                attempts,
            )
        return min(wait, self.settings.provider_retry_after_cap_s)

    def _quota_error(self, window: str, resume_at: float) -> ProviderError:
        iso = datetime.fromtimestamp(resume_at, tz=UTC).isoformat(timespec="minutes")
        if window == "rate-limited":
            return ProviderHttpError(f"provider rate limited — retry after {iso}")
        return ProviderHttpError(f"provider {window} quota exhausted — retry after {iso}")

    async def _pace(self, weight: float) -> None:
        """Reserve weighted quota for one request before it is sent.

        Open-Meteo's free tier checks *current* window usage (not projected)
        before counting the new request, so a request is allowed whenever the
        running total is still under budget; windows roll every minute, hour
        and day (docs/02 §5.3). Only our own usage is visible here — a shared
        egress IP is handled reactively by ``_rate_limit_wait``.
        """
        async with self._pace_lock:
            while True:
                now = self._now()
                self._roll_quota_windows(now)
                if self._pace_day_used >= self.settings.provider_daily_weight_budget:
                    raise _with_attempts(
                        self._quota_error("daily", _window_resume(now, "daily") or now), 0
                    )
                if self._pace_hour_used >= self.settings.provider_hourly_weight_budget:
                    await self._sleep(_window_resume(now, "hourly") - now + 0.05)
                    continue
                if self._pace_minute_used >= self.settings.provider_minute_weight_budget:
                    await self._sleep(_window_resume(now, "minutely") - now + 0.05)
                    continue
                break
            self._pace_minute_used += weight
            self._pace_hour_used += weight
            self._pace_day_used += weight

    def _roll_quota_windows(self, now: float) -> None:
        day, hour, minute = int(now // 86400), int(now // 3600), int(now // 60)
        if day != self._pace_day:
            self._pace_day = day
            self._pace_day_used = 0.0
        if hour != self._pace_hour:
            self._pace_hour = hour
            self._pace_hour_used = 0.0
        if minute != self._pace_minute:
            self._pace_minute = minute
            self._pace_minute_used = 0.0

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return None

    # -- payload parsing ---------------------------------------------------

    def _parse_payload(self, payload: Any) -> tuple[list[DailyReading], int, int]:
        """Parse a (possibly multi-location) archive payload.

        Returns ``(readings, rows_in, rows_rejected)``. Schema/unit problems
        raise fatal errors; bad *rows* are dropped and counted (docs/02 §5.6).
        """
        locations = payload if isinstance(payload, list) else [payload]
        readings: list[DailyReading] = []
        rows_in = rejected = 0

        for cell_index, location in enumerate(locations):
            if not isinstance(location, dict):
                raise SchemaDriftError("provider payload location is not an object")
            daily = location.get("daily")
            if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
                raise SchemaDriftError("provider payload missing daily.time")
            times: list[str] = daily["time"]
            tmaxs = _require_series(daily, "temperature_2m_max", len(times))
            tmins = daily.get("temperature_2m_min")
            precips = daily.get("precipitation_sum")

            units = location.get("daily_units")
            if not isinstance(units, dict):
                raise UnitMismatchError("provider payload missing daily_units")
            tmax_unit = normalize_temperature_unit(units.get("temperature_2m_max"))
            if tmins is not None:
                tmin_unit = (
                    normalize_temperature_unit(units["temperature_2m_min"])
                    if "temperature_2m_min" in units
                    else tmax_unit
                )
            else:
                tmin_unit = tmax_unit

            for i, day in enumerate(times):
                rows_in += 1
                try:
                    readings.append(
                        DailyReading(
                            date=day,
                            cell=cell_index,
                            tmax_c=_temperature(tmaxs[i], tmax_unit),
                            tmin_c=_temperature(tmins[i], tmin_unit)
                            if tmins is not None and i < len(tmins)
                            else None,
                            precip_mm=_number(precips[i])
                            if precips is not None and i < len(precips)
                            else None,
                        )
                    )
                except (ValidationError, ValueError, TypeError):
                    rejected += 1

        return readings, rows_in, rejected


def _with_attempts(error: ProviderError, attempts: int) -> ProviderError:
    """Tag the error so the run audit can record HTTP attempts (TC-I-05)."""
    error.attempts = attempts
    return error


def _quota_window(response: httpx.Response) -> str:
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = ""
    lowered = reason.lower()
    for window in ("minutely", "hourly", "daily"):
        if window in lowered:
            return window
    return "rate-limited"


def _window_resume(now: float, window: str) -> float | None:
    step = {"minutely": 60, "hourly": 3600, "daily": 86400}.get(window)
    if step is None:
        return None
    return (int(now // step) + 1) * step


def _require_series(daily: dict[str, Any], key: str, expected: int) -> list[Any]:
    series = daily.get(key)
    if not isinstance(series, list):
        raise SchemaDriftError(f"provider payload missing daily.{key}")
    if len(series) != expected:
        raise SchemaDriftError(f"daily.{key} length {len(series)} != time length {expected}")
    return series


def _temperature(value: Any, unit: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a number: {value!r}")
    return to_celsius(float(value), unit)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"not a number: {value!r}")
    return float(value)
