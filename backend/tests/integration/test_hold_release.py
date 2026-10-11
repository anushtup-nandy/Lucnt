"""Orphaned-hold release: `void()` and the server-side TTL.

An approved-but-uncaptured hold reserves the agent's budget — deliberately,
so an agent cannot authorize N times before capturing. The cost of that
fail-closed choice is that a hold nobody ever captures reserves budget *forever*:
one vendor error after `authorize()` returns, or one crash between authorize and
capture, permanently and silently shrinks what the agent may spend.

Two independent releases fix that, and they behave differently on purpose:

* `void()` — the caller states the spend did not happen. Stops reserving, and a
  later capture is refused (the caller already said the money never moved).
* the TTL — age alone. Stops reserving, but a later capture is still booked: a
  slow-but-real spend is a real spend, and dropping it from the ledger is worse
  than a stale forecast.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    Authorization,
    Company,
    Entity,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(db_session: Session, budget: str = "100") -> str:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db_session.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="test key",
            mode=ApiKeyMode.LIVE,
        )
    )
    entity = Entity(
        company_id=company.id,
        name="LUCNT SG",
        code="sg",
        full_name="LUCNT SG Pte Ltd",
        currency="SGD",
        accounting_std="SFRS",
        tax_regime="SG-GST",
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        Agent(
            company_id=company.id,
            entity_id=entity.id,
            name="Ops Agent",
            slug="ops",
            glyph="x",
            role="Operations",
            monthly_budget=Decimal(budget),
        )
    )
    db_session.flush()
    return plaintext


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _authorize(client: TestClient, key: str, amount: str) -> dict:
    return client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": amount,
            "currency": "SGD",
            "reason": "x",
        },
        headers=_headers(key),
    ).json()


def _void(client: TestClient, key: str, auth_id: str, reason: str | None = None):
    body: dict[str, object] = {"authorization_id": auth_id}
    if reason is not None:
        body["reason"] = reason
    return client.post("/transactions/void", json=body, headers=_headers(key))


def _capture(client: TestClient, key: str, auth_id: str):
    return client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk"},
        headers=_headers(key),
    )


# --- void() frees the reservation ---------------------------------------------


def test_void_frees_the_reserved_budget(
    client: TestClient, db_session: Session
) -> None:
    """The headline: without void(), the second $60 stays blocked forever because
    the first hold never stops reserving."""
    key = _seed(db_session, budget="100")

    first = _authorize(client, key, "60")
    assert first["status"] == "approved"
    assert _authorize(client, key, "60")["status"] == "blocked"

    assert _void(client, key, first["authorization_id"]).status_code == 200

    # The released hold no longer reserves, so the same spend now fits.
    assert _authorize(client, key, "60")["status"] == "approved"


def test_void_records_when_and_why(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]

    body = _void(client, key, auth_id, reason="vendor rejected the request").json()
    assert body["authorization_id"] == auth_id
    assert body["release_reason"] == "vendor rejected the request"
    assert body["already_released"] is False
    # `Z` rather than `+00:00`; datetime.fromisoformat only accepts Z from 3.11.
    released_at = datetime.fromisoformat(body["released_at"].replace("Z", "+00:00"))
    assert released_at <= datetime.now(UTC)


def test_void_is_idempotent(client: TestClient, db_session: Session) -> None:
    """The SDK calls void() from a failure path, so a retry must not become a
    second failure to handle."""
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]

    first = _void(client, key, auth_id, reason="original").json()
    second = _void(client, key, auth_id, reason="a different reason").json()

    assert second["already_released"] is True
    # The original release is what is reported — not overwritten by the retry.
    assert second["released_at"] == first["released_at"]
    assert second["release_reason"] == "original"


def test_void_of_an_unknown_authorization_is_404(
    client: TestClient, db_session: Session
) -> None:
    key = _seed(db_session)
    assert _void(client, key, str(uuid.uuid4())).status_code == 404


def test_void_is_scoped_to_the_calling_company(
    client: TestClient, db_session: Session
) -> None:
    """A hold belonging to another company must be invisible, not voidable."""
    key_a = _seed(db_session)
    key_b = _seed(db_session)
    auth_id = _authorize(client, key_a, "10")["authorization_id"]
    assert _void(client, key_b, auth_id).status_code == 404


# --- void() vs capture: the two orderings ------------------------------------


def test_capturing_a_voided_hold_is_refused(
    client: TestClient, db_session: Session
) -> None:
    """The caller said the spend did not happen; booking it afterwards would
    contradict them."""
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]
    _void(client, key, auth_id, reason="never happened")

    response = _capture(client, key, auth_id)
    assert response.status_code == 409
    assert "voided" in response.json()["detail"]


def test_voiding_a_captured_hold_is_refused(
    client: TestClient, db_session: Session
) -> None:
    """The money moved and the ledger says so — reverse it, don't void it."""
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]
    assert _capture(client, key, auth_id).status_code == 200

    response = _void(client, key, auth_id)
    assert response.status_code == 409
    assert "reverse" in response.json()["detail"]


# --- the TTL -----------------------------------------------------------------


def test_authorize_stamps_an_expiry(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]

    hold = db_session.get(Authorization, uuid.UUID(auth_id))
    assert hold is not None
    assert hold.expires_at is not None
    assert hold.expires_at > datetime.now(UTC)


def test_an_expired_hold_stops_reserving(
    client: TestClient, db_session: Session
) -> None:
    """Age alone releases the reservation, so no sweep job is needed and a
    restarted backend cannot leave a backlog of stale reservations behind."""
    key = _seed(db_session, budget="100")

    first = _authorize(client, key, "60")
    assert _authorize(client, key, "60")["status"] == "blocked"

    hold = db_session.get(Authorization, uuid.UUID(first["authorization_id"]))
    assert hold is not None
    hold.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    assert _authorize(client, key, "60")["status"] == "approved"


def test_an_expired_hold_can_still_be_captured(
    client: TestClient, db_session: Session
) -> None:
    """A slow-but-real spend arriving after the TTL is still a real spend.
    Refusing it would lose money from the ledger, which is strictly worse than
    the stale reservation the TTL already released."""
    key = _seed(db_session)
    auth_id = _authorize(client, key, "10")["authorization_id"]

    hold = db_session.get(Authorization, uuid.UUID(auth_id))
    assert hold is not None
    hold.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    assert _capture(client, key, auth_id).status_code == 200


def test_a_hold_with_no_expiry_keeps_reserving(
    client: TestClient, db_session: Session
) -> None:
    """Rows written before the TTL column existed carry NULL, and must keep the
    fail-closed behaviour they were written under — the migration must not
    release every historical hold at once."""
    key = _seed(db_session, budget="100")

    first = _authorize(client, key, "60")
    hold = db_session.get(Authorization, uuid.UUID(first["authorization_id"]))
    assert hold is not None
    hold.expires_at = None
    db_session.flush()

    assert _authorize(client, key, "60")["status"] == "blocked"


# --- velocity ----------------------------------------------------------------


def test_a_voided_hold_no_longer_counts_against_velocity(
    client: TestClient, db_session: Session
) -> None:
    """Velocity counts uncaptured holds to catch a runaway, but a hold the
    caller correctly cleaned up is not runaway activity."""
    key = _seed(db_session, budget="10000")
    auth_id = _authorize(client, key, "1")["authorization_id"]

    hold = db_session.get(Authorization, uuid.UUID(auth_id))
    assert hold is not None
    assert hold.released_at is None

    _void(client, key, auth_id)
    db_session.refresh(hold)
    assert hold.released_at is not None
