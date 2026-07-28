"""Shared fixtures for the lucnt SDK test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lucnt import client


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Make retry backoff instant, and record the schedule it would have used.

    Autouse because retries are on by default (T5-3): without this, any test
    that provokes a 5xx or a timeout on a retry-eligible request pays real
    wall-clock. Yields the list of requested delays so a test can assert the
    backoff schedule without waiting for it.
    """
    delays: list[float] = []
    monkeypatch.setattr(client, "_sleep", delays.append)
    yield delays
