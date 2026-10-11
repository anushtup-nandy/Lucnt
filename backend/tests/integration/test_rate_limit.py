"""Rate limiting against a live Redis; skipped when Redis is unreachable.

Each test re-enables the limiter (off suite-wide) and uses a fresh uuid identity
so its window starts empty without flushing Redis.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api import metrics
from lucnt_api.config import settings
from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.rate_limit import Bucket, RateLimiter, get_limiter


@pytest.fixture
def live_redis() -> Iterator[redis.Redis]:
    client: redis.Redis = redis.from_url(settings.redis_url, socket_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("redis unreachable; rate-limit behaviour cannot be verified")
    yield client
    client.close()


@pytest.fixture
def limiter(live_redis: redis.Redis) -> Iterator[RateLimiter]:
    """A limiter over live Redis with limiting switched back on."""
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = True
    try:
        yield RateLimiter(live_redis)
    finally:
        settings.rate_limit_enabled = original


def _identity() -> str:
    return f"test-{uuid.uuid4()}"


def _http_status(exc_info: pytest.ExceptionInfo[Exception]) -> int:
    status_code = getattr(exc_info.value, "status_code", None)
    assert isinstance(status_code, int)
    return status_code


# --- The counter actually counts -------------------------------------------


def test_allows_up_to_the_limit_then_rejects(limiter: RateLimiter) -> None:
    identity = _identity()
    limit = settings.rate_limit_default_per_minute
    for _ in range(limit):
        limiter.check(Bucket.DEFAULT, identity)
    with pytest.raises(Exception) as exc_info:  # noqa: B017 — HTTPException
        limiter.check(Bucket.DEFAULT, identity)
    assert _http_status(exc_info) == 429


def test_limit_is_per_identity(limiter: RateLimiter) -> None:
    """One noisy key must not spend another key's budget."""
    noisy, quiet = _identity(), _identity()
    for _ in range(settings.rate_limit_default_per_minute):
        limiter.check(Bucket.DEFAULT, noisy)
    limiter.check(Bucket.DEFAULT, quiet)  # must not raise


def test_buckets_are_independent(limiter: RateLimiter) -> None:
    """The tight `expensive` bucket must not be spent by ordinary reads."""
    identity = _identity()
    for _ in range(settings.rate_limit_expensive_per_minute + 5):
        limiter.check(Bucket.DEFAULT, identity)
    limiter.check(Bucket.EXPENSIVE, identity)  # a separate counter


def test_expensive_bucket_is_tighter_than_default() -> None:
    """The point of the bucket: ledger walks cost far more than a read."""
    assert (
        settings.rate_limit_expensive_per_minute
        < settings.rate_limit_default_per_minute
    )


def test_expensive_bucket_rejects_past_its_limit(limiter: RateLimiter) -> None:
    identity = _identity()
    for _ in range(settings.rate_limit_expensive_per_minute):
        limiter.check(Bucket.EXPENSIVE, identity)
    with pytest.raises(Exception) as exc_info:  # noqa: B017 — HTTPException
        limiter.check(Bucket.EXPENSIVE, identity)
    assert _http_status(exc_info) == 429


def test_429_carries_retry_after(limiter: RateLimiter) -> None:
    """A limited client needs to be told when to come back."""
    identity = _identity()
    for _ in range(settings.rate_limit_expensive_per_minute):
        limiter.check(Bucket.EXPENSIVE, identity)
    with pytest.raises(Exception) as exc_info:  # noqa: B017 — HTTPException
        limiter.check(Bucket.EXPENSIVE, identity)
    headers = getattr(exc_info.value, "headers", None) or {}
    assert headers.get("Retry-After") == "60"


# --- The auth-failure bucket: only failures pay ----------------------------


def test_auth_guard_passes_until_failures_accumulate(limiter: RateLimiter) -> None:
    ip = _identity()
    for _ in range(settings.rate_limit_auth_failure_per_minute):
        limiter.guard_auth_attempt(ip)  # read-only: never consumes budget
        limiter.record_auth_failure(ip)
    with pytest.raises(Exception) as exc_info:  # noqa: B017 — HTTPException
        limiter.guard_auth_attempt(ip)
    assert _http_status(exc_info) == 429


