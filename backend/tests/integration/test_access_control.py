"""API-key scope + mode gating (PRODUCTION_PLAN T0-12, T0-13, T0-16).

Separation of duties: a runtime agent key can spend but cannot raise its own
budget or edit the policies governing it; a test-mode key cannot drive the live
ledger; the interactive docs are off outside development.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app, create_app
from lucnt_api.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    ApiKeyScope,
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


def _make_key(
    db_session: Session,
    company: Company,
    *,
    mode: ApiKeyMode = ApiKeyMode.LIVE,
    scope: ApiKeyScope = ApiKeyScope.ADMIN,
) -> str:
    plaintext = generate_api_key(mode)
    db_session.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="k",
            mode=mode,
            scope=scope,
        )
    )
    db_session.flush()
    return plaintext


def _seed(db_session: Session) -> tuple[Company, Agent]:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
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
    return company, agent


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _authorize(client: TestClient, key: str) -> int:
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
        headers=_hdrs(key),
    ).status_code


def test_runtime_key_can_authorize(client: TestClient, db_session: Session) -> None:
    company, _ = _seed(db_session)
    runtime_key = _make_key(db_session, company, scope=ApiKeyScope.RUNTIME)
    assert _authorize(client, runtime_key) == 200


def test_runtime_key_cannot_raise_its_own_budget(
    client: TestClient, db_session: Session
) -> None:
    """The separation-of-duties fix: the governed principal can't be the governor."""
    company, agent = _seed(db_session)
    runtime_key = _make_key(db_session, company, scope=ApiKeyScope.RUNTIME)

    resp = client.patch(
        f"/budgets/{agent.id}",
        json={"monthly_budget": "99999999"},
        headers=_hdrs(runtime_key),
    )
    assert resp.status_code == 403


def test_admin_key_can_raise_budget(client: TestClient, db_session: Session) -> None:
    company, agent = _seed(db_session)
    admin_key = _make_key(db_session, company, scope=ApiKeyScope.ADMIN)

    resp = client.patch(
        f"/budgets/{agent.id}",
        json={"monthly_budget": "12000"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200


def test_test_mode_key_cannot_touch_live_ledger(
    client: TestClient, db_session: Session
) -> None:
    company, _ = _seed(db_session)
    test_key = _make_key(db_session, company, mode=ApiKeyMode.TEST)
    assert _authorize(client, test_key) == 403


def test_test_mode_key_can_read(client: TestClient, db_session: Session) -> None:
    company, _ = _seed(db_session)
    test_key = _make_key(db_session, company, mode=ApiKeyMode.TEST)
    # A read endpoint is fine for a test key.
    assert client.get("/transactions", headers=_hdrs(test_key)).status_code == 200


def test_docs_disabled_outside_development(monkeypatch: pytest.MonkeyPatch) -> None:
    from lucnt_api import main

    monkeypatch.setattr(main.settings, "environment", "production")
    prod_app = create_app()
    assert prod_app.docs_url is None
    assert prod_app.openapi_url is None

    monkeypatch.setattr(main.settings, "environment", "development")
    dev_app = create_app()
    assert dev_app.docs_url == "/docs"
