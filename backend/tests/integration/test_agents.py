"""Integration tests for the read-only agents router (Phase 1 (A))."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt.database import get_db
from lucnt.main import app
from lucnt.models import Agent, ApiKey, ApiKeyMode, Company, Entity
from lucnt.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient wired to the test's own rolled-back `db_session`."""
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_company_with_key(db_session: Session) -> tuple[Company, str]:
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
    db_session.flush()
    return company, plaintext


def _make_entity(db_session: Session, company: Company, code: str = "sg") -> Entity:
    entity = Entity(
        company_id=company.id,
        name=f"LUCNT {code.upper()}",
        code=code,
        full_name=f"LUCNT {code.upper()} Pte Ltd",
        currency="SGD",
        accounting_std="SFRS",
        tax_regime=f"{code.upper()}-GST",
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _make_agent(
    db_session: Session, company: Company, entity: Entity, slug: str
) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name=f"{slug.title()} Agent",
        slug=slug,
        glyph="x",
        role="Testing",
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def _auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_list_agents_is_scoped_to_company(
    client: TestClient, db_session: Session
) -> None:
    company_a, key_a = _make_company_with_key(db_session)
    _make_agent(db_session, company_a, _make_entity(db_session, company_a), "ops")

    company_b, key_b = _make_company_with_key(db_session)
    _make_agent(db_session, company_b, _make_entity(db_session, company_b), "coding")

    response_a = client.get("/agents", headers=_auth_headers(key_a))
    assert response_a.status_code == 200
    body_a = response_a.json()
    assert body_a["total"] == 1
    assert body_a["items"][0]["slug"] == "ops"

    response_b = client.get("/agents", headers=_auth_headers(key_b))
    assert response_b.json()["items"][0]["slug"] == "coding"


def test_list_agents_filters_by_entity(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    entity_sg = _make_entity(db_session, company, code="sg")
    entity_us = _make_entity(db_session, company, code="us")
    _make_agent(db_session, company, entity_sg, "ops")
    _make_agent(db_session, company, entity_us, "coding")

    response = client.get("/agents?entity=us", headers=_auth_headers(key))
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["slug"] == "coding"


def test_list_agents_rejects_unknown_entity_filter(
    client: TestClient, db_session: Session
) -> None:
    _, key = _make_company_with_key(db_session)
    response = client.get("/agents?entity=zz", headers=_auth_headers(key))
    assert response.status_code == 404


def test_get_agent_detail(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    agent = _make_agent(db_session, company, _make_entity(db_session, company), "ops")

    response = client.get(f"/agents/{agent.id}", headers=_auth_headers(key))
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "ops"
    assert body["monthly_budget"] == "0.0000"


def test_get_agent_detail_not_found(client: TestClient, db_session: Session) -> None:
    _, key = _make_company_with_key(db_session)
    response = client.get(f"/agents/{uuid.uuid4()}", headers=_auth_headers(key))
    assert response.status_code == 404


def test_get_agent_detail_rejects_cross_company_access(
    client: TestClient, db_session: Session
) -> None:
    company_a, key_a = _make_company_with_key(db_session)
    agent_a = _make_agent(
        db_session, company_a, _make_entity(db_session, company_a), "ops"
    )

    _, key_b = _make_company_with_key(db_session)
    response = client.get(f"/agents/{agent_a.id}", headers=_auth_headers(key_b))
    assert response.status_code == 404
