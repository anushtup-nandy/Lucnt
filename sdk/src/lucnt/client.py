"""Internal HTTP transport (sync and async): typed errors, idempotency, retries.

All retry/classification decisions are shared module-level functions, so the
sync and async paths cannot drift (a drift here is a double-booked spend).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from types import TracebackType

import httpx

from lucnt._version import __version__
from lucnt.exceptions import (
    APIError,
    AuthError,
    ConfigurationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    TransportError,
)
from lucnt.models import LucntConfig

_USER_AGENT = f"lucnt-python/{__version__}"

# Matches the backend's String(255) idempotency_key column.
MAX_IDEMPOTENCY_KEY_CHARS = 255

# Edge-proxy 4xx bodies are often HTML; keep them out of tracebacks.
_MAX_DETAIL_CHARS = 200

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_STATUS_EXCEPTIONS: dict[int, type[APIError]] = {
    400: InvalidRequestError,
    401: AuthError,
    403: AuthError,
    404: NotFoundError,
    409: ConflictError,
    422: InvalidRequestError,
    429: RateLimitError,
}


def check_idempotency_key(value: str, source: str) -> str:
    """Strip and validate an idempotency key; blank or over-long keys would collide.

    Raises:
        ConfigurationError: The key is blank or longer than 255 characters.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ConfigurationError(f"{source} must not be blank.")
    if len(cleaned) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ConfigurationError(
            f"{source} is {len(cleaned)} characters; the maximum is "
            f"{MAX_IDEMPOTENCY_KEY_CHARS}."
        )
    return cleaned


def _sleep(seconds: float) -> None:
    """Block between retry attempts (module-level so tests can patch it)."""
    time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    """Yield to the event loop between retry attempts (async `_sleep`)."""
    await asyncio.sleep(seconds)


def truncate_body(text: str) -> str:
    """Cap a response body to what may safely appear in an error message."""
    return text.strip()[:_MAX_DETAIL_CHARS]


def _parse_detail(response: httpx.Response) -> str | None:
    """Pull FastAPI's `detail` out of an error body, falling back to truncated text."""
    try:
        payload = response.json()
    except ValueError:
        return truncate_body(response.text) or None
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])[:_MAX_DETAIL_CHARS]
    return str(payload)[:_MAX_DETAIL_CHARS] or None


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read `Retry-After` as seconds, ignoring the HTTP-date form."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _raise_for_error(response: httpx.Response, path: str) -> None:
    """Map a 4xx response onto the typed exception hierarchy."""
    status_code = response.status_code
    detail = _parse_detail(response)
    request_id = response.headers.get("X-Request-Id")
    message = f"{path} failed: {status_code}"
    if detail:
        message = f"{message} {detail}"

    exc_type = _STATUS_EXCEPTIONS.get(status_code, APIError)
    if exc_type is RateLimitError:
        raise RateLimitError(
            message,
            status_code=status_code,
            detail=detail,
            request_id=request_id,
            retry_after=_parse_retry_after(response),
        )
    raise exc_type(
        message, status_code=status_code, detail=detail, request_id=request_id
    )


@dataclass(frozen=True)
class _Transient:
    """A retryable failure, carrying what to raise if attempts run out."""

    error: TransportError
    retry_after: float | None = None


def _transient_from_exc(exc: httpx.HTTPError, path: str) -> _Transient:
    """Wrap an httpx-level failure as a retryable `_Transient`."""
    if isinstance(exc, httpx.TimeoutException):
        error = TransportError(f"Request to {path} timed out")
    else:
        error = TransportError(f"Request to {path} failed: {exc}")
    error.__cause__ = exc
    return _Transient(error)


def _classify(
    response: httpx.Response, path: str, *, last_attempt: bool
) -> httpx.Response | _Transient:
    """Classify a response: success, retryable (5xx/429), or raise a typed 4xx.

    On the last attempt a 429 raises `RateLimitError` (with `retry_after`).
    """
    if response.status_code < 400:
        return response
    if response.status_code >= 500:
        return _Transient(
            TransportError(f"Server error for {path}: {response.status_code}")
        )
    if response.status_code == 429:
        if last_attempt:
            _raise_for_error(response, path)
        return _Transient(
            TransportError(f"Rate limited on {path}"), _parse_retry_after(response)
        )
    _raise_for_error(response, path)
    raise AssertionError("unreachable")  # pragma: no cover


