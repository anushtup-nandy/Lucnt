"""Integration tests for the Books router (Phase 2, Module 2/4).

Captures a known mix of transactions, then asserts the financial statements
derived from them — most importantly the double-entry identities: the trial
balance nets to zero and the balance sheet balances.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import Agent, ApiKey, ApiKeyMode, Company, Entity, FxRate, Vendor
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
        country="SG",  # place of supply of the recipient (TE-1)
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _make_vendor(
    db_session: Session,
    company: Company,
    name: str,
    *,
    country: str,
    gst_registered_in: list[str] | None = None,
) -> None:
    """Pre-enrich a vendor so a spend classifies by place of supply (TE-1)."""
    db_session.add(
        Vendor(
            company_id=company.id,
            name=name,
            normalized_name=name.strip().lower(),
            country=country,
            gst_registered_in=gst_registered_in or [],
        )
    )
    db_session.flush()


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


def _totals(payload: dict[str, object], currency: str = "SGD") -> dict[str, object]:
    """The per-currency totals block for one currency (statements are per-currency)."""
    currencies = payload["currencies"]
    assert isinstance(currencies, list)
    for entry in currencies:
        if entry["currency"] == currency:
            return entry  # type: ignore[no-any-return]
    raise AssertionError(f"no {currency} totals in {currencies}")


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
    # Place of supply (TE-1): openai is a foreign vendor not registered in SG ->
    # imported reverse charge; localcorp is domestic SG-registered -> forward GST.
    _make_vendor(db_session, company, "openai", country="US")
    _make_vendor(
        db_session, company, "localcorp", country="SG", gst_registered_in=["SG"]
    )
    # Seed a clean test FX rate so USD->SGD = 2.0 (round numbers): one SGD is
    # worth 0.5 USD. Effective 2026-01-02 to beat the 2026-01-01 baseline.
    db_session.add(
        FxRate(
            currency="SGD",
            usd_per_unit=Decimal("0.5"),
            effective_from=date(2026, 1, 2),
            source="test",
        )
    )
    db_session.flush()
    # Imported (USD 100 paid by SGD entity) -> SGD 200 @ 2.0; 9% RCM on top = 18.
    _capture(
        client,
        key,
        vendor="openai",
        amount="100",
        currency="USD",
        model="gpt-5",
        tokens=128000,
    )
    # Domestic (SGD) -> 9% GST embedded in 218 (== 18.00 tax).
    _capture(client, key, vendor="localcorp", amount="218", currency="SGD")
    return client, key


def test_trial_balance_balances(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    tb = client.get("/books/tb", headers=_headers(key)).json()
    # Imported: expense 200 + ITC 18; domestic: expense 200 + ITC 18 -> debit 436.
    # Credits: wallet (200 + 218) + GST payable 18 -> 436. Single currency (SGD).
    sgd = _totals(tb)
    assert Decimal(str(sgd["total_debit"])) == Decimal("436")
    assert Decimal(str(sgd["total_credit"])) == Decimal("436")
    assert sgd["balanced"] is True
    assert tb["balanced"] is True


def test_profit_and_loss(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    pl = client.get("/books/pl", headers=_headers(key)).json()
    sgd = _totals(pl)
    assert Decimal(str(sgd["total_income"])) == Decimal("0")
    assert Decimal(str(sgd["total_expenses"])) == Decimal("400")  # 200 AI + 200 sw
    assert Decimal(str(sgd["net_profit"])) == Decimal("-400")


def test_balance_sheet_balances(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    bs = client.get("/books/bs", headers=_headers(key)).json()
    sgd = _totals(bs)
    assert Decimal(str(sgd["total_assets"])) == Decimal("-382")  # wallet -418 + ITC 36
    assert Decimal(str(sgd["total_liabilities"])) == Decimal("18")  # GST payable
    assert Decimal(str(sgd["total_equity"])) == Decimal("-400")  # retained earnings
    assert sgd["balanced"] is True
    assert bs["balanced"] is True


def test_cash_flow_is_net_outflow(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    cf = client.get("/books/cf", headers=_headers(key)).json()
    assert Decimal(str(_totals(cf)["net_cash_flow"])) == Decimal("-418")  # 200 + 218


def test_general_ledger_lists_posted_accounts(
    books_client: tuple[TestClient, str],
) -> None:
    client, key = books_client
    gl = client.get("/books/gl", headers=_headers(key)).json()
    by_code = {a["account_code"]: a for a in gl["accounts"]}
    assert Decimal(by_code["1010"]["balance"]) == Decimal("-418")  # wallet
    assert Decimal(by_code["1400"]["balance"]) == Decimal("36")  # input tax credit
    assert Decimal(by_code["2200"]["balance"]) == Decimal("-18")  # GST payable (credit)


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
    assert tb["currencies"] == []  # no postings -> no per-currency totals
    assert tb["balanced"] is True  # vacuously (every currency balances)
    journals = client.get("/books/journals", headers=_headers(other_key)).json()
    assert journals["total"] == 0


def test_books_entity_filter(books_client: tuple[TestClient, str]) -> None:
    client, key = books_client
    tb_sg = client.get("/books/tb?entity=sg", headers=_headers(key)).json()
    assert Decimal(str(_totals(tb_sg)["total_debit"])) == Decimal("436")
    resp = client.get("/books/tb?entity=nope", headers=_headers(key))
    assert resp.status_code == 404


def test_books_date_filter_scopes_period(
    books_client: tuple[TestClient, str],
) -> None:
    """T1-17: a statement can be scoped to a period, not inception-to-date."""
    client, key = books_client
    # A future window contains none of the (now-dated) spends.
    future = client.get("/books/pl?date_from=2099-01-01", headers=_headers(key)).json()
    assert future["currencies"] == []
    # A window ending in the past likewise.
    past = client.get("/books/pl?date_to=2000-01-01", headers=_headers(key)).json()
    assert past["currencies"] == []
    # No filter -> the full period (both spends).
    full = client.get("/books/pl", headers=_headers(key)).json()
    assert Decimal(str(_totals(full)["total_expenses"])) == Decimal("400")


def test_company_level_statements_are_per_currency(
    client: TestClient, db_session: Session
) -> None:
    """T1-1: a company spanning currencies gets per-currency totals, never a single
    mixed 'balanced' total. Each currency balances independently."""
    company, key = _make_company_with_key(db_session)
    sg = _make_entity(db_session, company)  # SGD, SG-GST
    us = Entity(
        company_id=company.id,
        name="LUCNT US",
        code="us",
        full_name="LUCNT US Inc",
        currency="USD",
        accounting_std="US-GAAP",
        tax_regime="US-Sales-Tax",  # no self-assessed tax (T1-15)
        timezone="America/Los_Angeles",
    )
    db_session.add(us)
    db_session.flush()
    _make_agent(db_session, company, sg)  # slug "ops" on the SGD entity
    db_session.add(
        Agent(
            company_id=company.id,
            entity_id=us.id,
            name="US Agent",
            slug="us_ops",
            glyph="x",
            role="Ops",
            monthly_budget=Decimal("100000"),
        )
    )
    db_session.flush()

    def _spend(agent: str, entity: str, amount: str, currency: str) -> None:
        v = client.post(
            "/transactions/authorize",
            json={
                "agent": agent,
                "entity": entity,
                "vendor": "localcorp",
                "amount": amount,
                "currency": currency,
                "reason": "t",
            },
            headers=_headers(key),
        ).json()
        client.post(
            "/transactions/capture",
            json={"authorization_id": v["authorization_id"], "source": "sdk"},
            headers=_headers(key),
        )

    _spend("ops", "sg", "109", "SGD")  # domestic SGD: expense 100 + ITC 9 / wallet 109
    _spend("us_ops", "us", "100", "USD")  # US no-tax: expense 100 / wallet 100

    tb = client.get("/books/tb", headers=_headers(key)).json()
    by_ccy = {c["currency"]: c for c in tb["currencies"]}
    assert set(by_ccy) == {"SGD", "USD"}
    assert Decimal(by_ccy["SGD"]["total_debit"]) == Decimal("109")
    assert by_ccy["SGD"]["balanced"] is True
    assert Decimal(by_ccy["USD"]["total_debit"]) == Decimal("100")
    assert by_ccy["USD"]["balanced"] is True
    assert tb["balanced"] is True  # every currency balances
    assert "total_debit" not in tb  # no bogus cross-currency scalar total


# --- General ledger bounds (T3-2) -------------------------------------------


def test_general_ledger_listing_omits_entries_but_reports_the_count(
    books_client: tuple[TestClient, str],
) -> None:
    """The listing must not serialise every journal line.

    Journal lines run 2-5x transaction count, so returning them all was the one
    unbounded *response* among the T3-2 endpoints. `entry_count` is what keeps an
    empty `entries` from reading as "this account has no postings".
    """
    client, key = books_client
    gl = client.get("/books/gl", headers=_headers(key)).json()
    by_code = {a["account_code"]: a for a in gl["accounts"]}
    wallet = by_code["1010"]
    assert wallet["entries"] == []
    assert wallet["entry_count"] > 0


def test_general_ledger_totals_match_the_trial_balance(
    books_client: tuple[TestClient, str],
) -> None:
    """Both statements now read the same grouped aggregate — they cannot disagree."""
    client, key = books_client
    gl = client.get("/books/gl", headers=_headers(key)).json()
    tb = client.get("/books/tb", headers=_headers(key)).json()
    gl_by_code = {
        (a["account_code"], a["currency"]): (a["total_debit"], a["total_credit"])
        for a in gl["accounts"]
    }
    for row in tb["rows"]:
        key_ = (row["account_code"], row["currency"])
        assert gl_by_code[key_] == (row["debit"], row["credit"])


def test_general_ledger_account_filter_returns_bounded_entries(
    books_client: tuple[TestClient, str],
) -> None:
    """`?account=` is the detail view, and it is paginated."""
    client, key = books_client
    gl = client.get("/books/gl?account=1010", headers=_headers(key)).json()
    assert [a["account_code"] for a in gl["accounts"]] == ["1010"]
    section = gl["accounts"][0]
    assert len(section["entries"]) == section["entry_count"]

    one = client.get("/books/gl?account=1010&limit=1", headers=_headers(key)).json()
    assert len(one["accounts"][0]["entries"]) == 1
    # The totals are unaffected by the page — they come from the aggregate.
    assert one["accounts"][0]["total_debit"] == section["total_debit"]
    assert one["accounts"][0]["entry_count"] == section["entry_count"]


def test_general_ledger_rejects_oversized_limit(
    books_client: tuple[TestClient, str],
) -> None:
    client, key = books_client
    response = client.get("/books/gl?account=1010&limit=10000", headers=_headers(key))
    assert response.status_code == 422
