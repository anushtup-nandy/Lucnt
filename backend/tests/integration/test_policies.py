"""Integration tests for the Phase 3 policy engine wired into `authorize`.

Policies are inserted directly against the test's rolled-back session; the
verdict + machine-readable `code` come back through the real HTTP endpoint.
"""

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
    Company,
    Entity,
    Policy,
    PolicyRuleType,
    PolicyScopeType,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient wired to the test's own rolled-back `db_session`."""
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(
    db_session: Session, *, monthly_budget: Decimal = Decimal("100000")
) -> tuple[Company, Agent, str]:
    """A company (+ live key), an SG entity, and one agent with a large budget."""
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
        monthly_budget=monthly_budget,
    )
    db_session.add(agent)
    db_session.flush()
    return company, agent, plaintext


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _authorize(client: TestClient, key: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "agent": "ops",
        "entity": "sg",
        "vendor": "openai",
        "amount": "10",
        "currency": "SGD",
        "reason": "test spend",
    }
    body.update(overrides)
    return client.post(
        "/transactions/authorize", json=body, headers=_headers(key)
    ).json()


def test_merchant_blocklist_blocks_and_sets_code(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    db_session.add(
        Policy(
            company_id=company.id,
            name="no crypto",
            rule_type=PolicyRuleType.MERCHANT_LIST,
            config_json={"mode": "block", "vendors": ["coinbase"]},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()

    verdict = _authorize(client, key, vendor="coinbase")
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "merchant_blocked"


def test_amount_threshold_routes_to_review(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    db_session.add(
        Policy(
            company_id=company.id,
            name="review large",
            rule_type=PolicyRuleType.AMOUNT_THRESHOLD,
            config_json={"max_amount": "50", "action": "review"},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()

    verdict = _authorize(client, key, amount="500")
    assert verdict["status"] == "review"
    assert verdict["code"] == "amount_over_threshold"


def test_amount_threshold_human_hold(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    db_session.add(
        Policy(
            company_id=company.id,
            name="hold huge",
            rule_type=PolicyRuleType.AMOUNT_THRESHOLD,
            config_json={"max_amount": "50", "action": "human_hold"},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()

    assert _authorize(client, key, amount="500")["status"] == "human_hold"


def test_over_budget_returns_budget_exhausted_code(
    client: TestClient, db_session: Session
) -> None:
    _company, _agent, key = _seed(db_session, monthly_budget=Decimal("100"))
    verdict = _authorize(client, key, amount="999")
    assert verdict["status"] == "blocked"
    assert verdict["code"] == "budget_exhausted"


def test_velocity_blocks_after_cap_reached(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    db_session.add(
        Policy(
            company_id=company.id,
            name="one per hour",
            rule_type=PolicyRuleType.VELOCITY,
            config_json={"max_count": 1, "window": "hour"},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()

    # First spend: no prior transactions -> approved -> capture commits one.
    first = _authorize(client, key, amount="10")
    assert first["status"] == "approved"
    captured = client.post(
        "/transactions/capture",
        json={"authorization_id": first["authorization_id"], "source": "sdk"},
        headers=_headers(key),
    )
    assert captured.status_code == 200

    # Second spend within the hour: one prior spend reaches the cap of 1.
    second = _authorize(client, key, amount="10")
    assert second["status"] == "blocked"
    assert second["code"] == "velocity_exceeded"


def test_agent_scoped_policy_only_applies_to_that_agent(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    other = Agent(
        company_id=company.id,
        entity_id=_agent.entity_id,
        name="Other Agent",
        slug="other",
        glyph="y",
        role="Other",
        monthly_budget=Decimal("100000"),
    )
    db_session.add(other)
    db_session.flush()
    # Block coinbase, but only for the OTHER agent.
    db_session.add(
        Policy(
            company_id=company.id,
            name="other cannot crypto",
            rule_type=PolicyRuleType.MERCHANT_LIST,
            config_json={"mode": "block", "vendors": ["coinbase"]},
            scope_type=PolicyScopeType.AGENT,
            scope_id=other.id,
        )
    )
    db_session.flush()

    # `ops` is unaffected by a policy scoped to `other`.
    assert _authorize(client, key, vendor="coinbase")["status"] == "approved"
    # `other` is blocked.
    blocked = _authorize(client, key, agent="other", vendor="coinbase")
    assert blocked["status"] == "blocked"


def test_block_count_increments_on_block(
    client: TestClient, db_session: Session
) -> None:
    company, _agent, key = _seed(db_session)
    policy = Policy(
        company_id=company.id,
        name="no crypto",
        rule_type=PolicyRuleType.MERCHANT_LIST,
        config_json={"mode": "block", "vendors": ["coinbase"]},
        scope_type=PolicyScopeType.COMPANY,
    )
    db_session.add(policy)
    db_session.flush()

    _authorize(client, key, vendor="coinbase")

    refreshed = db_session.execute(
        select(Policy).where(Policy.id == policy.id)
    ).scalar_one()
    assert refreshed.hit_count == 1
    assert refreshed.block_count == 1
