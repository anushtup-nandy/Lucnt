"""Smoke tests: the app imports and the health endpoint responds."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    """The /health endpoint returns ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_roundtrip() -> None:
    """A hashed password verifies and a signed token decodes."""
    from lucnt.security import (
        create_access_token,
        get_password_hash,
        verify_password,
        verify_token,
    )

    hashed = get_password_hash("s3cret")
    assert verify_password("s3cret", hashed)
    assert not verify_password("wrong", hashed)

    token = create_access_token({"sub": "abc", "role": "user"})
    payload = verify_token(token)
    assert payload is not None
    assert payload["sub"] == "abc"
