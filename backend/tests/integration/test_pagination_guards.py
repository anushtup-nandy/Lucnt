"""List-endpoint bounds and the batched spend aggregate (T3-3).

`GET /budgets` and `GET /approvals` previously returned every matching row, and
`/budgets` cost two spend queries *per agent* plus one lazy-load per workflow.
Adding a bound is only safe if two properties hold, which is what most of these
tests pin:

* **A rollup must not silently shrink.** Workflow spend sums *all* member agents,
  so paginating the `agents` array must not change a workflow's total — a budget
  that under-reports because a member fell off page 1 is worse than a slow one.
* **The batched aggregate must equal the per-agent form** it replaced, including
  reversal signs and the today-window, or the numbers move under a perf fix.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
    Authorization,
    Company,
    Entity,
    Transaction,
    TransactionSource,
    VerdictStatus,
    Workflow,
)
from lucnt_api.security import generate_api_key, hash_api_key
from lucnt_api.services.spend_window import (
    company_period_committed_spend,
    now_in_entity_tz,
    period_committed_spend,
    period_starts,
)


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


def _entity(db: Session, company: Company, code: str = "sg") -> Entity:
    entity = Entity(
        company_id=company.id,
        name=f"LUCNT {code.upper()}",
        code=code,
        full_name=f"LUCNT {code.upper()} Pte Ltd",
        currency="SGD",
        accounting_std="SFRS",
        tax_regime="SG-GST",
        timezone="Asia/Singapore",
    )
    db.add(entity)
    db.flush()
    return entity


def _agent(db: Session, company: Company, entity: Entity, slug: str) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name=f"Agent {slug}",
        slug=slug,
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("1000"),
    )
    db.add(agent)
    db.flush()
    return agent


def _spend(
    db: Session,
    company: Company,
    entity: Entity,
    agent: Agent,
    amount: Decimal,
    *,
    when: datetime | None = None,
    reverses: uuid.UUID | None = None,
) -> Transaction:
    """Book a committed transaction; budget spend derives from the ledger (T2-3)."""
    txn = Transaction(
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        source=TransactionSource.SDK,
        vendor="aws",
        paid_amount=amount,
        paid_currency=entity.currency,
        local_amount=amount,
        local_currency=entity.currency,
        transaction_date=when or datetime.now(UTC),
        reverses_transaction_id=reverses,
    )
    db.add(txn)
    db.flush()
    return txn


def _hold(
    db: Session,
    company: Company,
    entity: Entity,
    agent: Agent,
    status: VerdictStatus,
) -> Authorization:
    hold = Authorization(
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        vendor="aws",
        amount=Decimal("10"),
        currency=entity.currency,
        local_amount=Decimal("10"),
        local_currency=entity.currency,
        reason="needs a human",
        status=status,
    )
    db.add(hold)
    db.flush()
    return hold


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ------------------------------ /budgets bounds ------------------------------


def test_budgets_honours_limit_and_reports_full_total(
    client: TestClient, db_session: Session
) -> None:
    """`total` counts every matching agent, not the length of the page."""
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    for i in range(5):
        _agent(db_session, company, entity, f"ops-{i}")

    body = client.get("/budgets?limit=2", headers=_headers(key)).json()
    assert len(body["agents"]) == 2
    assert body["total"] == 5


def test_budgets_offset_walks_the_full_set_without_gaps(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    for i in range(5):
        _agent(db_session, company, entity, f"ops-{i}")

    seen: list[str] = []
    for offset in (0, 2, 4):
        body = client.get(
            f"/budgets?limit=2&offset={offset}", headers=_headers(key)
        ).json()
        seen.extend(row["slug"] for row in body["agents"])
    assert sorted(seen) == [f"ops-{i}" for i in range(5)]


def test_budgets_rejects_oversized_limit(
    client: TestClient, db_session: Session
) -> None:
    """A limit above the cap is a 422, never a silent clamp."""
    _, key = _company(db_session)
    assert client.get("/budgets?limit=10000", headers=_headers(key)).status_code == 422


def test_budgets_agent_filter_finds_an_agent_past_the_first_page(
    client: TestClient, db_session: Session
) -> None:
    """The SDK's `check_budget()` path: server-side filter, not a client scan."""
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    for i in range(5):
        _agent(db_session, company, entity, f"ops-{i}")
    target = _agent(db_session, company, entity, "zzz-last")
    _spend(db_session, company, entity, target, Decimal("250"))

    body = client.get("/budgets?agent=zzz-last&limit=1", headers=_headers(key)).json()
    assert [row["slug"] for row in body["agents"]] == ["zzz-last"]
    assert Decimal(body["agents"][0]["spent"]) == Decimal("250")
    assert body["total"] == 1


def test_budgets_agent_filter_is_company_scoped(
    client: TestClient, db_session: Session
) -> None:
    """A slug is unique per tenant, so the filter must not cross companies."""
    mine, key = _company(db_session)
    _entity(db_session, mine, "sg")
    theirs, _ = _company(db_session)
    other_entity = _entity(db_session, theirs, "us")
    _agent(db_session, theirs, other_entity, "shared-slug")

    body = client.get("/budgets?agent=shared-slug", headers=_headers(key)).json()
    assert body["agents"] == []
    assert body["total"] == 0


