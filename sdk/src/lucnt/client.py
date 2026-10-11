"""Internal HTTP transport for the lucnt SDK — synchronous and async.

`Transport` / `AsyncTransport` are the single choke point every SDK call
(`authorize`, `capture`, `spend`, ...) routes through. They map HTTP failures
onto the typed exception hierarchy in `exceptions.py` so callers never see a
bare `httpx` exception, forward the caller's `Idempotency-Key` so a retried
write replays server-side instead of double-booking, and retry transient
failures with jittered exponential backoff.

**The two share every decision and differ only in the lines that do I/O**.
Retry eligibility, the attempt count, the backoff schedule, response
classification, and the status→exception mapping are all module-level and pure;
each class contributes a short loop around them. That split is deliberate rather
than tidy: a divergence in *which* failures the async path retries, or in
whether it sends the idempotency key, is a double-booked spend — not a cosmetic
inconsistency. Sharing the decisions makes such a drift impossible to introduce
in one path only.
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

# The backend persists the key in a String(255) column with a partial unique
# index on (company_id, idempotency_key). Declared here, beside the header that
# carries it, and read by every producer of a key — a second copy of this number
# is a copy that eventually disagrees with the column.
MAX_IDEMPOTENCY_KEY_CHARS = 255

# A 4xx from an edge proxy is often an HTML error page. Cap what reaches the
# exception message so a traceback never carries a screenful of markup.
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


def check_idempotency_key(value: str, source: str) -> str:
    """Validate a key against the server's column before it can silently collide.

    A blank key is the dangerous case: it is falsy server-side, so it would
    either be ignored (no dedupe at all, and the caller believes they have it) or
    dedupe *every* blank-keyed request in the company against the first one. An
    over-long key would be truncated into a collision with whatever shares its
    first 255 characters.

    Args:
        value: The candidate key.
        source: What produced it, named in the error message — e.g.
            `"operation() op_id"` or `"idempotency_key"`.

    Returns:
        The key, whitespace-stripped.

    Raises:
        ConfigurationError: The key is blank or longer than the column.
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
    """Block between retry attempts.

    A module-level indirection so tests can assert the backoff *schedule*
    without spending real wall-clock on it.
    """
    time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    """Yield to the event loop between retry attempts.

    The async twin of `_sleep`, and patched the same way in tests. Never
    `time.sleep` here: blocking the loop for a backoff delay would stall every
    other coroutine in the process, which is the whole failure the async
    transport exists to remove.
    """
    await asyncio.sleep(seconds)


def truncate_body(text: str) -> str:
    """Cap a response body to what may safely appear in an error message.

    Shared with `ledger.py`, which quotes an unreadable body in a
    `ContractError`: one policy for "never put a screenful of an edge proxy's
    HTML into a traceback", applied wherever a body reaches a message.
    """
    return text.strip()[:_MAX_DETAIL_CHARS]


