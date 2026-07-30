"""Integration tests for GET /pricing/feed (F-2)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import ApiKey, ApiKeyMode, Company
from lucnt_api.security import generate_api_key, hash_api_key
from lucnt_api.services.pricing_feed import resolve_feed


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient wired to the test's own rolled-back `db_session`."""
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_key(db: Session) -> str:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(company)
    db.flush()
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="test key",
            mode=ApiKeyMode.LIVE,
        )
    )
    db.flush()
    return plaintext


def test_feed_requires_an_api_key(client: TestClient) -> None:
    assert client.get("/pricing/feed").status_code == 401


def test_feed_serves_todays_resolved_prices(
    client: TestClient, db_session: Session
) -> None:
    """The endpoint must serve exactly what the resolver produces for today —
    including a date-ranged rate like claude-sonnet-5's intro window — with
    the SDK pack shape and cache headers."""
    key = _make_key(db_session)
    response = client.get("/pricing/feed", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"

    body = response.json()
    expected = resolve_feed(datetime.now(UTC).date())
    assert body == expected
    # Shape the SDK's parser expects: version + estimation + vendors, and
    # every price is a string.
    assert set(body) >= {"version", "estimation", "vendors"}
    sonnet = body["vendors"]["anthropic"]["models"]["claude-sonnet-5"]
    assert isinstance(sonnet["input"], str)
    assert "rates" not in sonnet
