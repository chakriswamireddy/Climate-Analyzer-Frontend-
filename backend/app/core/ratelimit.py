"""In-process token-bucket rate limiting (docs/02 §11, DEC-13).

One bucket per ``(IP, class)``. Limits are requests-per-minute expressed as a
bucket of capacity ``limit`` refilling at ``limit / 60`` tokens per second, so
a burst of ``limit`` is allowed and the bucket refills continuously — the
behaviour docs/02 §11 calls "token bucket cap N".

The clock is injectable (TC-U-25 runs the bucket against a fake clock) and the
whole table can be dropped with :meth:`RateLimiter.reset` between tests.

Known limitation, documented in the README (DEC-13): state is per process —
it resets on redeploy and does not aggregate across instances.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after_seconds: int | None = None


class _Bucket:
    __slots__ = ("capacity", "refill_per_s", "tokens", "updated_at")

    def __init__(self, capacity: float, refill_per_s: float, now: float) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.tokens = capacity
        self.updated_at = now


class RateLimiter:
    """Token buckets keyed ``(ip, bucket_class)``; classes → per-minute caps."""

    def __init__(
        self,
        limits: dict[str, int],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
    ) -> None:
        self.limits = dict(limits)
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def check(self, ip: str, bucket_class: str) -> RateDecision:
        limit = self.limits.get(bucket_class)
        if limit is None:
            return RateDecision(allowed=True, limit=0, remaining=0, reset_seconds=0)
        now = self._clock()
        key = (ip, bucket_class)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._evict_if_full(now)
            bucket = _Bucket(float(limit), limit / 60.0, now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_s)
            bucket.updated_at = now

        reset = math.ceil((bucket.capacity - bucket.tokens) / bucket.refill_per_s)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateDecision(
                allowed=True,
                limit=limit,
                remaining=int(bucket.tokens),
                reset_seconds=max(reset, 0),
            )
        retry_after = max(1, math.ceil((1.0 - bucket.tokens) / bucket.refill_per_s))
        return RateDecision(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_seconds=max(reset, 0),
            retry_after_seconds=retry_after,
        )

    def reset(self) -> None:
        self._buckets.clear()

    def _evict_if_full(self, now: float) -> None:
        if len(self._buckets) < self._max_buckets:
            return
        # Drop anything that has fully refilled — it carries no state we need.
        expired = [
            key
            for key, bucket in self._buckets.items()
            if bucket.tokens >= bucket.capacity - 1e-9 and now - bucket.updated_at >= 60.0
        ]
        for key in expired:
            del self._buckets[key]
        if len(self._buckets) >= self._max_buckets:
            self._buckets.clear()


def classify(path: str, method: str) -> str | None:
    """Map a request to its rate-limit class (docs/02 §11); ``None`` = unlimited."""
    path = path.rstrip("/") or "/"
    if not path.startswith("/api/v1"):
        return None  # /healthz, /readyz, /docs — never limited (§7.1)
    if path.startswith("/api/v1/admin"):
        return "admin"
    if path == "/api/v1/client-errors":
        return "client_errors"
    if path == "/api/v1/regions/search" or (path == "/api/v1/regions" and method == "POST"):
        return "region"
    return "analysis"


__all__ = ["RateDecision", "RateLimiter", "classify"]
