"""Internal HTTP transport for the lucnt SDK.

`Transport` is the single choke point every SDK call (`authorize`,
`capture`, `spend`, ...) routes through. It maps HTTP failures onto the
typed exception hierarchy in `exceptions.py` so callers never see a bare
`httpx` exception, and forwards the caller's `Idempotency-Key` so a retried
write replays server-side instead of double-booking (T0-6).

Retries/backoff land in T5-3 on top of this.
"""

from __future__ import annotations

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

_STATUS_EXCEPTIONS: dict[int, type[APIError]] = {
    400: InvalidRequestError,
    401: AuthError,
    403: AuthError,
    404: NotFoundError,
    409: ConflictError,
    422: InvalidRequestError,
    429: RateLimitError,
}


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
            timeout=config.timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "User-Agent": _USER_AGENT,
            },
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """Send a request and raise a typed `LucntError` on failure.

        Args:
            method: HTTP method, e.g. `"POST"`.
            path: Path relative to the configured base URL.
            json: Optional JSON request body.
            params: Optional URL query parameters (e.g. for GET reads).
            idempotency_key: Sent as `Idempotency-Key`. The server replays the
                original result for a repeat key instead of acting twice (T0-6),
                which is what makes retrying a write safe.

        Returns:
            The successful `httpx.Response` (status < 400).

        Raises:
            AuthError: The API key was rejected (401/403).
            NotFoundError: No such resource (404).
            ConflictError: The request collided with resource state (409).
            InvalidRequestError: Malformed or invalid request (400/422).
            RateLimitError: The rate limiter rejected the request (429).
            APIError: Any other 4xx response.
            TransportError: A network failure, timeout, or 5xx response.
        """
        headers = (
            {"Idempotency-Key": idempotency_key}
            if idempotency_key is not None
            else None
        )
        try:
            response = self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise TransportError(f"Request to {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"Request to {path} failed: {exc}") from exc

        if response.status_code >= 500:
            raise TransportError(f"Server error for {path}: {response.status_code}")
        if response.status_code >= 400:
            self._raise_for_error(response, path)
        return response

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
