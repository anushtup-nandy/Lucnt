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
    """Raised by `authorize()`/`spend()` in strict mode when policy blocks a spend."""


class BudgetExhausted(LucntError):
    """Raised by `authorize()`/`spend()` in strict mode when a budget is exhausted."""
