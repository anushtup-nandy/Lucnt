"""Integration tests for the Phase 3 budgets and policies routers.

NOTE: the seed helpers below mirror those in `test_transactions.py` /
`test_policies.py`; consolidating them into `conftest.py` is a tracked cleanup
(kept local here to avoid churning the already-green sibling test files).
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
    ApiKey,
    ApiKeyMode,
    Company,
    Entity,
    Workflow,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient wired to the test's own rolled-back `db_session`."""
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _company(db: Session) -> tuple[Company, str]:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(company)
    db.flush()
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="test key",
            mode=ApiKeyMode.LIVE,
        )
    )
    db.flush()
    return company, plaintext


def _entity(db: Session, company: Company) -> Entity:
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
    db.add(entity)
    db.flush()
    return entity


def _agent(
    db: Session,
    company: Company,
    entity: Entity,
    slug: str = "ops",
    monthly_budget: Decimal = Decimal("1000"),
    spent: Decimal = Decimal("0"),
) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name=f"Agent {slug}",
        slug=slug,
        glyph="x",
        role="Operations",
        monthly_budget=monthly_budget,
        spent_ytd=spent,
    )
    db.add(agent)
    db.flush()
    return agent


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ------------------------------ budgets ------------------------------


def test_get_budgets_reports_agent_utilisation(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    _agent(db_session, company, _entity(db_session, company), spent=Decimal("250"))

    body = client.get("/budgets", headers=_headers(key)).json()
    assert len(body["agents"]) == 1
    agent = body["agents"][0]
    assert Decimal(agent["remaining"]) == Decimal("750")
    assert Decimal(agent["pct_used"]) == Decimal("25.0")
    assert agent["status"] == "ok"


def test_budget_status_warning_then_exhausted(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    _agent(db_session, company, entity, slug="warn", spent=Decimal("900"))
    _agent(db_session, company, entity, slug="over", spent=Decimal("1000"))

    agents = client.get("/budgets", headers=_headers(key)).json()["agents"]
    by_slug = {a["slug"]: a for a in agents}
    assert by_slug["warn"]["status"] == "warning"
    assert by_slug["over"]["status"] == "exhausted"
    assert Decimal(by_slug["over"]["remaining"]) == Decimal("0")


def test_patch_agent_budget_updates_cap(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    agent = _agent(db_session, company, _entity(db_session, company))

    resp = client.patch(
        f"/budgets/{agent.id}",
        json={"monthly_budget": "5000"},
        headers=_headers(key),
    )
    assert resp.status_code == 200
    assert Decimal(resp.json()["monthly_budget"]) == Decimal("5000")

    db_session.refresh(agent)
    assert agent.monthly_budget == Decimal("5000.0000")


def test_patch_unknown_agent_budget_404(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    resp = client.patch(
        f"/budgets/{uuid.uuid4()}",
        json={"monthly_budget": "1"},
        headers=_headers(key),
    )
    assert resp.status_code == 404


def test_workflow_rollup_sums_member_spend(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    a1 = _agent(db_session, company, entity, slug="a1", spent=Decimal("300"))
    a2 = _agent(db_session, company, entity, slug="a2", spent=Decimal("400"))
    workflow = Workflow(
        company_id=company.id, name="Q3 offsite", budget=Decimal("1000")
    )
    workflow.agents = [a1, a2]
    db_session.add(workflow)
    db_session.flush()

    body = client.get("/budgets", headers=_headers(key)).json()
    assert len(body["workflows"]) == 1
    wf = body["workflows"][0]
    assert Decimal(wf["spent"]) == Decimal("700")
    assert Decimal(wf["remaining"]) == Decimal("300")
    assert set(wf["agent_ids"]) == {str(a1.id), str(a2.id)}


# ------------------------------ policies ------------------------------


def test_create_policy_validates_and_echoes_config(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    resp = client.post(
        "/policies",
        json={
            "name": "no crypto",
            "rule_type": "merchant_list",
            "config": {"mode": "block", "vendors": ["coinbase"]},
        },
        headers=_headers(key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["rule_type"] == "merchant_list"
    assert body["config"] == {"mode": "block", "vendors": ["coinbase"]}
    assert body["scope_type"] == "company"


def test_create_policy_rejects_invalid_config(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    resp = client.post(
        "/policies",
        json={
            "name": "broken velocity",
            "rule_type": "velocity",
            "config": {"window": "hour"},  # missing max_count
        },
        headers=_headers(key),
    )
    assert resp.status_code == 400


def test_create_agent_scoped_policy_requires_scope_id(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    resp = client.post(
        "/policies",
        json={
            "name": "scoped",
            "rule_type": "amount_threshold",
            "config": {"max_amount": "100"},
            "scope_type": "agent",
        },
        headers=_headers(key),
    )
    assert resp.status_code == 400


def test_list_policies_scoped_to_company(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    for name in ("p1", "p2"):
        client.post(
            "/policies",
            json={
                "name": name,
                "rule_type": "amount_threshold",
                "config": {"max_amount": "100"},
            },
            headers=_headers(key),
        )
    body = client.get("/policies", headers=_headers(key)).json()
    assert body["total"] == 2


def test_patch_policy_toggles_active(
    client: TestClient, db_session: Session
) -> None:
    _company_row, key = _company(db_session)
    created = client.post(
        "/policies",
        json={
            "name": "toggle me",
            "rule_type": "amount_threshold",
            "config": {"max_amount": "100"},
        },
        headers=_headers(key),
    ).json()

    patched = client.patch(
        f"/policies/{created['id']}",
        json={"active": False},
        headers=_headers(key),
    )
    assert patched.status_code == 200
    assert patched.json()["active"] is False


def test_created_policy_blocks_authorize_end_to_end(
    client: TestClient, db_session: Session
) -> None:
    """A policy created via the API is enforced at authorize (full loop)."""
    company, key = _company(db_session)
    _agent(
        db_session,
        company,
        _entity(db_session, company),
        monthly_budget=Decimal("1000000"),
    )
    client.post(
        "/policies",
        json={
            "name": "no crypto",
            "rule_type": "merchant_list",
            "config": {"mode": "block", "vendors": ["coinbase"]},
        },
        headers=_headers(key),
    )

    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": "coinbase",
            "amount": "10",
            "currency": "SGD",
            "reason": "buy crypto",
        },
        headers=_headers(key),
    ).json()
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "merchant_blocked"