def test_guard_alone_never_locks_anyone_out(limiter: RateLimiter) -> None:
    """The pre-check is read-only, or successful clients would lock themselves out."""
    ip = _identity()
    for _ in range(settings.rate_limit_auth_failure_per_minute * 3):
        limiter.guard_auth_attempt(ip)


def test_auth_failures_are_per_ip(limiter: RateLimiter) -> None:
    """One attacker must not lock out every other client."""
    attacker, bystander = _identity(), _identity()
    for _ in range(settings.rate_limit_auth_failure_per_minute + 1):
        limiter.record_auth_failure(attacker)
    limiter.guard_auth_attempt(bystander)  # must not raise


# --- Fail-open, the deliberate availability choice -------------------------


def test_unreachable_redis_admits_the_request() -> None:
    """A Redis outage fails open (counted in a metric), not closed on the money path."""
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = True
    try:
        dead: redis.Redis = redis.from_url(
            "redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05
        )
        broken = RateLimiter(dead)
        identity = _identity()
        counter = metrics.rate_limiter_fail_open_total.labels(
            bucket=Bucket.EXPENSIVE.value
        )
        before = counter._value.get()
        for _ in range(settings.rate_limit_expensive_per_minute + 10):
            broken.check(Bucket.EXPENSIVE, identity)
        broken.guard_auth_attempt(identity)
        broken.record_auth_failure(identity)
        assert counter._value.get() - before >= (
            settings.rate_limit_expensive_per_minute + 10
        )
    finally:
        settings.rate_limit_enabled = original


def test_disabled_limiter_is_a_no_op(live_redis: redis.Redis) -> None:
    """The kill switch must bypass the limiter without touching Redis."""
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    try:
        limiter = RateLimiter(live_redis)
        identity = _identity()
        for _ in range(settings.rate_limit_expensive_per_minute + 10):
            limiter.check(Bucket.EXPENSIVE, identity)
    finally:
        settings.rate_limit_enabled = original


def test_get_limiter_is_disabled_by_the_kill_switch() -> None:
    """With the flag off, the factory must not build a client at all."""
    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = False
    get_limiter.cache_clear()
    try:
        assert get_limiter()._client is None
    finally:
        settings.rate_limit_enabled = original
        get_limiter.cache_clear()


# --- Keys must never carry secrets ----------------------------------------


def test_brute_force_over_http_eventually_429s(
    live_redis: redis.Redis, db_session: Session
) -> None:
    """`get_auth_context` is wired to the limiter: repeated bad keys end in 429.

    The fixed `testclient` IP bucket is cleared first so reruns start unspent.
    """
    stale = live_redis.keys("lucnt:rl:auth_failure:testclient:*")
    if stale:
        live_redis.delete(*stale)

    original = settings.rate_limit_enabled
    settings.rate_limit_enabled = True
    get_limiter.cache_clear()
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer lucnt_live_not_a_real_key"}
            budget = settings.rate_limit_auth_failure_per_minute
            for _ in range(budget):
                assert client.get("/agents", headers=headers).status_code == 401
            assert client.get("/agents", headers=headers).status_code == 429
            # Per-IP bucket: the deployment must pass real client addresses.
            assert client.get("/agents", headers=headers).status_code == 429
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.rate_limit_enabled = original
        get_limiter.cache_clear()
        leftover = live_redis.keys("lucnt:rl:auth_failure:testclient:*")
        if leftover:
            live_redis.delete(*leftover)


def test_redis_keys_are_namespaced_and_carry_no_secret(
    limiter: RateLimiter, live_redis: redis.Redis
) -> None:
    """Keys are namespaced; the identity is an API-key id, never the bearer token."""
    identity = _identity()
    limiter.check(Bucket.DEFAULT, identity)
    keys = [k.decode() for k in live_redis.keys(f"lucnt:rl:default:{identity}:*")]
    assert len(keys) == 1
    assert keys[0].startswith("lucnt:rl:default:")
    live_redis.delete(*keys)
