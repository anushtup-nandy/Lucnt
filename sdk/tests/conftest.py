"""Shared fixtures for the lucnt SDK test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lucnt import client


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Make retry backoff instant; yields requested delays for assertions.

    Autouse because retries are on by default.
    """
    delays: list[float] = []
    monkeypatch.setattr(client, "_sleep", delays.append)
    yield delays


@pytest.fixture(autouse=True)
def no_real_async_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Async twin of `no_real_sleep`; `_asleep` must stay a coroutine function."""
    delays: list[float] = []

    async def _record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(client, "_asleep", _record)
    yield delays
