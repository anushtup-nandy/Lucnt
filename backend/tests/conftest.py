"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from lucnt.main import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """Return a TestClient wrapping a fresh app instance."""
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
