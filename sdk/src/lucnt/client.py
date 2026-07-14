"""Internal HTTP transport for the lucnt SDK.

`Transport` is the single choke point every future SDK call (`authorize`,
`capture`, `spend`, ...) routes through. It maps HTTP failures onto the
typed exception hierarchy in `exceptions.py` so callers never see a bare
`httpx` exception. Retries/backoff and idempotency keys land in P6
(`sdk/PLAN.md` §4) — this is the bare pooled-client plumbing.
"""

from __future__ import annotations

from types import TracebackType

import httpx

from lucnt._version import __version__
from lucnt.exceptions import AuthError, LucntError, TransportError
from lucnt.models import LucntConfig

_USER_AGENT = f"lucnt-python/{__version__}"


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
                "Authorization": f"Bearer {config.api_key}",
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
    ) -> httpx.Response:
        """Send a request and raise a typed `LucntError` on failure.

        Args:
            method: HTTP method, e.g. `"POST"`.
            path: Path relative to the configured base URL.
            json: Optional JSON request body.
            params: Optional URL query parameters (e.g. for GET reads).

        Returns:
            The successful `httpx.Response` (status < 400).

        Raises:
            AuthError: The API key was rejected (401/403).
            TransportError: A network failure, timeout, or 5xx response.
            LucntError: Any other non-2xx response.
        """
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise TransportError(f"Request to {path} timed out") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"Request to {path} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(
                f"Authentication failed for {path}: {response.status_code}"
            )
        if response.status_code >= 500:
            raise TransportError(f"Server error for {path}: {response.status_code}")
        if response.status_code >= 400:
            raise LucntError(
                f"Request to {path} failed: {response.status_code} {response.text}"
            )
        return response

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
