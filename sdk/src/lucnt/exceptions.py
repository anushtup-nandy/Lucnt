"""Public exception hierarchy; everything inherits from `LucntError`."""

from __future__ import annotations


class LucntError(Exception):
    """Base class for all errors raised by the lucnt SDK."""


class ConfigurationError(LucntError):
    """Raised when the SDK is used before `lucnt.configure()` has succeeded."""


class TransportError(LucntError):
    """Raised on network failures, timeouts, and 5xx responses.

    Retryable, but the server may have acted: retry with the same idempotency key.
    """


class ContractError(LucntError):
    """Raised when a 2xx response does not match the contract the SDK expects.

    Not retryable: the SDK version and backend disagree on the wire contract.

    Attributes:
        path: The API path whose response could not be read, when known.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        """Store the path alongside the message."""
        super().__init__(message)
        self.path = path


class APIError(LucntError):
    """Base for 4xx responses the API returned deliberately.

    Attributes:
        status_code: The HTTP status code, or None if unavailable.
        detail: The API's JSON `detail`, else a truncated body snippet.
        request_id: The `X-Request-Id` echoed by the backend, for support.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Store the response metadata alongside the message."""
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id


class AuthError(APIError):
    """Raised on 401/403 responses: an invalid, revoked, or unauthorized API key."""


class InvalidRequestError(APIError):
    """Raised on 400/422: the request was malformed. Never retry."""


class NotFoundError(APIError):
    """Raised on 404: no such agent, entity, authorization, or transaction."""


class ConflictError(APIError):
    """Raised on 409: the request collided with the resource's current state.

    A retried `capture()` is replayed with 200, not 409; a 409 means e.g.
    capturing a blocked authorization.
    """


class RateLimitError(APIError):
    """Raised on 429: the API's rate limiter rejected the request.

    Attributes:
        retry_after: Seconds from `Retry-After`, or None if absent.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store `retry_after` alongside the standard API error metadata."""
        super().__init__(
            message, status_code=status_code, detail=detail, request_id=request_id
        )
        self.retry_after = retry_after


class IdempotencyConflict(LucntError):
    """Raised when one `operation()` scope is used for two different spends.

    The server replays the first verdict for a repeated key without comparing
    payloads. Use one scope per spend, or pass an explicit `idempotency_key=`.
    """


class PolicyBlocked(LucntError):
    """Raised by `authorize()`/`spend()` in strict mode on a non-approved verdict.

    Carries the verdict's `status`, machine-readable `code`, and `reason`.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str | None = None,
        code: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Store the verdict fields alongside the message."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.reason = reason


class BudgetExhausted(PolicyBlocked):
    """A `PolicyBlocked` whose `code == "budget_exhausted"`."""
