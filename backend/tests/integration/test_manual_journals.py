"""Integration tests for manual journals + funding + the unmapped bucket.

Exercises the write path the ledger previously lacked: a funding/opening-balance
entry (so the balance sheet isn't negative from day one), a balanced manual
journal (accrual/adjustment), and the loud `unmapped` bucket that surfaces a
posting whose account code resolves to no CoA account instead of dropping it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.models import (
    ApiKey,
    ApiKeyMode,
    ApiKeyScope,
    AuditLog,
    Company,
    Entity,
    JournalEntry,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_key(
    db_session: Session,
    company: Company,
    *,
    scope: ApiKeyScope = ApiKeyScope.ADMIN,
) -> str:
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db_session.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="k",
            mode=ApiKeyMode.LIVE,
            scope=scope,
        )
    )
    db_session.flush()
    return plaintext


def _make_company(db_session: Session) -> Company:
    company = Company(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    return company


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


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _totals(payload: dict[str, object], currency: str = "SGD") -> dict[str, object]:
    currencies = payload["currencies"]
    assert isinstance(currencies, list)
    for entry in currencies:
        if entry["currency"] == currency:
            return entry  # type: ignore[no-any-return]
    raise AssertionError(f"no {currency} totals in {currencies}")


@pytest.fixture
def funded(
    client: TestClient, db_session: Session
) -> tuple[TestClient, str, Company, Entity]:
    company = _make_company(db_session)
    entity = _make_entity(db_session, company)
    key = _make_key(db_session, company)
    return client, key, company, entity


def test_funding_makes_wallet_positive_and_balances(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    """Dr Wallet / Cr Contributed Capital — positive cash & equity, balanced."""
    client, key, _, _ = funded
    resp = client.post(
        "/books/funding",
        json={"entity": "sg", "amount": "1000"},
        headers=_headers(key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert Decimal(body["total_debit"]) == Decimal("1000")
    assert Decimal(body["total_credit"]) == Decimal("1000")

    bs = client.get("/books/bs", headers=_headers(key)).json()
    sgd = _totals(bs)
    assert Decimal(str(sgd["total_assets"])) == Decimal("1000")  # wallet
    assert Decimal(str(sgd["total_equity"])) == Decimal("1000")  # contributed capital
    assert sgd["balanced"] is True

    cf = client.get("/books/cf", headers=_headers(key)).json()
    assert Decimal(str(_totals(cf)["net_cash_flow"])) == Decimal("1000")


def test_manual_journal_posts_and_appears_in_tb(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    """A balanced manual journal (a prepaid) posts and shows in the trial balance."""
    client, key, _, _ = funded
    resp = client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "Prepaid software",
            "lines": [
                {"account_code": "6010", "debit": "100"},
                {"account_code": "1010", "credit": "100"},
            ],
        },
        headers=_headers(key),
    )
    assert resp.status_code == 201
    tb = client.get("/books/tb", headers=_headers(key)).json()
    codes = {r["account_code"] for r in tb["rows"]}
    assert {"6010", "1010"} <= codes
    assert _totals(tb)["balanced"] is True


def test_manual_journal_rejects_unbalanced(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    client, key, _, _ = funded
    resp = client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "bad",
            "lines": [
                {"account_code": "6010", "debit": "100"},
                {"account_code": "1010", "credit": "50"},
            ],
        },
        headers=_headers(key),
    )
    assert resp.status_code == 422
    assert "balance" in resp.json()["detail"].lower()


def test_manual_journal_rejects_unknown_account(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    client, key, _, _ = funded
    resp = client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "bad code",
            "lines": [
                {"account_code": "9999", "debit": "10"},
                {"account_code": "1010", "credit": "10"},
            ],
        },
        headers=_headers(key),
    )
    assert resp.status_code == 422
    assert "9999" in resp.json()["detail"]


def test_manual_journal_line_must_be_one_sided(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    """A line with both debit and credit non-zero is rejected by validation."""
    client, key, _, _ = funded
    resp = client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "two-sided",
            "lines": [
                {"account_code": "6010", "debit": "10", "credit": "10"},
                {"account_code": "1010", "credit": "10"},
            ],
        },
        headers=_headers(key),
    )
    assert resp.status_code == 422


def test_write_endpoints_require_admin(client: TestClient, db_session: Session) -> None:
    """A runtime-scoped key cannot post manual journals or funding."""
    company = _make_company(db_session)
    _make_entity(db_session, company)
    runtime = _make_key(db_session, company, scope=ApiKeyScope.RUNTIME)
    for path, payload in (
        ("/books/funding", {"entity": "sg", "amount": "100"}),
        (
            "/books/journal",
            {
                "entity": "sg",
                "memo": "x",
                "lines": [
                    {"account_code": "6010", "debit": "1"},
                    {"account_code": "1010", "credit": "1"},
                ],
            },
        ),
    ):
        resp = client.post(path, json=payload, headers=_headers(runtime))
        assert resp.status_code == 403


def test_unmapped_account_is_loud_not_dropped(
    funded: tuple[TestClient, str, Company, Entity],
    db_session: Session,
) -> None:
    """A posting to a code with no CoA account lands in an `unmapped` bucket."""
    client, key, company, entity = funded
    # Provision the CoA (so 1010 maps) via a funding call.
    client.post(
        "/books/funding",
        json={"entity": "sg", "amount": "100"},
        headers=_headers(key),
    )
    # Insert a balanced pair directly, one leg to a code that is in no CoA.
    db_session.add_all(
        [
            JournalEntry(
                company_id=company.id,
                entity_id=entity.id,
                transaction_id=None,
                posting_date=date(2026, 6, 1),
                account_name="Mystery",
                account_code="8888",
                debit=Decimal("30"),
                credit=Decimal("0"),
                ledger="sg",
                currency="SGD",
            ),
            JournalEntry(
                company_id=company.id,
                entity_id=entity.id,
                transaction_id=None,
                posting_date=date(2026, 6, 1),
                account_name="Cash & Wallet",
                account_code="1010",
                debit=Decimal("0"),
                credit=Decimal("30"),
                ledger="sg",
                currency="SGD",
            ),
        ]
    )
    db_session.flush()

    tb = client.get("/books/tb", headers=_headers(key)).json()
    unmapped = [r for r in tb["rows"] if r["account_code"] == "8888"]
    assert len(unmapped) == 1
    assert unmapped[0]["category"] == "unmapped"  # surfaced, not silently dropped
    assert _totals(tb)["balanced"] is True  # the pair still balances


def test_funding_writes_an_audit_row(
    funded: tuple[TestClient, str, Company, Entity],
    db_session: Session,
) -> None:
    client, key, company, _ = funded
    client.post(
        "/books/funding",
        json={"entity": "sg", "amount": "500"},
        headers=_headers(key),
    )
    actions = (
        db_session.execute(
            select(AuditLog.action).where(AuditLog.company_id == company.id)
        )
        .scalars()
        .all()
    )
    assert "books.funding" in actions


def test_opening_balances_via_manual_journal(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    """Take-on/opening balances via a manual journal: BS balances, +equity."""
    client, key, _, _ = funded
    resp = client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "Opening balances 2026",
            "lines": [
                {"account_code": "1010", "debit": "10000"},  # opening cash
                {"account_code": "1400", "debit": "500"},  # opening input tax credit
                {"account_code": "3000", "credit": "10500"},  # contributed capital
            ],
        },
        headers=_headers(key),
    )
    assert resp.status_code == 201
    bs = client.get("/books/bs", headers=_headers(key)).json()
    sgd = _totals(bs)
    assert Decimal(str(sgd["total_assets"])) == Decimal("10500")
    assert Decimal(str(sgd["total_equity"])) == Decimal("10500")
    assert sgd["balanced"] is True


def test_manual_journal_periodises_by_posting_date(
    funded: tuple[TestClient, str, Company, Entity],
) -> None:
    """A manual journal's posting_date scopes it into the right period."""
    client, key, _, _ = funded
    client.post(
        "/books/journal",
        json={
            "entity": "sg",
            "memo": "future accrual",
            "posting_date": "2030-06-15",
            "lines": [
                {"account_code": "6090", "debit": "77"},
                {"account_code": "1010", "credit": "77"},
            ],
        },
        headers=_headers(key),
    )
    inside = client.get(
        "/books/tb?date_from=2030-01-01&date_to=2030-12-31", headers=_headers(key)
    ).json()
    assert Decimal(str(_totals(inside)["total_debit"])) == Decimal("77")

    outside = client.get("/books/tb?date_from=2031-01-01", headers=_headers(key)).json()
    assert outside["currencies"] == []