def _parse_detail(response: httpx.Response) -> str | None:
    """Pull FastAPI's `detail` out of an error body, falling back to text.

    Returns None when the body is empty. Never returns more than
    `_MAX_DETAIL_CHARS` characters. Safe on an async response: both transports
    read the body in full before classifying, so `.json()` / `.text` never
    block here.
    """
    try:
        payload = response.json()
    except ValueError:
        return truncate_body(response.text) or None
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])[:_MAX_DETAIL_CHARS]
    return str(payload)[:_MAX_DETAIL_CHARS] or None


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Read `Retry-After` as seconds, ignoring the HTTP-date form.

    The backend's limiter always sends delta-seconds; a date-form
    header from an intermediary is dropped rather than guessed at.
    """
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
    """A failure worth retrying, carrying what to raise if attempts run out.

    Returned rather than raised so the caller's loop decides between sleeping
    and giving up — the one decision that differs between the sync and async
    transports (`_sleep` vs `await _asleep`).
    """

    error: TransportError
    retry_after: float | None = None


def _transient_from_exc(exc: httpx.HTTPError, path: str) -> _Transient:
    """Wrap an httpx-level failure as a retryable `_Transient`.

    A timeout gets its own message because it is the one network failure a
    caller can act on by raising `read_timeout` rather than by investigating
    connectivity.
    """
    if isinstance(exc, httpx.TimeoutException):
        error = TransportError(f"Request to {path} timed out")
    else:
        error = TransportError(f"Request to {path} failed: {exc}")
    error.__cause__ = exc
    return _Transient(error)


def _classify(
    response: httpx.Response, path: str, *, last_attempt: bool
) -> httpx.Response | _Transient:
    """Decide what a response means: success, retryable, or a typed refusal.

    A 4xx other than 429 is never retried — a policy block is an answer, not a
    blip, and repeating it just burns the agent's latency budget to be refused
    again. A 429 *is* retryable, but on the final attempt the caller gets the
    typed `RateLimitError` (carrying `retry_after`) so it can schedule its own
    retry rather than a bare `TransportError`.

    Returns:
        The response itself on success, or a `_Transient` to retry.

    Raises:
        APIError: Any deliberate 4xx (subclass per `_STATUS_EXCEPTIONS`).
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

    Conservative by default: a request is retried only if it is a safe method,
    carries an idempotency key, or the call site opted in. An unkeyed POST is
    **not** retried — it may have landed server-side before the connection
    dropped, and repeating it is the double-book.
    """
    eligible = (
        retry_safe or method.upper() in _SAFE_METHODS or idempotency_key is not None
    )
    return config.max_retries + 1 if eligible else 1


def _backoff_delay(
    config: LucntConfig, attempt: int, retry_after: float | None
) -> float:
    """Seconds to wait before `attempt` (0-based), capped by `backoff_max`.

    Exponential with **full jitter** — a uniform draw from `[0, window)` rather
    than the window itself. Without it, every client that failed on the same
    blip retries in the same millisecond and reproduces the load that caused the
    failure.

    A server-sent `Retry-After` wins when honoured, but is still capped: a
    hostile or misconfigured intermediary must not be able to park the caller's
    agent for an hour.
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
    """Build the per-phase timeout both transports use.

    The connect/read split exists because connect and read fail for different reasons
    — a dead host should give up fast, a slow policy evaluation should not. Both
    fall back to the overall timeout.
    """
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
        """Build the pooled client.

        Args:
            config: Resolved SDK configuration (API key, base URL, timeouts,
                retry policy).
            transport: Optional `httpx.BaseTransport` override, used in tests
                to inject an `httpx.MockTransport` instead of real sockets.
        """
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
                original result for a repeat key instead of acting twice,
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
    """Thin wrapper around a pooled `httpx.AsyncClient` bound to one `LucntConfig`.

    Behaviourally identical to `Transport` — same retry eligibility, same
    backoff schedule, same status→exception mapping, all shared at module level
    — differing only in awaiting the request and the backoff.

    **Bound to the event loop that created it.** An `httpx.AsyncClient`'s
    connection pool holds loop-bound objects, so one built on a loop that has
    since closed cannot serve requests on a new loop. `ledger.py` therefore
    keeps one instance per running loop rather than a process-wide singleton.
    """

    def __init__(
        self, config: LucntConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Build the pooled async client.

        Args:
            config: Resolved SDK configuration (API key, base URL, timeouts,
                retry policy).
            transport: Optional `httpx.AsyncBaseTransport` override, used in
                tests to inject an `httpx.MockTransport` instead of real sockets.
        """
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
        """Send a request, retrying transient failures, and raise typed errors.

        Identical in behaviour to `Transport.request` — same eligibility rules,
        same backoff schedule, same exception mapping, all shared at module
        level — except that the request and the backoff are awaited.

        Args:
            method: HTTP method, e.g. `"POST"`.
            path: Path relative to the configured base URL.
            json: Optional JSON request body.
            params: Optional URL query parameters (e.g. for GET reads).
            idempotency_key: Sent as `Idempotency-Key`. The server replays the
                original result for a repeat key instead of acting twice,
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
        """Close the underlying connection pool.

        Must be awaited from the loop that built the pool. `lucnt.aclose()` is
        the public shutdown call; this is what it delegates to.
        """
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
