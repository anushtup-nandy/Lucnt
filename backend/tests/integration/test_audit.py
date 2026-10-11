"""Governance mutations leave an append-only before/after audit record."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    AuditLog,
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


def _seed(db_session: Session) -> tuple[Company, Agent, ApiKey, str]:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    api_key = ApiKey(
        company_id=company.id,
        key_prefix=plaintext[:12],
        key_hash=hash_api_key(plaintext),
        name="admin key",
        mode=ApiKeyMode.LIVE,
    )
    db_session.add(api_key)
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
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops",
        slug="ops",
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("9000"),
    )
    db_session.add(agent)
    db_session.flush()
    return company, agent, api_key, plaintext


def _audit_rows(db_session: Session, company: Company) -> list[AuditLog]:
    return list(
        db_session.execute(select(AuditLog).where(AuditLog.company_id == company.id))
        .scalars()
        .all()
    )


def test_budget_update_is_audited(client: TestClient, db_session: Session) -> None:
    company, agent, api_key, key = _seed(db_session)

    resp = client.patch(
        f"/budgets/{agent.id}",
        json={"monthly_budget": "12000"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    rows = _audit_rows(db_session, company)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.action == "budget.update"
    assert entry.entity_type == "agent"
    assert entry.entity_id == agent.id
    assert entry.actor_api_key_id == api_key.id
    assert entry.before == {"monthly_budget": "9000.0000"}
    assert entry.after == {"monthly_budget": "12000"}


def test_failed_budget_update_leaves_no_audit_row(
    client: TestClient, db_session: Session
) -> None:
    """The audit row commits with the mutation — a rejected change writes nothing."""
    company, _, _, key = _seed(db_session)

    resp = client.patch(
        f"/budgets/{uuid.uuid4()}",  # unknown agent -> 404, no mutation
        json={"monthly_budget": "12000"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert _audit_rows(db_session, company) == []
