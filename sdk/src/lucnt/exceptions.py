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


class ContractError(LucntError):
    """Raised when a 2xx response does not match the contract the SDK expects.

    Sits between the two neighbours it is deliberately *not*: `APIError` is the
    API refusing on purpose, and `TransportError` is retryable. This is neither.
    A body that failed to parse, lacked an expected key, or failed model
    validation will fail identically on a retry, so it must never be retried —
    it means this SDK version and the backend disagree about the wire contract.

    Before this existed, those three failures escaped as a raw
    `json.JSONDecodeError`, `KeyError`, or `pydantic.ValidationError` — outside
    the hierarchy, so `except LucntError` missed them and a caller's money-path
    error handling did not cover the case where the SDK could not read the
    verdict it had just been given.

    Attributes:
        path: The API path whose response could not be read, when known.
    """

    def __init__(self, message: str, *, path: str | None = None) -> None:
        """Store the path alongside the message."""
        super().__init__(message)
        self.path = path


class APIError(LucntError):
    """Base for 4xx responses the API returned deliberately.

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


class IdempotencyConflict(LucntError):
    """Raised when one `operation()` scope is used for two different spends.

    The server replays the first hold for a repeated idempotency key *without
    comparing the payload*, so a second, materially different spend under the
    same op id would be handed a verdict belonging to the first one. That is a
    silent double-book in the opposite direction — an unapproved spend reported
    as approved — so the SDK refuses locally instead.

    One `operation()` scope is one logical spend. Open a separate scope per
    spend, or pass an explicit `idempotency_key=` to opt out.
    """


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
