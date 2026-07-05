"""Public exception hierarchy for the lucnt SDK.

Every error the SDK raises inherits from `LucntError` so callers can catch
one type and still discriminate on subclass when they need to.
"""

from __future__ import annotations


class LucntError(Exception):
    """Base class for all errors raised by the lucnt SDK."""


class ConfigurationError(LucntError):
    """Raised when the SDK is used before `lucnt.configure()` has succeeded."""


class AuthError(LucntError):
    """Raised on 401/403 responses: an invalid, revoked, or unauthorized API key."""


class TransportError(LucntError):
    """Raised on network failures, timeouts, and 5xx responses."""


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