def _attempt_count(
    config: LucntConfig,
    method: str,
    idempotency_key: str | None,
    retry_safe: bool,
) -> int:
    """How many times this request may be sent.

    An unkeyed POST is never retried: it may already have landed server-side.
    """
    eligible = (
        retry_safe or method.upper() in _SAFE_METHODS or idempotency_key is not None
    )
    return config.max_retries + 1 if eligible else 1


def _backoff_delay(
    config: LucntConfig, attempt: int, retry_after: float | None
) -> float:
    """Seconds to wait before `attempt` (0-based): full-jitter exponential backoff.

    `Retry-After` wins when honoured but is still capped by `backoff_max`.
    """
    if retry_after is not None and config.respect_retry_after:
        return min(retry_after, config.backoff_max)
    window = min(config.backoff_base * (2**attempt), config.backoff_max)
    return random.uniform(0, window)  # noqa: S311 — jitter, not cryptography


def _idempotency_headers(idempotency_key: str | None) -> dict[str, str] | None:
    """Build the `Idempotency-Key` header, or None when there is no key."""
    if idempotency_key is None:
        return None
    return {"Idempotency-Key": idempotency_key}


def _timeout(config: LucntConfig) -> httpx.Timeout:
    """Build the per-phase timeout; unset phases fall back to the overall one."""
    return httpx.Timeout(
        config.timeout,
        connect=config.connect_timeout or config.timeout,
        read=config.read_timeout or config.timeout,
    )


def _auth_headers(config: LucntConfig) -> dict[str, str]:
    """Build the standing request headers (bearer credential + user agent)."""
    return {
        "Authorization": f"Bearer {config.api_key.get_secret_value()}",
        "User-Agent": _USER_AGENT,
    }


class Transport:
    """Thin wrapper around a pooled `httpx.Client` bound to one `LucntConfig`."""

    def __init__(
        self, config: LucntConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        """Build the pooled client; `transport` is a test injection point."""
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=_timeout(config),
            headers=_auth_headers(config),
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
    ) -> httpx.Response:
        """Send a request, retrying transient failures (network, 5xx, 429).

        Args:
            method: HTTP method.
            path: Path relative to the configured base URL.
            json: Optional JSON request body.
            params: Optional URL query parameters.
            idempotency_key: Sent as `Idempotency-Key`; makes the write retry-safe.
            retry_safe: Retry a write the server dedupes by resource id.

        Raises:
            APIError: A 4xx (typed subclass per status); never retried.
            TransportError: Network failure, timeout, or 5xx after retries.
        """
        headers = _idempotency_headers(idempotency_key)
        attempts = _attempt_count(self._config, method, idempotency_key, retry_safe)

        for attempt in range(attempts):
            last = attempt == attempts - 1
            outcome: httpx.Response | _Transient
            try:
                response = self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                outcome = _transient_from_exc(exc, path)
            else:
                outcome = _classify(response, path, last_attempt=last)
            if isinstance(outcome, httpx.Response):
                return outcome
            if last:
                raise outcome.error
            _sleep(_backoff_delay(self._config, attempt, outcome.retry_after))

        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> Transport:
        """Return self to support use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool on context exit."""
        self.close()


class AsyncTransport:
    """Async `Transport`, bound to the event loop that created it."""

    def __init__(
        self, config: LucntConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Build the pooled async client; `transport` is a test injection point."""
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=_timeout(config),
            headers=_auth_headers(config),
            transport=transport,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
    ) -> httpx.Response:
        """Async `Transport.request`; same retry and error semantics."""
        headers = _idempotency_headers(idempotency_key)
        attempts = _attempt_count(self._config, method, idempotency_key, retry_safe)

        for attempt in range(attempts):
            last = attempt == attempts - 1
            outcome: httpx.Response | _Transient
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                outcome = _transient_from_exc(exc, path)
            else:
                outcome = _classify(response, path, last_attempt=last)
            if isinstance(outcome, httpx.Response):
                return outcome
            if last:
                raise outcome.error
            await _asleep(_backoff_delay(self._config, attempt, outcome.retry_after))

        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        """Close the pool; must be awaited on the loop that built it."""
        await self._client.aclose()

    async def __aenter__(self) -> AsyncTransport:
        """Return self to support use as an async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool on context exit."""
        await self.aclose()
