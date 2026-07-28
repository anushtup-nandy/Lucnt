"""Public exception hierarchy for the lucnt SDK.

Every error the SDK raises inherits from `LucntError` so callers can catch
one type and still discriminate on subclass when they need to.
"""

from __future__ import annotations


class LucntError(Exception):
    """Base class for all errors raised by the lucnt SDK."""


class ConfigurationError(LucntError):
    """Raised when the SDK is used before `lucnt.configure()` has succeeded."""


class TransportError(LucntError):
    """Raised on network failures, timeouts, and 5xx responses.

    These are the retryable failures: nothing is known about whether the
    server acted, so a retry must carry the original idempotency key.
    """


class APIError(LucntError):
    """Base for 4xx responses the API returned deliberately (T5-9).

    Carries the parsed `detail` rather than the raw response body — a 4xx from
    an edge proxy is often HTML, and interpolating that into a message is how
    a stack trace ends up with a page of markup in it.

    Attributes:
        status_code: The HTTP status code, or None if unavailable.
        detail: The API's `detail` field when the body was JSON, else a
            truncated snippet of the response text.
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
    """Raised on 400/422: the request was malformed or failed validation.

    Never retry this — the same request will fail identically.
    """


class NotFoundError(APIError):
    """Raised on 404: no such agent, entity, authorization, or transaction."""


class ConflictError(APIError):
    """Raised on 409: the request collided with the resource's current state.

    Note this is *not* how a duplicate `capture()` surfaces — the backend
    dedupes a retried capture on `authorization_id` and replays the original
    transaction with a 200. A 409 from capture means something genuinely
    different, e.g. capturing an authorization that policy blocked.
    """


class RateLimitError(APIError):
    """Raised on 429: the API's rate limiter rejected the request.

    Attributes:
        retry_after: Seconds to wait, parsed from the `Retry-After` header,
            or None when the server did not send one.
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


class PolicyBlocked(LucntError):
    """Raised by `authorize()`/`spend()` in strict mode on a non-approved verdict.

    Carries the verdict's `status` (blocked/review/human_hold), machine-readable
    `code`, and human `reason` so callers that catch it can still branch.
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
    """A `PolicyBlocked` whose `code == "budget_exhausted"`.

    Subclasses `PolicyBlocked` so `except PolicyBlocked` catches budget blocks
    too, while callers who care specifically can catch `BudgetExhausted`.
    """
