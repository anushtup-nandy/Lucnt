"""Redis-backed fixed-window rate limiting.

Fails open: a Redis outage must not take down `authorize()`. nginx MUST set
`real_ip_header`/`set_real_ip_from`, or all clients share one IP bucket.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from functools import lru_cache

import redis
from fastapi import HTTPException, Request, status

from lucnt_api import metrics
from lucnt_api.config import settings

__all__ = [
    "Bucket",
    "RateLimiter",
    "get_limiter",
    "client_ip",
]

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60


class Bucket(enum.StrEnum):
    """A named limit. The value is part of the Redis key, so keep it stable."""

    DEFAULT = "default"
    AUTH_FAILURE = "auth_failure"
    EXPENSIVE = "expensive"


@dataclass(frozen=True)
class _Reading:
    """A counter reading; callers compare differently (see `check` vs guard)."""

    count: int
    limit: int


def client_ip(request: Request) -> str:
    """Best-effort client address, `unknown` when the transport hides it.

    Never reads the forgeable `X-Forwarded-For`; the proxy rewrites the address.
    """
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed-window counter over Redis (boundary bursts may admit up to 2x)."""

    def __init__(self, client: redis.Redis | None) -> None:
        """Store the backing client; `None` disables limiting entirely."""
        self._client = client

    def _limit_for(self, bucket: Bucket) -> int:
        if bucket is Bucket.AUTH_FAILURE:
            return settings.rate_limit_auth_failure_per_minute
        if bucket is Bucket.EXPENSIVE:
            return settings.rate_limit_expensive_per_minute
        return settings.rate_limit_default_per_minute

    def _key(self, bucket: Bucket, identity: str, window: int) -> str:
        return f"lucnt:rl:{bucket.value}:{identity}:{window}"

    def _window(self) -> int:
        """Current window index. Uses Redis' clock so all workers agree."""
        if self._client is None:
            return 0
        seconds, _micros = self._client.time()
        return int(seconds) // _WINDOW_SECONDS

    def _hit(self, bucket: Bucket, identity: str, *, increment: bool) -> _Reading:
        """Read (optionally increment) a counter; zero on Redis error (fail open)."""
        limit = self._limit_for(bucket)
        if self._client is None or not settings.rate_limit_enabled:
            return _Reading(count=0, limit=limit)
        try:
            key = self._key(bucket, identity, self._window())
            if increment:
                pipe = self._client.pipeline()
                pipe.incr(key)
                pipe.expire(key, _WINDOW_SECONDS)
                count = int(pipe.execute()[0])
            else:
                raw = self._client.get(key)
                count = int(raw) if raw is not None else 0
        except redis.RedisError:
            # The metric is what alerting pages on.
            metrics.rate_limiter_fail_open_total.labels(bucket=bucket.value).inc()
            logger.warning(
                "rate limiter unavailable; admitting request",
                extra={"bucket": bucket.value},
                exc_info=True,
            )
            return _Reading(count=0, limit=limit)
        return _Reading(count=count, limit=limit)

    def check(self, bucket: Bucket, identity: str) -> None:
        """Count this request against `bucket`; raise 429 once over the limit.

        Args:
            bucket: Which limit applies.
            identity: The subject of the limit — an API key id, or an IP.

        Raises:
            HTTPException: 429 when the window's limit is exceeded.
        """
        reading = self._hit(bucket, identity, increment=True)
        # `<=`: this request is already counted, so `limit` calls must pass.
        if reading.count > reading.limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"rate limit exceeded ({reading.limit} per minute)",
                headers={"Retry-After": str(_WINDOW_SECONDS)},
            )

    def guard_auth_attempt(self, ip: str) -> None:
        """Reject a caller that has already burned through its failed attempts.

        Read-only and called before the key lookup, so guesses cost no DB trip.

        Raises:
            HTTPException: 429 when too many recent attempts from `ip` failed.
        """
        reading = self._hit(Bucket.AUTH_FAILURE, ip, increment=False)
        # `>=`: nothing was counted here, so N spent failures refuse the next.
        if reading.count >= reading.limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many failed authentication attempts",
                headers={"Retry-After": str(_WINDOW_SECONDS)},
            )

    def record_auth_failure(self, ip: str) -> None:
        """Charge one failed authentication to `ip`."""
        self._hit(Bucket.AUTH_FAILURE, ip, increment=True)


@lru_cache(maxsize=1)
def get_limiter() -> RateLimiter:
    """The process-wide limiter; Redis connects lazily, so startup never fails."""
    if not settings.rate_limit_enabled:
        return RateLimiter(None)
    client: redis.Redis = redis.from_url(
        settings.redis_url,
        socket_timeout=settings.rate_limit_redis_timeout,
        socket_connect_timeout=settings.rate_limit_redis_timeout,
    )
    return RateLimiter(client)
