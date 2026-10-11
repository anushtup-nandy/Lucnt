"""Non-positive money is rejected at the wire (422) and by DB CHECKs."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
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
    JournalEntry,
    Transaction,
    TransactionSource,
    VerdictStatus,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(db_session: Session) -> tuple[Company, Entity, Agent, str]:
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
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops Agent",
        slug="ops",
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("9000"),
    )
    db_session.add(agent)
    db_session.flush()
    return company, entity, agent, plaintext


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- Schema rejects non-positive money at the wire (422) ---


@pytest.mark.parametrize("amount", ["0", "-5", "-0.0001"])
def test_authorize_rejects_non_positive_amount(
    client: TestClient, db_session: Session, amount: str
) -> None:
    _, _, _, key = _seed(db_session)
    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": amount,
            "reason": "negative-amount attack",
        },
        headers=_headers(key),
    )
    assert response.status_code == 422


def test_authorize_rejects_negative_tokens(
    client: TestClient, db_session: Session
) -> None:
    _, _, _, key = _seed(db_session)
    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": "10",
            "reason": "x",
            "tokens": -1,
        },
        headers=_headers(key),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("amount", ["0", "-1000000"])
def test_capture_rejects_non_positive_settlement(
    client: TestClient, db_session: Session, amount: str
) -> None:
    """A negative settlement override would decrement spent_ytd (budget refund)."""
    _, _, _, key = _seed(db_session)
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": "10",
            "reason": "x",
        },
        headers=_headers(key),
    ).json()
    response = client.post(
        "/transactions/capture",
        json={
            "authorization_id": verdict["authorization_id"],
            "source": "sdk",
            "amount": amount,
        },
        headers=_headers(key),
    )
    assert response.status_code == 422


# --- Settlement override is bounded by the authorized amount ---


def _authorize_ten(client: TestClient, key: str) -> str:
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
        headers=_headers(key),
    ).json()["authorization_id"]


def test_capture_rejects_settlement_over_buffer(
    client: TestClient, db_session: Session
) -> None:
    """Authorize $10, settle $13 (> 10 x 1.25 buffer) -> rejected."""
    _, _, _, key = _seed(db_session)
    auth_id = _authorize_ten(client, key)
    response = client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk", "amount": "13"},
        headers=_headers(key),
    )
    assert response.status_code == 422


def test_capture_allows_settlement_within_buffer(
    client: TestClient, db_session: Session
) -> None:
    """Authorize $10, settle $12 (<= 10 x 1.25 buffer) -> allowed."""
    _, _, _, key = _seed(db_session)
    auth_id = _authorize_ten(client, key)
    response = client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk", "amount": "12"},
        headers=_headers(key),
    )
    assert response.status_code == 200
    assert Decimal(response.json()["amount"]) == Decimal("12")


# --- DB CHECK constraints reject non-positive / two-sided rows ---


def test_db_rejects_negative_authorization_amount(db_session: Session) -> None:
    _, entity, agent, _ = _seed(db_session)
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            Authorization(
                company_id=agent.company_id,
                agent_id=agent.id,
                entity_id=entity.id,
                vendor="x",
                amount=Decimal("-5"),
                reason="direct write bypassing the schema",
                status=VerdictStatus.APPROVED,
            )
        )
        db_session.flush()


def test_db_rejects_negative_transaction_amount(db_session: Session) -> None:
    _, entity, agent, _ = _seed(db_session)
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            Transaction(
                company_id=agent.company_id,
                agent_id=agent.id,
                entity_id=entity.id,
                source=TransactionSource.SDK,
                vendor="x",
                paid_amount=Decimal("-5"),
                paid_currency="SGD",
                local_amount=Decimal("-5"),
                local_currency="SGD",
                transaction_date=datetime.now(UTC),
            )
        )
        db_session.flush()


def test_db_rejects_two_sided_journal_line(db_session: Session) -> None:
    _, entity, agent, _ = _seed(db_session)
    txn = Transaction(
        company_id=agent.company_id,
        agent_id=agent.id,
        entity_id=entity.id,
        source=TransactionSource.SDK,
        vendor="x",
        paid_amount=Decimal("10"),
        paid_currency="SGD",
        local_amount=Decimal("10"),
        local_currency="SGD",
        transaction_date=datetime.now(UTC),
    )
    db_session.add(txn)
    db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            JournalEntry(
                transaction_id=txn.id,
                account_name="Wallet",
                account_code="1010",
                debit=Decimal("5"),
                credit=Decimal("5"),
                ledger="sg",
                currency="SGD",  # set so the one-sided CHECK is what fails
            )
        )
        db_session.flush()
