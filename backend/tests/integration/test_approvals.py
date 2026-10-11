"""Approval queue: review/human_hold holds get approved (budget re-checked)."""

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
    ApiKey,
    ApiKeyMode,
    Company,
    Entity,
    Policy,
    PolicyRuleType,
    PolicyScopeType,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(db_session: Session, budget: str = "9000") -> str:
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
    db_session.add(
        Agent(
            company_id=company.id,
            entity_id=entity.id,
            name="Ops",
            slug="ops",
            glyph="x",
            role="Operations",
            monthly_budget=Decimal(budget),
        )
    )
    # A review-routing threshold: anything over 50 needs approval.
    db_session.add(
        Policy(
            company_id=company.id,
            name="review over 50",
            rule_type=PolicyRuleType.AMOUNT_THRESHOLD,
            config_json={"max_amount": "50", "action": "review"},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()
    return plaintext


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _authorize_for_review(client: TestClient, key: str, amount: str = "100") -> str:
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "x",
            "currency": "SGD",
            "amount": amount,
            "reason": "needs sign-off",
        },
        headers=_hdrs(key),
    ).json()
    assert verdict["status"] == "review"
    return verdict["authorization_id"]


def test_pending_holds_listed(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    auth_id = _authorize_for_review(client, key)

    listing = client.get("/approvals", headers=_hdrs(key)).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == auth_id
    assert listing["items"][0]["status"] == "review"


def test_approve_then_capture(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    auth_id = _authorize_for_review(client, key)

    # Capture is refused while pending.
    pre = client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk"},
        headers=_hdrs(key),
    )
    assert pre.status_code == 409

    approved = client.post(f"/approvals/{auth_id}/approve", headers=_hdrs(key))
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    captured = client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk"},
        headers=_hdrs(key),
    )
    assert captured.status_code == 200


def test_reject_blocks_capture(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    auth_id = _authorize_for_review(client, key)

    rejected = client.post(f"/approvals/{auth_id}/reject", headers=_hdrs(key))
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "blocked"

    captured = client.post(
        "/transactions/capture",
        json={"authorization_id": auth_id, "source": "sdk"},
        headers=_hdrs(key),
    )
    assert captured.status_code == 409


def test_approve_rechecks_budget(client: TestClient, db_session: Session) -> None:
    """Two $80 holds on a $100 budget: approving the second would bust it."""
    key = _seed(db_session, budget="100")
    first = _authorize_for_review(client, key, amount="80")
    second = _authorize_for_review(client, key, amount="80")

    ok = client.post(f"/approvals/{first}/approve", headers=_hdrs(key))
    assert ok.status_code == 200

    busted = client.post(f"/approvals/{second}/approve", headers=_hdrs(key))
    assert busted.status_code == 409
    assert "budget" in busted.json()["detail"].lower()


def test_approve_already_approved_conflicts(
    client: TestClient, db_session: Session
) -> None:
    key = _seed(db_session)
    auth_id = _authorize_for_review(client, key)

    first = client.post(f"/approvals/{auth_id}/approve", headers=_hdrs(key))
    assert first.status_code == 200
    # Second approve: no longer pending.
    again = client.post(f"/approvals/{auth_id}/approve", headers=_hdrs(key))
    assert again.status_code == 409


def test_approve_unknown_is_404(client: TestClient, db_session: Session) -> None:
    key = _seed(db_session)
    resp = client.post(f"/approvals/{uuid.uuid4()}/approve", headers=_hdrs(key))
    assert resp.status_code == 404
