"""Smoke tests for the top-level lucnt package surface."""

from __future__ import annotations

import lucnt


def test_public_surface_is_importable() -> None:
    assert callable(lucnt.configure)
    assert callable(lucnt.task)
    assert callable(lucnt.authorize)
    assert callable(lucnt.capture)
    assert callable(lucnt.spend)
    assert isinstance(lucnt.__version__, str)
    assert issubclass(lucnt.PolicyBlocked, lucnt.LucntError)
    assert issubclass(lucnt.BudgetExhausted, lucnt.LucntError)
    assert issubclass(lucnt.AuthError, lucnt.LucntError)
    assert issubclass(lucnt.TransportError, lucnt.LucntError)
    assert issubclass(lucnt.ConfigurationError, lucnt.LucntError)
