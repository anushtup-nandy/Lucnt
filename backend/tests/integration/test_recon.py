"""Integration tests for the Phase 4 reconciliation (recovery) layer.

Transaction rows are inserted directly so the tests control `transaction_date`
(the duplicate/subscription detectors are time-sensitive). The per-file
company/agent/entity helpers mirror the other integration files (a conftest
consolidation is a deferred cleanup — see logs/log_05070726.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
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
    Company,
    Entity,
    Transaction,
    TransactionSource,
)
from lucnt_api.security import generate_api_key, hash_api_key

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


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


def _make_entity(db_session: Session, company: Company) -> Entity:
    entity = Entity(
        company_id=company.id,
        name="LUCNT US",
        code="us",
        full_name="LUCNT US Inc",
        currency="USD",
        accounting_std="US-GAAP",
        tax_regime="US-SALES",
        timezone="America/New_York",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _make_agent(
    db_session: Session, company: Company, entity: Entity, slug: str = "ops"
) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops Agent",
        slug=slug,
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("100000"),
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def _make_transaction(
    db_session: Session,
    company: Company,
    agent: Agent,
    entity: Entity,
    *,
    vendor: str,
    amount: str,
    when: datetime,
    currency: str = "USD",
) -> Transaction:
    txn = Transaction(
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        source=TransactionSource.SDK,
        vendor=vendor,
        paid_amount=Decimal(amount),
        paid_currency=currency,
        local_amount=Decimal(amount),
        local_currency=currency,
        transaction_date=when,
    )
    db_session.add(txn)
    db_session.flush()
    return txn


def _auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _setup(db_session: Session) -> tuple[Company, str, Agent, Entity]:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity)
    return company, key, agent, entity


# --- scan / detectors ---


def test_scan_detects_duplicate(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    _make_transaction(
        db_session, company, agent, entity, vendor="stripe", amount="49.00", when=_NOW
    )
    _make_transaction(
        db_session,
        company,
        agent,
        entity,
        vendor="stripe",
        amount="49.00",
        when=_NOW + timedelta(hours=1),
    )

    scan = client.post("/recon/scan", headers=_auth_headers(key)).json()
    assert scan["scanned"] == 2
    assert scan["created"] == 1
    assert scan["by_type"] == {"duplicate": 1}

    inbox = client.get("/recon", headers=_auth_headers(key)).json()
    assert inbox["total"] == 1
    exc = inbox["items"][0]
    assert exc["exception_type"] == "duplicate"
    assert exc["status"] == "open"
    assert Decimal(exc["amount_at_stake"]) == Decimal("49.00")
    # the recovery agent drafted a plan into evidence
    assert exc["evidence"]["recovery_plan"]["method"] == "vendor_email"


def test_scan_is_idempotent(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    _make_transaction(
        db_session, company, agent, entity, vendor="stripe", amount="49", when=_NOW
    )
    _make_transaction(
        db_session,
        company,
        agent,
        entity,
        vendor="stripe",
        amount="49",
        when=_NOW + timedelta(hours=1),
    )

    first = client.post("/recon/scan", headers=_auth_headers(key)).json()
    second = client.post("/recon/scan", headers=_auth_headers(key)).json()
    assert first["created"] == 1
    assert second["created"] == 0
    assert client.get("/recon", headers=_auth_headers(key)).json()["total"] == 1


def test_scan_detects_overcharge(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    for i, amount in enumerate(["10", "10", "10", "100"]):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="aws",
            amount=amount,
            when=_NOW + timedelta(days=2 * i),  # >24h apart: no duplicate flag
        )

    scan = client.post("/recon/scan", headers=_auth_headers(key)).json()
    assert scan["by_type"].get("overcharge") == 1

    items = client.get(
        "/recon?exception_type=overcharge", headers=_auth_headers(key)
    ).json()["items"]
    assert len(items) == 1
    assert Decimal(items[0]["amount_at_stake"]) == Decimal("90")  # 100 - median 10


def test_scan_detects_unused_subscription(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    for i in range(3):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="notion",
            amount="8.00",
            when=_NOW + timedelta(days=30 * i),
        )

    scan = client.post("/recon/scan", headers=_auth_headers(key)).json()
    assert scan["by_type"].get("unused_subscription") == 1
    items = client.get(
        "/recon?exception_type=unused_subscription", headers=_auth_headers(key)
    ).json()["items"]
    assert items[0]["evidence"]["recovery_plan"]["escalate_to_human"] is True


def test_scan_is_scoped_to_company(client: TestClient, db_session: Session) -> None:
    company_a, key_a, agent_a, entity_a = _setup(db_session)
    _make_transaction(
        db_session, company_a, agent_a, entity_a, vendor="s", amount="5", when=_NOW
    )
    _make_transaction(
        db_session,
        company_a,
        agent_a,
        entity_a,
        vendor="s",
        amount="5",
        when=_NOW + timedelta(hours=1),
    )
    _, key_b = _make_company_with_key(db_session)

    client.post("/recon/scan", headers=_auth_headers(key_a))
    assert client.get("/recon", headers=_auth_headers(key_b)).json()["total"] == 0
    assert client.get("/recon", headers=_auth_headers(key_a)).json()["total"] == 1


# --- inbox summary + ordering ---


def test_inbox_summary_and_ordering(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    # a $90 overcharge and a $5 duplicate -> ordered by $ at stake, summed
    for i, amount in enumerate(["10", "10", "10", "100"]):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="aws",
            amount=amount,
            when=_NOW + timedelta(days=2 * i),  # >24h apart: no duplicate flag
        )
    _make_transaction(
        db_session, company, agent, entity, vendor="s", amount="5", when=_NOW
    )
    _make_transaction(
        db_session,
        company,
        agent,
        entity,
        vendor="s",
        amount="5",
        when=_NOW + timedelta(hours=1),
    )
    client.post("/recon/scan", headers=_auth_headers(key))

    inbox = client.get("/recon", headers=_auth_headers(key)).json()
    assert inbox["open_count"] == 2
    assert Decimal(inbox["total_at_stake"]) == Decimal("95")  # 90 + 5
    # highest $ at stake first
    assert inbox["items"][0]["exception_type"] == "overcharge"


# --- failed delivery (SDK hook path) ---


def test_failed_delivery_creates_exception(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    txn = _make_transaction(
        db_session, company, agent, entity, vendor="dataco", amount="50", when=_NOW
    )

    resp = client.post(
        "/recon/failed-delivery",
        json={"transaction_id": str(txn.id), "reason": "delivered file was corrupt"},
        headers=_auth_headers(key),
    )
    assert resp.status_code == 201
    exc = resp.json()
    assert exc["exception_type"] == "failed_delivery"
    assert Decimal(exc["amount_at_stake"]) == Decimal("50")
    assert exc["evidence"]["reason"] == "delivered file was corrupt"
    assert exc["evidence"]["recovery_plan"]["method"] == "refund_request"


def test_failed_delivery_is_idempotent(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    txn = _make_transaction(
        db_session, company, agent, entity, vendor="dataco", amount="50", when=_NOW
    )
    body = {"transaction_id": str(txn.id), "reason": "corrupt"}
    first = client.post(
        "/recon/failed-delivery", json=body, headers=_auth_headers(key)
    ).json()
    second = client.post(
        "/recon/failed-delivery", json=body, headers=_auth_headers(key)
    ).json()
    assert first["id"] == second["id"]
    assert client.get("/recon", headers=_auth_headers(key)).json()["total"] == 1


def test_failed_delivery_unknown_transaction(
    client: TestClient, db_session: Session
) -> None:
    _, key, _, _ = _setup(db_session)
    resp = client.post(
        "/recon/failed-delivery",
        json={"transaction_id": str(uuid.uuid4()), "reason": "x"},
        headers=_auth_headers(key),
    )
    assert resp.status_code == 404


def test_failed_delivery_custom_amount_at_stake(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    txn = _make_transaction(
        db_session, company, agent, entity, vendor="dataco", amount="50", when=_NOW
    )
    exc = client.post(
        "/recon/failed-delivery",
        json={
            "transaction_id": str(txn.id),
            "reason": "partial",
            "amount_at_stake": "20",
        },
        headers=_auth_headers(key),
    ).json()
    assert Decimal(exc["amount_at_stake"]) == Decimal("20")


# --- resolve ---


def test_resolve_transitions_and_updates_summary(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    txn = _make_transaction(
        db_session, company, agent, entity, vendor="dataco", amount="50", when=_NOW
    )
    exc = client.post(
        "/recon/failed-delivery",
        json={"transaction_id": str(txn.id), "reason": "corrupt"},
        headers=_auth_headers(key),
    ).json()

    resolved = client.post(
        f"/recon/{exc['id']}/resolve",
        json={"action": "resolve", "fix_notes": "vendor refunded"},
        headers=_auth_headers(key),
    ).json()
    assert resolved["status"] == "resolved"
    assert resolved["fix_notes"] == "vendor refunded"
    assert resolved["resolved_at"] is not None

    # resolved exception leaves the active summary
    inbox = client.get("/recon", headers=_auth_headers(key)).json()
    assert inbox["open_count"] == 0
    assert Decimal(inbox["total_at_stake"]) == Decimal("0")


def test_resolve_rejects_already_terminal(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    txn = _make_transaction(
        db_session, company, agent, entity, vendor="dataco", amount="50", when=_NOW
    )
    exc = client.post(
        "/recon/failed-delivery",
        json={"transaction_id": str(txn.id), "reason": "corrupt"},
        headers=_auth_headers(key),
    ).json()
    client.post(
        f"/recon/{exc['id']}/resolve",
        json={"action": "dismiss"},
        headers=_auth_headers(key),
    )
    again = client.post(
        f"/recon/{exc['id']}/resolve",
        json={"action": "resolve"},
        headers=_auth_headers(key),
    )
    assert again.status_code == 409


def test_get_exception_not_found(client: TestClient, db_session: Session) -> None:
    _, key, _, _ = _setup(db_session)
    resp = client.get(f"/recon/{uuid.uuid4()}", headers=_auth_headers(key))
    assert resp.status_code == 404


def test_resolved_ledger_fact_is_not_reflagged(
    client: TestClient, db_session: Session
) -> None:
    """A detected ledger fact is immutable: once an exception exists for a
    (transaction, type) — even resolved/dismissed — a later scan must not
    re-flag it (the unique constraint holds across all statuses)."""
    company, key, agent, entity = _setup(db_session)
    _make_transaction(
        db_session, company, agent, entity, vendor="s", amount="5", when=_NOW
    )
    _make_transaction(
        db_session,
        company,
        agent,
        entity,
        vendor="s",
        amount="5",
        when=_NOW + timedelta(hours=1),
    )
    client.post("/recon/scan", headers=_auth_headers(key))
    exc = client.get("/recon", headers=_auth_headers(key)).json()["items"][0]
    client.post(
        f"/recon/{exc['id']}/resolve",
        json={"action": "resolve", "fix_notes": "vendor refunded"},
        headers=_auth_headers(key),
    )
    # re-scan: the resolved duplicate (its ledger rows still exist) is NOT
    # re-flagged — no nagging on an already-handled fact.
    rescan = client.post("/recon/scan", headers=_auth_headers(key)).json()
    assert rescan["created"] == 0
    assert client.get("/recon", headers=_auth_headers(key)).json()["total"] == 1
