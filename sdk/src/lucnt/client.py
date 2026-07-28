"""Internal HTTP transport for the lucnt SDK.

`Transport` is the single choke point every SDK call (`authorize`,
`capture`, `spend`, ...) routes through. It maps HTTP failures onto the
typed exception hierarchy in `exceptions.py` so callers never see a bare
`httpx` exception, and forwards the caller's `Idempotency-Key` so a retried
write replays server-side instead of double-booking (T0-6).

Retries/backoff land in T5-3 on top of this.
"""

from __future__ import annotations

import random
import time
from types import TracebackType

import httpx

from lucnt._version import __version__
from lucnt.exceptions import (
    APIError,
    AuthError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    TransportError,
)
from lucnt.models import LucntConfig

_USER_AGENT = f"lucnt-python/{__version__}"

# A 4xx from an edge proxy is often an HTML error page. Cap what reaches the
# exception message so a traceback never carries a screenful of markup (T5-9).
_MAX_DETAIL_CHARS = 200

# Methods with no side effect, so a retry is safe even without an idempotency
# key. Everything else must opt in — see `Transport.request(retry_safe=...)`.
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


def _sleep(seconds: float) -> None:
    """Block between retry attempts.

    A module-level indirection so tests can assert the backoff *schedule*
    without spending real wall-clock on it.
    """
    time.sleep(seconds)


def _parse_detail(response: httpx.Response) -> str | None:
    """Pull FastAPI's `detail` out of an error body, falling back to text.

    Returns None when the body is empty. Never returns more than
    `_MAX_DETAIL_CHARS` characters.
    """
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:_MAX_DETAIL_CHARS] or None
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])[:_MAX_DETAIL_CHARS]
    return str(payload)[:_MAX_DETAIL_CHARS] or None


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read `Retry-After` as seconds, ignoring the HTTP-date form.

    The backend's limiter (T3-7) always sends delta-seconds; a date-form
    header from an intermediary is dropped rather than guessed at.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Transport:
    """Thin wrapper around a pooled `httpx.Client` bound to one `LucntConfig`."""

    def __init__(
        self, config: LucntConfig, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        """Build the pooled client.

        Args:
            config: Resolved SDK configuration (API key, base URL, timeout).
            transport: Optional `httpx.BaseTransport` override, used in tests
                to inject an `httpx.MockTransport` instead of real sockets.
        """
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            # T5-10: connect and read fail for different reasons and deserve
            # different budgets — a dead host should give up fast, a slow policy
            # evaluation should not. Both fall back to the overall timeout.
            timeout=httpx.Timeout(
                config.timeout,
                connect=config.connect_timeout or config.timeout,
                read=config.read_timeout or config.timeout,
            ),
            transport=transport,
            headers={
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "User-Agent": _USER_AGENT,
            },
        )

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before `attempt` (0-based), capped by `backoff_max`.

        Exponential with **full jitter** — a uniform draw from `[0, window)`
        rather than the window itself. Without it, every client that failed on
        the same blip retries in the same millisecond and reproduces the load
        that caused the failure.

        A server-sent `Retry-After` wins when honoured, but is still capped: a
        hostile or misconfigured intermediary must not be able to park the
        caller's agent for an hour.
        """
        config = self._config
        if retry_after is not None and config.respect_retry_after:
            return min(retry_after, config.backoff_max)
        window = min(config.backoff_base * (2**attempt), config.backoff_max)
        return random.uniform(0, window)  # noqa: S311 — jitter, not cryptography

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
        """Send a request, retrying transient failures, and raise typed errors.

        Retries only transient failures — network errors, timeouts, 5xx and 429
        — and only when the request is safe to repeat. A 4xx is never retried:
        a policy block is an answer, not a blip, and repeating it just burns the
        agent's latency budget to get the same refusal.

        Args:
            method: HTTP method, e.g. `"POST"`.
            path: Path relative to the configured base URL.
            json: Optional JSON request body.
            params: Optional URL query parameters (e.g. for GET reads).
            idempotency_key: Sent as `Idempotency-Key`. The server replays the
                original result for a repeat key instead of acting twice (T0-6),
                which is what makes retrying a write safe — so passing one also
                marks the request retry-safe.
            retry_safe: Force retry eligibility for a write the server dedupes
                by resource id rather than by key (`capture` dedupes on
                `authorization_id`). Reads are eligible automatically.

        Returns:
            The successful `httpx.Response` (status < 400).

        Raises:
            AuthError: The API key was rejected (401/403).
            NotFoundError: No such resource (404).
            ConflictError: The request collided with resource state (409).
            InvalidRequestError: Malformed or invalid request (400/422).
            RateLimitError: The rate limiter rejected the request (429).
            APIError: Any other 4xx response.
            TransportError: A network failure, timeout, or 5xx response — after
                retries are exhausted.
        """
        headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        eligible = (
            retry_safe
            or method.upper() in _SAFE_METHODS
            or idempotency_key is not None
        )
        attempts = self._config.max_retries + 1 if eligible else 1

        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                error: TransportError = TransportError(
                    f"Request to {path} timed out"
                )
                error.__cause__ = exc
            except httpx.HTTPError as exc:
                error = TransportError(f"Request to {path} failed: {exc}")
                error.__cause__ = exc
            else:
                if response.status_code < 400:
                    return response
                if response.status_code >= 500:
                    error = TransportError(
                        f"Server error for {path}: {response.status_code}"
                    )
                elif response.status_code == 429:
                    retry_after = _parse_retry_after(response)
                    # Retryable, but if this is the last attempt the caller gets
                    # the typed RateLimitError (with retry_after) to act on.
                    if attempt == attempts - 1:
                        self._raise_for_error(response, path)
                    error = TransportError(f"Rate limited on {path}")
                else:
                    # Every other 4xx is a deliberate answer — never retried.
                    self._raise_for_error(response, path)
                    raise AssertionError("unreachable")  # pragma: no cover

            if attempt == attempts - 1:
                raise error
            _sleep(self._backoff_delay(attempt, retry_after))

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for_error(response: httpx.Response, path: str) -> None:
        """Map a 4xx response onto the typed exception hierarchy (T5-9)."""
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