def test_workflow_rollup_includes_members_off_the_page(
    client: TestClient, db_session: Session
) -> None:
    """The correctness property pagination must not break.

    Both members spend, but `limit=1` returns only one agent. The workflow total
    must still be the full 700 — a rollup that shrank with the page size would
    silently under-report a shared budget.
    """
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    first = _agent(db_session, company, entity, "aaa-first")
    second = _agent(db_session, company, entity, "zzz-second")
    _spend(db_session, company, entity, first, Decimal("300"))
    _spend(db_session, company, entity, second, Decimal("400"))
    workflow = Workflow(
        company_id=company.id, name="Q3 offsite", budget=Decimal("1000")
    )
    workflow.agents = [first, second]
    db_session.add(workflow)
    db_session.flush()

    body = client.get("/budgets?limit=1", headers=_headers(key)).json()
    assert len(body["agents"]) == 1
    wf = body["workflows"][0]
    assert Decimal(wf["spent"]) == Decimal("700")
    assert set(wf["agent_ids"]) == {str(first.id), str(second.id)}


# --------------------- the batched aggregate equals the per-agent form -------


def test_batched_spend_matches_per_agent_form(db_session: Session) -> None:
    """One aggregate per entity must produce what two queries per agent produced."""
    company, _ = _company(db_session)
    sg = _entity(db_session, company, "sg")
    us = _entity(db_session, company, "us")
    agents = [
        _agent(db_session, company, sg, "sg-a"),
        _agent(db_session, company, sg, "sg-b"),
        _agent(db_session, company, us, "us-a"),
    ]
    _spend(db_session, company, sg, agents[0], Decimal("120"))
    _spend(db_session, company, us, agents[2], Decimal("75"))
    # agents[1] deliberately has no spend — it must be absent from the map, and
    # the caller's zero default must reproduce the per-agent form's Decimal("0").

    batched = company_period_committed_spend(db_session, company.id, [sg, us])
    entities = {sg.id: sg, us.id: us}
    for agent in agents:
        expected = period_committed_spend(
            db_session, agent.id, entities[agent.entity_id]
        )
        assert batched.get(agent.id, (Decimal("0"), Decimal("0"))) == expected


def test_batched_spend_subtracts_reversals(db_session: Session) -> None:
    """A refund frees budget (T1-16) — the signed sum is duplicated in the
    batched query, so it needs its own coverage."""
    company, _ = _company(db_session)
    entity = _entity(db_session, company)
    agent = _agent(db_session, company, entity, "ops")
    original = _spend(db_session, company, entity, agent, Decimal("500"))
    _spend(db_session, company, entity, agent, Decimal("200"), reverses=original.id)

    batched = company_period_committed_spend(db_session, company.id, [entity])
    assert batched[agent.id][0] == Decimal("300")


def test_batched_spend_separates_today_from_this_month(db_session: Session) -> None:
    """The `FILTER` on the today-window must not leak earlier-month spend."""
    company, _ = _company(db_session)
    entity = _entity(db_session, company)
    month_start, day_start = period_starts(now_in_entity_tz(entity))
    if day_start <= month_start:
        pytest.skip("today is the 1st: no earlier-this-month window exists")

    agent = _agent(db_session, company, entity, "ops")
    _spend(
        db_session,
        company,
        entity,
        agent,
        Decimal("90"),
        when=day_start - timedelta(hours=1),
    )
    _spend(db_session, company, entity, agent, Decimal("10"))

    month_total, today_total = company_period_committed_spend(
        db_session, company.id, [entity]
    )[agent.id]
    assert month_total == Decimal("100")
    assert today_total == Decimal("10")


def test_batched_spend_is_company_scoped(db_session: Session) -> None:
    """The `agents` join must not total another tenant's spend."""
    mine, _ = _company(db_session)
    my_entity = _entity(db_session, mine, "sg")
    my_agent = _agent(db_session, mine, my_entity, "ops")
    theirs, _ = _company(db_session)
    their_entity = _entity(db_session, theirs, "sg")
    their_agent = _agent(db_session, theirs, their_entity, "ops")
    _spend(db_session, theirs, their_entity, their_agent, Decimal("999"))

    batched = company_period_committed_spend(db_session, mine.id, [my_entity])
    assert their_agent.id not in batched
    assert my_agent.id not in batched


# ------------------------------ /approvals bounds ----------------------------


def test_approvals_honours_limit_and_reports_full_total(
    client: TestClient, db_session: Session
) -> None:
    """The queue grows with how long holds go unattended — it must be bounded."""
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    agent = _agent(db_session, company, entity, "ops")
    for _ in range(4):
        _hold(db_session, company, entity, agent, VerdictStatus.HUMAN_HOLD)

    body = client.get("/approvals?limit=2", headers=_headers(key)).json()
    assert len(body["items"]) == 2
    assert body["total"] == 4


def test_approvals_offset_walks_the_queue(
    client: TestClient, db_session: Session
) -> None:
    company, key = _company(db_session)
    entity = _entity(db_session, company)
    agent = _agent(db_session, company, entity, "ops")
    ids = {
        str(_hold(db_session, company, entity, agent, VerdictStatus.REVIEW).id)
        for _ in range(4)
    }

    seen: set[str] = set()
    for offset in (0, 2):
        body = client.get(
            f"/approvals?limit=2&offset={offset}", headers=_headers(key)
        ).json()
        seen.update(row["id"] for row in body["items"])
    assert seen == ids


def test_approvals_rejects_oversized_limit(
    client: TestClient, db_session: Session
) -> None:
    _, key = _company(db_session)
    assert (
        client.get("/approvals?limit=10000", headers=_headers(key)).status_code == 422
    )
