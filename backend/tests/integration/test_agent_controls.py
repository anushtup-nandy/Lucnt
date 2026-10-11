"""Agent / entity state controls at authorize.

"Pause this agent" must actually stop spend — a paused/disabled/non-live agent,
or an inactive entity, is blocked before the policy engine runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import (
    Agent,
    AgentStatus,
    ApiKey,
    ApiKeyMode,
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


def _seed(
    db_session: Session,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    is_live: bool = True,
    entity_active: bool = True,
) -> str:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db_session.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="k",
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
        is_active=entity_active,
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        Agent(
            company_id=company.id,
            entity_id=entity.id,
            name="Ops",
            slug="ops",
            glyph="x",
            role="Operations",
            monthly_budget=Decimal("9000"),
            status=status,
            is_live=is_live,
        )
    )
    db_session.flush()
    return plaintext


def _authorize(client: TestClient, key: str) -> dict:
    return client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": "10",
            "reason": "x",
        },
        headers={"Authorization": f"Bearer {key}"},
    ).json()


def test_active_agent_still_approves(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    assert _authorize(client, key)["status"] == "approved"


@pytest.mark.parametrize("status", [AgentStatus.PAUSED, AgentStatus.DISABLED])
def test_non_active_agent_is_blocked(
    client: TestClient, db_session: Session, status: AgentStatus
) -> None:
    key = _seed(db_session, status=status)
    verdict = _authorize(client, key)
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "agent_inactive"


def test_non_live_agent_is_blocked(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session, is_live=False)
    verdict = _authorize(client, key)
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "agent_inactive"


def test_inactive_entity_is_blocked(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session, entity_active=False)
    verdict = _authorize(client, key)
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "entity_inactive"
