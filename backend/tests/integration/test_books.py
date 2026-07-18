"""Integration tests for the Books router (Phase 2, Module 2/4).

Captures a known mix of transactions, then asserts the financial statements
derived from them — most importantly the double-entry identities: the trial
balance nets to zero and the balance sheet balances.
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
from lucnt_api.models import Agent, ApiKey, ApiKeyMode, Company, Entity
from lucnt_api.security import generate_api_key, hash_api_key


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
    return entity


def _make_agent(db_session: Session, company: Company, entity: Entity) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops Agent",
        slug="ops",
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("100000"),
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _capture(
    client: TestClient,
    key: str,
    *,
    vendor: str,
    amount: str,
    currency: str,
    **extra: object,
) -> None:
    """Authorize + capture one spend for agent `ops` / entity `sg`."""
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "sg",
            "vendor": vendor,
            "amount": amount,
            "currency": currency,
            "reason": "test",
            **extra,
        },
        headers=_headers(key),
    ).json()
    client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_headers(key),
    )


@pytest.fixture
def books_client(client: TestClient, db_session: Session) -> tuple[TestClient, str]:
    """A company with two captured spends: an imported RCM and a domestic GST one."""
    company, key = _make_company_with_key(db_session)
    _make_agent(db_session, company, _make_entity(db_session, company))
    # Imported (USD paid by SGD entity) -> 9% reverse charge on top of 100.
    _capture(
        client, key, vendor="openai", amount="100", currency="USD",
        model="gpt-5", tokens=128000,
    )
    # Domestic (SGD) -> 9% GST embedded in 218 (== 18.00 tax).
    _capture(client, key, vendor="localcorp", amount="218", currency="SGD")
    return client, key


def test_trial_balance_balances(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    tb = client.get("/books/tb", headers=_headers(key)).json()
    assert Decimal(tb["total_debit"]) == Decimal("327")
    assert Decimal(tb["total_credit"]) == Decimal("327")
    assert tb["balanced"] is True


def test_profit_and_loss(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    pl = client.get("/books/pl", headers=_headers(key)).json()
    assert Decimal(pl["total_income"]) == Decimal("0")
    assert Decimal(pl["total_expenses"]) == Decimal("300")  # 100 AI + 200 software
    assert Decimal(pl["net_profit"]) == Decimal("-300")


def test_balance_sheet_balances(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    bs = client.get("/books/bs", headers=_headers(key)).json()
    assert Decimal(bs["total_assets"]) == Decimal("-291")  # wallet -318 + ITC 27
    assert Decimal(bs["total_liabilities"]) == Decimal("9")  # GST payable
    assert Decimal(bs["total_equity"]) == Decimal("-300")  # retained earnings
    assert bs["balanced"] is True


def test_cash_flow_is_net_outflow(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    cf = client.get("/books/cf", headers=_headers(key)).json()
    assert Decimal(cf["net_cash_flow"]) == Decimal("-318")  # 100 + 218 out


def test_general_ledger_lists_posted_accounts(
    books_client: tuple[TestClient, str],
) -> None:
    client, key = books_client
    gl = client.get("/books/gl", headers=_headers(key)).json()
    by_code = {a["account_code"]: a for a in gl["accounts"]}
    assert Decimal(by_code["1010"]["balance"]) == Decimal("-318")  # wallet
    assert Decimal(by_code["1400"]["balance"]) == Decimal("27")  # input tax credit
    assert Decimal(by_code["2200"]["balance"]) == Decimal("-9")  # GST payable (credit)


def test_journals_group_by_transaction(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    journals = client.get("/books/journals", headers=_headers(key)).json()
    assert journals["total"] == 2
    for group in journals["items"]:
        assert Decimal(group["total_debit"]) == Decimal(group["total_credit"])


def test_books_are_company_scoped(
    client: TestClient, db_session: Session, books_client: tuple[TestClient, str]
) -> None:
    """A different company sees none of the first company's books."""
    _, other_key = _make_company_with_key(db_session)
    tb = client.get("/books/tb", headers=_headers(other_key)).json()
    assert tb["rows"] == []
    assert Decimal(tb["total_debit"]) == Decimal("0")
    journals = client.get("/books/journals", headers=_headers(other_key)).json()
    assert journals["total"] == 0


def test_books_entity_filter(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    tb_sg = client.get("/books/tb?entity=sg", headers=_headers(key)).json()
    assert Decimal(tb_sg["total_debit"]) == Decimal("327")
    resp = client.get("/books/tb?entity=nope", headers=_headers(key))
    assert resp.status_code == 404
