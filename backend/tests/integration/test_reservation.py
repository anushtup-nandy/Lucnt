"""Budget reservation, hold-aware velocity, and concurrency (T0-1, T0-5, T0-8).

The moat guarantee — "block *before* money moves" — must hold under a hostile or
buggy caller that authorizes many times before (or without) capturing, and under
genuine concurrency. These tests exercise that the way the old code could not.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from lucnt_api.database import SessionLocal, get_db
from lucnt_api.main import app
from lucnt_api.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    Authorization,
    Company,
    Entity,
    Policy,
    PolicyRuleType,
    PolicyScopeType,
    Transaction,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _seed(
    db_session: Session, budget: str = "100"
) -> tuple[Company, Entity, Agent, str]:
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
        monthly_budget=Decimal(budget),
    )
    db_session.add(agent)
    db_session.flush()
    return company, entity, agent, plaintext


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _authorize(client: TestClient, key: str, amount: str, vendor: str = "x") -> dict:
    return client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": vendor,
            "amount": amount,
            "currency": "SGD",
            "reason": "x",
        },
        headers=_headers(key),
    ).json()


# --- T0-1: approved holds reserve budget even before any capture ---


def test_holds_reserve_budget_before_capture(
    client: TestClient, db_session: Session
) -> None:
    """Two $60 authorizes on a $100 budget: the second is blocked by the first's
    outstanding hold — with no capture in between (the old N-before-capture hole)."""
    _, _, _, key = _seed(db_session, budget="100")

    first = _authorize(client, key, "60")
    assert first["status"] == "approved"

    second = _authorize(client, key, "60")
    assert second["status"] == "blocked"
    assert second["code"] == "budget_exhausted"


def test_committed_and_reserved_combine(
    client: TestClient, db_session: Session
) -> None:
    """After capture the hold becomes committed spend (from the ledger), still
    counted; a captured hold stops double-reserving."""
    _, _, _, key = _seed(db_session, budget="100")

    first = _authorize(client, key, "60")
    captured = client.post(
        "/transactions/capture",
        json={"authorization_id": first["authorization_id"], "source": "sdk"},
        headers=_headers(key),
    )
    assert captured.status_code == 200

    # committed 60 + 60 = 120 > 100 -> blocked
    assert _authorize(client, key, "60")["status"] == "blocked"
    # committed 60 + 40 = 100 <= 100 -> approved (proves the captured hold is not
    # ALSO still reserved, which would wrongly block at 60 + 60 + 40)
    assert _authorize(client, key, "40")["status"] == "approved"


# --- T0-8: velocity counts uncaptured approved holds, not just committed ---


def test_velocity_counts_uncaptured_holds(
    client: TestClient, db_session: Session
) -> None:
    company, _, _, key = _seed(db_session, budget="1000000")
    db_session.add(
        Policy(
            company_id=company.id,
            name="max 2 per hour",
            rule_type=PolicyRuleType.VELOCITY,
            config_json={"max_count": 2, "window": "hour"},
            scope_type=PolicyScopeType.COMPANY,
        )
    )
    db_session.flush()

    assert _authorize(client, key, "1")["status"] == "approved"
    assert _authorize(client, key, "1")["status"] == "approved"
    third = _authorize(client, key, "1")
    assert third["status"] == "blocked"
    assert third["code"] == "velocity_exceeded"


# --- T0-1: the row lock serializes genuinely concurrent authorizes ---


def test_concurrent_authorizes_cannot_bust_budget() -> None:
    """Two parallel $60 authorizes on a $100 budget: the FOR UPDATE agent lock
    lets exactly one through. Runs against the real DB (no rollback fixture) so
    the two requests hold separate transactions, as production would."""
    app.dependency_overrides.pop(get_db, None)  # ensure real sessions

    key = generate_api_key(ApiKeyMode.LIVE)
    setup = SessionLocal()
    try:
        company = Company(name="Race", slug=f"race-{uuid.uuid4().hex[:8]}")
        setup.add(company)
        setup.flush()
        company_id = company.id
        setup.add(
            ApiKey(
                company_id=company_id,
                key_prefix=key[:12],
                key_hash=hash_api_key(key),
                name="k",
                mode=ApiKeyMode.LIVE,
            )
        )
        entity = Entity(
            company_id=company_id,
            name="Race SG",
            code="sg",
            full_name="Race SG",
            currency="SGD",
            accounting_std="SFRS",
            tax_regime="SG-GST",
            timezone="Asia/Singapore",
        )
        setup.add(entity)
        setup.flush()
        setup.add(
            Agent(
                company_id=company_id,
                entity_id=entity.id,
                name="Race",
                slug="ops",
                glyph="x",
                role="r",
                monthly_budget=Decimal("100"),
            )
        )
        setup.commit()
    except Exception:
        setup.rollback()
        setup.close()
        raise

    results: list[str] = []
    results_lock = threading.Lock()
    start = threading.Barrier(2)

    def worker() -> None:
        thread_client = TestClient(app)
        start.wait()
        body = thread_client.post(
            "/transactions/authorize",
            json={
                "agent": "ops",
                "entity": "sg",
                "vendor": "x",
                "amount": "60",
                "currency": "SGD",
                "reason": "race",
            },
            headers=_headers(key),
        ).json()
        with results_lock:
            results.append(body["status"])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        # Exactly one approved, one blocked — never two approvals busting budget.
        assert sorted(results) == ["approved", "blocked"], results
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.execute(
                delete(Transaction).where(Transaction.company_id == company_id)
            )
            cleanup.execute(
                delete(Authorization).where(Authorization.company_id == company_id)
            )
            cleanup.execute(delete(Agent).where(Agent.company_id == company_id))
            cleanup.execute(delete(ApiKey).where(ApiKey.company_id == company_id))
            cleanup.execute(delete(Entity).where(Entity.company_id == company_id))
            cleanup.execute(delete(Company).where(Company.id == company_id))
            cleanup.commit()
        finally:
            cleanup.close()
            setup.close()
