"""Redis-backed fixed-window rate limiting (T3-8's sibling, T3-7).

Three buckets, because the abuse they stop is not the same shape:

* ``auth_failure`` — keyed by client IP and checked **before** the API-key hash
  lookup. The cost of a brute-force attempt is a DB round trip, so a limiter
  placed after the lookup would still pay for every guess. Only *failures*
  increment it, so a busy legitimate client is never penalised.
* ``expensive`` — the scan/recompute endpoints, which walk the ledger. Valid
  credentials are enough to pin a worker today (T3-2), so these need a far
  tighter bucket than ordinary reads.
* ``default`` — every other authenticated call, keyed by API key. Generous; it
  exists to stop row/telemetry inflation from a runaway agent loop, not to
  shape normal traffic.

**Shared state, deliberately.** A per-process counter multiplies the effective
limit by the worker count and resets on deploy. Redis keeps it correct for any
topology, which matters because the deployment topology is not settled yet
(T4-1).

**Fail-open, deliberately.** A Redis outage must not take down the money path:
rate limiting is a mitigation, not an authorization boundary, so an unreachable
Redis logs a warning and admits the request. The inverse — failing closed —
would convert a cache blip into a total outage of `authorize()`.

**Deployment requirement.** The IP is read from `request.client.host`. Once nginx
fronts the app, it MUST set `real_ip_header`/`set_real_ip_from`, otherwise every
client collapses into a single IP bucket and the `auth_failure` limit becomes a
self-inflicted lockout for all of them.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from functools import lru_cache

import redis
from fastapi import HTTPException, Request, status

from lucnt_api.config import settings

__all__ = [
    "Bucket",
    "RateLimiter",
    "get_limiter",
    "client_ip",
]

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60


class Bucket(str, enum.Enum):
    """A named limit. The value is part of the Redis key, so keep it stable.

    `str, Enum` rather than `enum.StrEnum` — the project targets Python 3.10.
    """

    DEFAULT = "default"
    AUTH_FAILURE = "auth_failure"
    EXPENSIVE = "expensive"


@dataclass(frozen=True)
class _Reading:
    """A counter reading. The comparison is the caller's, deliberately.

    `check()` has already counted the current request, so it admits while
    `count <= limit`. `guard_auth_attempt()` reads *prior* failures and has not
    counted anything, so a budget of N spent failures must refuse the next
    attempt: `count >= limit`. Folding both into one `allowed` flag here is what
    produced an off-by-one the first time round.
    """

    count: int
    limit: int


def client_ip(request: Request) -> str:
    """Best-effort client address, `unknown` when the transport hides it.

    Deliberately does *not* read `X-Forwarded-For`: an attacker can forge that
    header, which would make the `auth_failure` bucket trivially evadable. The
    proxy is responsible for rewriting the real address (see module docstring).
    """
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Fixed-window counter over Redis.

    A fixed window (rather than a sliding log or token bucket) is two commands
    per check — `INCR` plus a first-hit `EXPIRE` — and needs no Lua or sorted
    sets. Its known weakness is a burst straddling a window boundary admitting
    up to 2x the limit; for brute-force and resource-abuse mitigation that is an
    acceptable trade against the extra round trips a sliding window costs on the
    blocking money path.
    """

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
        """Read (and optionally increment) the counter for one identity.

        On any Redis error returns a zero count, which every caller reads as
        "admit" — the fail-open path described in the module docstring.
        """
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
            # Logged so a silent loss of limiting is visible rather than
            # inferred from an incident.
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

        Read-only on purpose, and called *before* the key lookup: a successful
        request must not consume the failure budget, and a guess must not cost a
        DB round trip once the budget is gone.

        Raises:
            HTTPException: 429 when too many recent attempts from `ip` failed.
        """
        reading = self._hit(Bucket.AUTH_FAILURE, ip, increment=False)
        # `>=`: nothing was counted here, so a fully-spent budget of N failures
        # must refuse the next attempt rather than admit an N+1'th.
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
    """The process-wide limiter, backed by a lazily-connected Redis pool.

    Cached so the connection pool is built once. `redis.from_url` does not
    connect eagerly, so an unreachable Redis surfaces as a `RedisError` on first
    use and is handled by the fail-open path rather than breaking startup.
    """
    if not settings.rate_limit_enabled:
        return RateLimiter(None)
    client: redis.Redis = redis.from_url(
        settings.redis_url,
        socket_timeout=settings.rate_limit_redis_timeout,
        socket_connect_timeout=settings.rate_limit_redis_timeout,
    )
    return RateLimiter(client)
