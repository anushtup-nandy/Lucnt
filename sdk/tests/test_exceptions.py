"""Unit tests for the lucnt exception hierarchy."""

from __future__ import annotations

import pytest

from lucnt.exceptions import (
    AuthError,
    BudgetExhausted,
    ConfigurationError,
    LucntError,
    PolicyBlocked,
    TransportError,
)


@pytest.mark.parametrize(
    "exc_type",
    [ConfigurationError, AuthError, TransportError, PolicyBlocked, BudgetExhausted],
)
def test_all_errors_subclass_lucnt_error(exc_type: type[LucntError]) -> None:
    """Every public error type must be catchable as a LucntError."""
    assert issubclass(exc_type, LucntError)
