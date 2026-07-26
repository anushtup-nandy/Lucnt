"""Integration tests for the Phase 5 Intelligence layer (M8).

Transactions are inserted directly (bypassing capture) so the tests control
amounts, projects, and dates. Dates are relative to real ``now`` because
``/intelligence/recompute`` and ``/intelligence/query`` read the wall clock; every
inserted spend lands in the current calendar month.

Determinism note: a single outlier of value B over ``k`` identical baseline points
of value b has z-score exactly ``sqrt(k)`` (independent of b, B). So ``9×10 + 1×100``
gives an anomaly score of exactly ``3.0`` — comfortably over the 2.5σ threshold.

The per-file company/agent/entity helpers mirror the other integration files.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
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
    Fingerprint,
    Transaction,
    TransactionSource,
)
from lucnt_api.routers.intelligence import (
    _cost_units_for_agent,
    _month_to_date,
    _month_to_date_for_agent,
    _record,
)
from lucnt_api.security import generate_api_key, hash_api_key
from lucnt_api.services.intelligence import CostUnit, cost_per_output

_NOW = datetime.now(timezone.utc)


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
        tax_regime="US-Sales-Tax",
        timezone="America/New_York",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _make_agent(
    db_session: Session,
    company: Company,
    entity: Entity,
    *,
    slug: str = "ops",
    monthly_budget: str = "100000",
    spent_ytd: str = "0",
) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name=f"Agent {slug}",
        slug=slug,
        glyph="x",
        role="Operations",
        monthly_budget=Decimal(monthly_budget),
        spent_ytd=Decimal(spent_ytd),
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
    vendor: str = "aws",
    amount: str,
    when: datetime | None = None,
    currency: str = "USD",
    project: str | None = None,
    tokens: int | None = None,
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
        project=project,
        tokens_consumed=tokens,
        transaction_date=when if when is not None else _NOW,
    )
    db_session.add(txn)
    db_session.flush()
    return txn


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _setup(db_session: Session) -> tuple[Company, str, Agent, Entity]:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity)
    return company, key, agent, entity


# --- recompute ---


def test_recompute_empty_ledger(client: TestClient, db_session: Session) -> None:
    _, key, _, _ = _setup(db_session)
    resp = client.post("/intelligence/recompute", headers=_auth(key)).json()
    assert resp["agents_analyzed"] == 0
    assert resp["insights"] == 0
    assert resp["memories"] == 0


def test_recompute_builds_fingerprint_and_memories(
    client: TestClient, db_session: Session
) -> None:
    company, key, agent, entity = _setup(db_session)
    for i in range(4):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="openai",
            amount="10",
            project="research",
            tokens=1000,
            when=_NOW - timedelta(hours=i),
        )

    resp = client.post("/intelligence/recompute", headers=_auth(key)).json()
    assert resp["agents_analyzed"] == 1
    assert resp["fingerprints"] == 1

    memories = client.get("/memory", headers=_auth(key)).json()
    types = {m["memory_type"] for m in memories["items"]}
    assert types == {"spend_pattern", "vendor_affinity", "cost_profile"}
    affinity = next(
        m for m in memories["items"] if m["memory_type"] == "vendor_affinity"
    )
    assert "openai" in affinity["summary"]


def test_recompute_flags_budget_risk(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    # $5,000 this month against a $1,000 budget -> run-rate always projects over.
    agent = _make_agent(
        db_session, company, entity, slug="burner", monthly_budget="1000"
    )
    for i in range(5):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            amount="1000",
            when=_NOW - timedelta(hours=i),
        )

    client.post("/intelligence/recompute", headers=_auth(key))
    insights = client.get(
        "/insights?insight_type=budget_risk", headers=_auth(key)
    ).json()
    assert insights["total"] == 1
    assert "overrun" in insights["items"][0]["value_text"]


def test_recompute_flags_anomaly(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    # 9 spends of 10 + 1 of 100 -> z of the outlier = sqrt(9) = 3.0 >= 2.5.
    for i in range(9):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            amount="10",
            when=_NOW - timedelta(hours=i + 1),
        )
    _make_transaction(db_session, company, agent, entity, amount="100", when=_NOW)

    client.post("/intelligence/recompute", headers=_auth(key))
    insights = client.get("/insights?insight_type=anomaly", headers=_auth(key)).json()
    assert insights["total"] == 1
    assert "σ" in insights["items"][0]["value_text"]


def test_recompute_flags_cost_spike(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    # Two charges per project (a one-off is not a spike, T2-13). Unit avg costs are
    # [10, 12, 100]; leave-one-out median for "spike" is 11, 3x = 33, so only
    # "spike" (100) fires.
    for project, amount in [("cheap", "10"), ("mid", "12"), ("spike", "100")]:
        for _ in range(2):
            _make_transaction(
                db_session, company, agent, entity, amount=amount, project=project
            )

    client.post("/intelligence/recompute", headers=_auth(key))
    insights = client.get(
        "/insights?insight_type=cost_spike", headers=_auth(key)
    ).json()
    assert insights["total"] == 1
    assert insights["items"][0]["action_payload"]["project"] == "spike"


def test_recompute_is_idempotent(client: TestClient, db_session: Session) -> None:
    company, key, agent, entity = _setup(db_session)
    for i in range(4):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="openai",
            amount="10",
            project="research",
            when=_NOW - timedelta(hours=i),
        )

    first = client.post("/intelligence/recompute", headers=_auth(key)).json()
    second = client.post("/intelligence/recompute", headers=_auth(key)).json()
    assert first == second
    # Memories upserted, not duplicated.
    assert client.get("/memory", headers=_auth(key)).json()["total"] == 3


def test_recompute_scoped_to_company(client: TestClient, db_session: Session) -> None:
    company_a, key_a = _make_company_with_key(db_session)
    entity_a = _make_entity(db_session, company_a)
    agent_a = _make_agent(
        db_session, company_a, entity_a, slug="burner", monthly_budget="1"
    )
    _make_transaction(db_session, company_a, agent_a, entity_a, amount="1000")
    _, key_b = _make_company_with_key(db_session)

    client.post("/intelligence/recompute", headers=_auth(key_a))
    assert client.get("/insights", headers=_auth(key_b)).json()["total"] == 0
    assert client.get("/insights", headers=_auth(key_a)).json()["total"] >= 1


def test_recompute_upserts_insight_in_place(
    client: TestClient, db_session: Session
) -> None:
    """T2-10: a re-generated insight keeps its id (no delete+reinsert churn)."""
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity, slug="over", monthly_budget="10")
    _make_transaction(db_session, company, agent, entity, amount="1000")

    client.post("/intelligence/recompute", headers=_auth(key))
    first = client.get("/insights", headers=_auth(key)).json()["items"]
    assert len(first) >= 1
    insight_id = first[0]["id"]

    client.post("/intelligence/recompute", headers=_auth(key))
    second = client.get("/insights", headers=_auth(key)).json()["items"]
    assert any(i["id"] == insight_id for i in second)  # same row, refreshed in place


def test_dismissed_insight_not_regenerated(
    client: TestClient, db_session: Session
) -> None:
    """T2-10: a dismissed insight stays gone and is not regenerated by recompute."""
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity, slug="over", monthly_budget="10")
    _make_transaction(db_session, company, agent, entity, amount="1000")

    client.post("/intelligence/recompute", headers=_auth(key))
    insight_id = client.get("/insights", headers=_auth(key)).json()["items"][0]["id"]
    client.post(f"/insights/{insight_id}/dismiss", headers=_auth(key))

    assert client.get("/insights", headers=_auth(key)).json()["total"] == 0  # hidden
    client.post("/intelligence/recompute", headers=_auth(key))  # must not resurrect
    assert client.get("/insights", headers=_auth(key)).json()["total"] == 0
    all_ins = client.get("/insights?include_dismissed=true", headers=_auth(key)).json()[
        "items"
    ]
    assert any(i["id"] == insight_id and i["is_dismissed"] for i in all_ins)


def test_cold_start_memory_for_learning_agent(
    client: TestClient, db_session: Session
) -> None:
    """T2-12: an agent with too little history still gets a 'learning' memory row."""
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity, slug="fresh")
    _make_transaction(db_session, company, agent, entity, amount="10")  # 1 spend

    client.post("/intelligence/recompute", headers=_auth(key))
    memories = client.get("/memory", headers=_auth(key)).json()["items"]
    learning = [
        m
        for m in memories
        if m["memory_type"] == "spend_pattern"
        and m["metrics_json"].get("learning") is True
    ]
    assert len(learning) == 1


# --- apply insight ---


def test_apply_insight(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity, slug="burner", monthly_budget="1")
    _make_transaction(db_session, company, agent, entity, amount="1000")

    client.post("/intelligence/recompute", headers=_auth(key))
    inbox = client.get("/insights", headers=_auth(key)).json()
    assert inbox["unapplied_count"] == 1
    insight_id = inbox["items"][0]["id"]

    applied = client.post(f"/insights/{insight_id}/apply", headers=_auth(key)).json()
    assert applied["is_applied"] is True
    assert applied["applied_at"] is not None

    after = client.get("/insights", headers=_auth(key)).json()
    assert after["total"] == 0  # default excludes applied
    assert after["unapplied_count"] == 0
    with_applied = client.get(
        "/insights?include_applied=true", headers=_auth(key)
    ).json()
    assert with_applied["total"] == 1


def test_applied_insight_survives_recompute(
    client: TestClient, db_session: Session
) -> None:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(db_session, company, entity, slug="burner", monthly_budget="1")
    _make_transaction(db_session, company, agent, entity, amount="1000")

    client.post("/intelligence/recompute", headers=_auth(key))
    insight_id = client.get("/insights", headers=_auth(key)).json()["items"][0]["id"]
    client.post(f"/insights/{insight_id}/apply", headers=_auth(key))

    # Recompute clears only un-applied insights; the applied one persists.
    client.post("/intelligence/recompute", headers=_auth(key))
    all_insights = client.get(
        "/insights?include_applied=true", headers=_auth(key)
    ).json()
    ids = {i["id"] for i in all_insights["items"]}
    assert insight_id in ids


def test_apply_unknown_insight_404(client: TestClient, db_session: Session) -> None:
    _, key, _, _ = _setup(db_session)
    resp = client.post(f"/insights/{uuid.uuid4()}/apply", headers=_auth(key))
    assert resp.status_code == 404


# --- query (living financial memory) ---


def test_query_blank_slate(client: TestClient, db_session: Session) -> None:
    _, key, _, _ = _setup(db_session)  # agent "ops" with no transactions
    mem = client.get("/intelligence/query?agent=ops", headers=_auth(key)).json()
    assert mem["agent"] == "ops"
    assert mem["fingerprint"] is None
    assert mem["cost_per_output"] == []
    assert mem["projection"]["on_track"] is True
    assert Decimal(mem["budget"]["monthly_budget"]) == Decimal("100000")


def test_query_live_financial_memory(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(
        db_session,
        company,
        entity,
        slug="research",
        monthly_budget="1000000",
        spent_ytd="500",
    )
    for i in range(4):
        _make_transaction(
            db_session,
            company,
            agent,
            entity,
            vendor="openai",
            amount="10",
            project="report",
            tokens=1000,
            when=_NOW - timedelta(hours=i),
        )

    mem = client.get("/intelligence/query?agent=research", headers=_auth(key)).json()
    assert mem["currency"] == "USD"
    # Budget spend is derived from this month's committed ledger (4 x 10), not the
    # never-reset spent_ytd=500 counter (PRODUCTION_PLAN T2-3).
    assert Decimal(mem["budget"]["spent"]) == Decimal("40")
    assert mem["projection"]["on_track"] is True  # huge budget
    assert mem["fingerprint"]["total_records"] == 4
    assert mem["fingerprint"]["top_vendors"][0]["vendor"] == "openai"
    report = next(u for u in mem["cost_per_output"] if u["unit"] == "report")
    assert Decimal(report["avg_cost"]) == Decimal("10.0000")
    assert Decimal(report["cost_per_1k_tokens"]) == Decimal("10.0000")


def test_query_headline_routing(client: TestClient, db_session: Session) -> None:
    company, key = _make_company_with_key(db_session)
    entity = _make_entity(db_session, company)
    agent = _make_agent(
        db_session, company, entity, slug="research", monthly_budget="1000000"
    )
    _make_transaction(db_session, company, agent, entity, amount="10", project="report")

    budget_q = client.get(
        "/intelligence/query?agent=research&q=how much budget is left",
        headers=_auth(key),
    ).json()
    assert "left this cycle" in budget_q["headline"]

    cost_q = client.get(
        "/intelligence/query?agent=research&q=what is my cost per output",
        headers=_auth(key),
    ).json()
    assert "report" in cost_q["headline"]

    track_q = client.get(
        "/intelligence/query?agent=research&q=am I on track this month",
        headers=_auth(key),
    ).json()
    assert "track" in track_q["headline"].lower()


def test_query_unknown_agent_404(client: TestClient, db_session: Session) -> None:
    _, key, _, _ = _setup(db_session)
    resp = client.get("/intelligence/query?agent=ghost", headers=_auth(key))
    assert resp.status_code == 404


# --- query: persisted fast path vs live fallback (T3-2) ----------------------


def test_query_falls_back_to_live_without_a_recompute(
    client: TestClient, db_session: Session
) -> None:
    """The documented contract: `query()` needs no prior recompute.

    Pinned explicitly because the fast path below reads a stored fingerprint —
    an agent that has never been recomputed must still get real statistics, not
    a null block.
    """
    company, key, agent, entity = _setup(db_session)
    _make_transaction(db_session, company, agent, entity, amount="10")
    assert (
        db_session.execute(
            select(Fingerprint).where(Fingerprint.agent_id == agent.id)
        ).scalar_one_or_none()
        is None
    )

    body = client.get(
        f"/intelligence/query?agent={agent.slug}", headers=_auth(key)
    ).json()
    assert body["fingerprint"] is not None
    assert body["fingerprint"]["total_records"] == 1


def test_query_serves_the_persisted_fingerprint_after_recompute(
    client: TestClient, db_session: Session
) -> None:
    """The fast path: identical figures, read rather than recomputed."""
    company, key, agent, entity = _setup(db_session)
    for amount in ("10", "20", "30"):
        _make_transaction(db_session, company, agent, entity, amount=amount)

    live = client.get(
        f"/intelligence/query?agent={agent.slug}", headers=_auth(key)
    ).json()["fingerprint"]
    client.post("/intelligence/recompute", headers=_auth(key))
    stored = client.get(
        f"/intelligence/query?agent={agent.slug}", headers=_auth(key)
    ).json()["fingerprint"]

    assert stored == live, "reading the stored fingerprint changed the figures"


def test_query_budget_stays_live_when_the_fingerprint_is_stale(
    client: TestClient, db_session: Session
) -> None:
    """The deliberate staleness tradeoff, stated as a test.

    After a recompute, new spend does NOT move the stored statistics — but it
    MUST move `budget`, which is a live SQL sum. Budget headroom is the figure an
    agent gates on, so it is the one that may never be stale.
    """
    company, key, agent, entity = _setup(db_session)
    _make_transaction(db_session, company, agent, entity, amount="10")
    client.post("/intelligence/recompute", headers=_auth(key))

    _make_transaction(db_session, company, agent, entity, amount="25")
    body = client.get(
        f"/intelligence/query?agent={agent.slug}", headers=_auth(key)
    ).json()

    assert Decimal(body["budget"]["spent"]) == Decimal("35"), "budget went stale"
    assert body["fingerprint"]["total_records"] == 1, "fingerprint unexpectedly fresh"


def test_sql_cost_units_match_the_pure_function(
    client: TestClient, db_session: Session
) -> None:
    """The drift guard between the SQL aggregate and the engine it mirrors.

    `_cost_units_for_agent` reimplements `cost_per_output` in SQL. Comments cannot
    stop those diverging; this can. Covers the awkward cases: an unattributed
    group, a group where only some rows carry tokens (so token metrics must be
    suppressed), and two currencies.
    """
    company, _key, agent, entity = _setup(db_session)
    rows = [
        {"amount": "10", "project": "alpha", "tokens": 100},
        {"amount": "20", "project": "alpha", "tokens": 200},
        {"amount": "30", "project": "beta", "tokens": None},
        {"amount": "40", "project": "beta", "tokens": 400},
        {"amount": "50", "project": None, "tokens": 500},
        {"amount": "60", "project": "", "tokens": 600},
        {"amount": "70", "project": "alpha", "tokens": 700, "currency": "SGD"},
    ]
    for row in rows:
        _make_transaction(db_session, company, agent, entity, **row)  # type: ignore[arg-type]

    records = [
        _record(txn)
        for txn in db_session.execute(
            select(Transaction).where(Transaction.agent_id == agent.id)
        ).scalars()
    ]
    expected = cost_per_output(records)
    actual = _cost_units_for_agent(db_session, agent.id)

    def _key_of(u: CostUnit) -> tuple[str, str]:
        return (u.unit, u.currency)

    assert sorted(expected, key=_key_of) == sorted(actual, key=_key_of)


def test_sql_month_to_date_matches_the_python_form(
    client: TestClient, db_session: Session
) -> None:
    """Same guard for the MTD sum — including its two quirks.

    It filters to the home currency and does NOT sign-flip reversals, which is
    what makes it different from `spend_window.committed_spend`.
    """
    company, _key, agent, entity = _setup(db_session)
    _make_transaction(db_session, company, agent, entity, amount="10")
    _make_transaction(db_session, company, agent, entity, amount="20")
    _make_transaction(db_session, company, agent, entity, amount="99", currency="SGD")
    _make_transaction(
        db_session,
        company,
        agent,
        entity,
        amount="5",
        when=_NOW - timedelta(days=70),
    )

    records = [
        _record(txn)
        for txn in db_session.execute(
            select(Transaction).where(Transaction.agent_id == agent.id)
        ).scalars()
    ]
    assert _month_to_date_for_agent(
        db_session, agent.id, "USD", _NOW
    ) == _month_to_date(records, _NOW, "USD")
