"""Idempotent authorize + capture (PRODUCTION_PLAN T0-6).

A timed-out `spend()` that actually committed server-side must not double-book
when retried. The client sends the same `Idempotency-Key`; the server returns the
original hold (authorize) and the original transaction (capture) instead of
minting fresh ones.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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
    Transaction,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(db_session: Session, budget: str = "9000") -> tuple[Company, Agent, str]:
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
        monthly_budget=Decimal(budget),
    )
    db_session.add(agent)
    db_session.flush()
    return company, agent, plaintext


def _hdrs(key: str, idem: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return headers


def _authorize(
    client: TestClient, key: str, amount: str = "10", idem: str | None = None
) -> dict:
    return client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": amount,
            "reason": "x",
        },
        headers=_hdrs(key, idem),
    ).json()


def _count_authorizations(db_session: Session, company: Company) -> int:
    return db_session.execute(
        select(func.count())
        .select_from(Authorization)
        .where(Authorization.company_id == company.id)
    ).scalar_one()


def test_authorize_idempotent_returns_same_hold(
    client: TestClient, db_session: Session
) -> None:
    company, _, key = _seed(db_session)
    idem = str(uuid.uuid4())

    first = _authorize(client, key, idem=idem)
    second = _authorize(client, key, idem=idem)

    assert first["authorization_id"] == second["authorization_id"]
    assert _count_authorizations(db_session, company) == 1


def test_authorize_idempotent_preserves_blocked_verdict(
    client: TestClient, db_session: Session
) -> None:
    """A replayed blocked verdict keeps its status AND machine code."""
    _, _, key = _seed(db_session, budget="1")
    idem = str(uuid.uuid4())

    first = _authorize(client, key, amount="999", idem=idem)
    second = _authorize(client, key, amount="999", idem=idem)

    assert first["status"] == second["status"] == "blocked"
    assert first["code"] == second["code"] == "budget_exhausted"
    assert first["authorization_id"] == second["authorization_id"]


def test_different_idempotency_keys_create_distinct_holds(
    client: TestClient, db_session: Session
) -> None:
    company, _, key = _seed(db_session)

    first = _authorize(client, key, idem=str(uuid.uuid4()))
    second = _authorize(client, key, idem=str(uuid.uuid4()))

    assert first["authorization_id"] != second["authorization_id"]
    assert _count_authorizations(db_session, company) == 2


def test_spend_retry_does_not_double_book(
    client: TestClient, db_session: Session
) -> None:
    """The full spend() retry: same idempotency key -> one hold, one transaction,
    budget charged once (the double-book the old code allowed)."""
    company, agent, key = _seed(db_session)
    idem = str(uuid.uuid4())

    # First spend(): authorize + capture.
    v1 = _authorize(client, key, amount="100", idem=idem)
    cap1 = client.post(
        "/transactions/capture",
        json={"authorization_id": v1["authorization_id"], "source": "sdk"},
        headers=_hdrs(key),
    )
    assert cap1.status_code == 200

    # Retry: same idempotency key -> same hold -> capture is idempotent.
    v2 = _authorize(client, key, amount="100", idem=idem)
    assert v2["authorization_id"] == v1["authorization_id"]
    cap2 = client.post(
        "/transactions/capture",
        json={"authorization_id": v2["authorization_id"], "source": "sdk"},
        headers=_hdrs(key),
    )
    assert cap2.status_code == 200
    assert cap2.json()["id"] == cap1.json()["id"]

    # Exactly one hold, one transaction, budget charged once.
    assert _count_authorizations(db_session, company) == 1
    txn_count = db_session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.company_id == company.id)
    ).scalar_one()
    assert txn_count == 1
    db_session.refresh(agent)
    assert agent.spent_ytd == Decimal("100.0000")
