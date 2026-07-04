"""Integration tests for the Phase 1 (A) authorize -> capture spine."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt.database import get_db
from lucnt.main import app
from lucnt.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    Company,
    Entity,
    JournalEntry,
    TaxRecord,
)
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


def _make_entity(
    db_session: Session, company: Company, code: str = "sg", currency: str = "SGD"
) -> Entity:
    entity = Entity(
        company_id=company.id,
        name=f"LUCNT {code.upper()}",
        code=code,
        full_name=f"LUCNT {code.upper()} Pte Ltd",
        currency=currency,
        accounting_std="SFRS",
        tax_regime=f"{code.upper()}-GST",
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _make_agent(
    db_session: Session,
    company: Company,
    entity: Entity,
    slug: str = "ops",
    monthly_budget: Decimal = Decimal("9000"),
) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops Agent",
        slug=slug,
        glyph="x",
        role="Operations",
        monthly_budget=monthly_budget,
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def _auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_authorize_approves_within_budget(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    _make_agent(db_session, company, _make_entity(db_session, company))

    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "singapore_airlines",
            "amount": "1240",
            "currency": "SGD",
            "reason": "SIN->SFO, premium fare",
        },
        headers=_auth_headers(key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["reason"] is None
    assert uuid.UUID(body["authorization_id"])


def test_authorize_blocks_over_budget(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    _make_agent(
        db_session,
        company,
        _make_entity(db_session, company),
        monthly_budget=Decimal("100"),
    )

    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "999",
            "reason": "deliberately over budget",
        },
        headers=_auth_headers(key),
    )
    body = response.json()
    assert body["status"] == "blocked"
    assert "exceeds monthly budget" in (body["reason"] or "")


def test_authorize_rejects_unknown_agent(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    _make_entity(db_session, company)

    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ghost",
            "entity": "sg",
            "vendor": "x",
            "amount": "1",
            "reason": "x",
        },
        headers=_auth_headers(key),
    )
    assert response.status_code == 404


def test_authorize_rejects_missing_api_key(client: TestClient) -> None:
    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "1",
            "reason": "x",
        },
    )
    assert response.status_code == 401


def test_authorize_rejects_unknown_api_key(client: TestClient) -> None:
    response = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "1",
            "reason": "x",
        },
        headers=_auth_headers("lucnt_live_not_a_real_key"),
    )
    assert response.status_code == 401


def test_capture_commits_and_updates_agent_spend(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    agent = _make_agent(db_session, company, _make_entity(db_session, company))

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "singapore_airlines",
            "amount": "1240",
            "currency": "SGD",
            "reason": "flight",
        },
        headers=_auth_headers(key),
    ).json()

    response = client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_auth_headers(key),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["authorization_id"] == verdict["authorization_id"]
    assert Decimal(body["amount"]) == Decimal("1240")

    db_session.refresh(agent)
    assert agent.spent_ytd == Decimal("1240.0000")
    assert agent.spent_today == Decimal("1240.0000")


def test_capture_books_reverse_charge_journal_and_tax(
    client: TestClient, db_session: Session
) -> None:
    """An imported service (USD paid by SGD entity) books RCM + balanced journal."""
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company, code="sg", currency="SGD")
    _make_agent(db_session, company, entity)

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "openai",
            "amount": "100",
            "currency": "USD",
            "reason": "token batch",
            "model": "gpt-5",
            "tokens": 128000,
        },
        headers=_auth_headers(key),
    ).json()
    body = client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_auth_headers(key),
    ).json()

    tax = body["tax"]
    assert tax["tax_type"] == "GST_RCM"
    assert tax["reverse_charge"] is True
    assert tax["self_invoice_generated"] is True
    assert Decimal(tax["rate"]) == Decimal("0.09")
    assert Decimal(tax["tax_amount"]) == Decimal("9.00")  # 9% on top of 100

    journal = body["journal"]
    assert len(journal) == 4
    total_debit = sum(Decimal(line["debit"]) for line in journal)
    total_credit = sum(Decimal(line["credit"]) for line in journal)
    assert total_debit == total_credit == Decimal("109.00")

    # Persisted, not just returned.
    tx_id = uuid.UUID(body["id"])
    persisted_lines = (
        db_session.execute(
            select(JournalEntry).where(JournalEntry.transaction_id == tx_id)
        )
        .scalars()
        .all()
    )
    assert len(persisted_lines) == 4
    tax_rows = (
        db_session.execute(
            select(TaxRecord).where(TaxRecord.transaction_id == tx_id)
        )
        .scalars()
        .all()
    )
    assert len(tax_rows) == 1
    assert tax_rows[0].self_invoice_generated is True


def test_capture_books_domestic_gst(
    client: TestClient, db_session: Session
) -> None:
    """A domestic service (INR paid by an INR entity) extracts embedded GST, no RCM."""
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company, code="in", currency="INR")
    _make_agent(db_session, company, entity)

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "in",
            "vendor": "local_saas",
            "amount": "118",
            "currency": "INR",
            "reason": "domestic subscription",
        },
        headers=_auth_headers(key),
    ).json()
    body = client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_auth_headers(key),
    ).json()

    tax = body["tax"]
    assert tax["tax_type"] == "GST"
    assert tax["reverse_charge"] is False
    assert tax["self_invoice_generated"] is False
    assert Decimal(tax["tax_amount"]) == Decimal("18.00")  # 118 * 0.18 / 1.18

    journal = body["journal"]
    assert len(journal) == 3
    total_debit = sum(Decimal(line["debit"]) for line in journal)
    total_credit = sum(Decimal(line["credit"]) for line in journal)
    assert total_debit == total_credit == Decimal("118")


def test_capture_rejects_double_capture(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    _make_agent(db_session, company, _make_entity(db_session, company))

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "10",
            "reason": "x",
        },
        headers=_auth_headers(key),
    ).json()
    capture_body = {"authorization_id": verdict["authorization_id"], "source": "sdk"}

    first = client.post(
        "/transactions/capture", json=capture_body, headers=_auth_headers(key)
    )
    assert first.status_code == 200

    second = client.post(
        "/transactions/capture", json=capture_body, headers=_auth_headers(key)
    )
    assert second.status_code == 409


def test_capture_rejects_blocked_authorization(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    _make_agent(
        db_session,
        company,
        _make_entity(db_session, company),
        monthly_budget=Decimal("1"),
    )

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "999",
            "reason": "x",
        },
        headers=_auth_headers(key),
    ).json()
    assert verdict["status"] == "blocked"

    response = client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_auth_headers(key),
    )
    assert response.status_code == 409


def test_capture_rejects_non_sdk_source(
    client: TestClient, db_session: Session
) -> None:
    _, key = _make_company_with_key(db_session)

    response = client.post(
        "/transactions/capture",
        json={"authorization_id": str(uuid.uuid4()), "source": "card"},
        headers=_auth_headers(key),
    )
    assert response.status_code == 400


def test_list_transactions_is_scoped_to_company(
    client: TestClient, db_session: Session
) -> None:
    company_a, key_a = _make_company_with_key(db_session)
    _make_agent(db_session, company_a, _make_entity(db_session, company_a))

    _, key_b = _make_company_with_key(db_session)

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "amount": "10",
            "reason": "x",
        },
        headers=_auth_headers(key_a),
    ).json()
    client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_auth_headers(key_a),
    )

    other_company_view = client.get("/transactions", headers=_auth_headers(key_b))
    assert other_company_view.json()["total"] == 0

    own_company_view = client.get("/transactions", headers=_auth_headers(key_a))
    assert own_company_view.json()["total"] == 1


def test_get_transaction_detail_not_found(
    client: TestClient, db_session: Session
) -> None:
    _, key = _make_company_with_key(db_session)
    response = client.get(f"/transactions/{uuid.uuid4()}", headers=_auth_headers(key))
    assert response.status_code == 404
