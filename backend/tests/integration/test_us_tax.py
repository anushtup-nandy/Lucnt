"""Integration tests for US per-state sales/use tax.

A US entity's spend is taxed by the buyer's **state** (the `tax_rates` per-state
rows seeded from Appendix A), not a blanket 0%: a taxable state self-accrues use
tax (a payable, never reclaimable), an exempt state books nothing, and a disputed
or unconfigured state routes to the recon exception queue rather than guessing.
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
    JournalEntry,
    ReconciliationException,
    ReconExceptionType,
    TaxRecord,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_key(db_session: Session, company: Company) -> str:
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
    db_session.flush()
    return plaintext


def _make_us_company(
    db_session: Session, *, us_state: str | None
) -> tuple[Company, str]:
    """A US entity (USD functional currency) with the given buyer state + a key."""
    company = Company(name="Acme US", slug=f"us-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    entity = Entity(
        company_id=company.id,
        name="Acme US Inc",
        code="us",
        full_name="Acme US Incorporated",
        currency="USD",
        accounting_std="US-GAAP",
        tax_regime="US-Sales-Tax",
        country="US",
        us_state=us_state,
        timezone="America/Los_Angeles",
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
            role="Ops",
            monthly_budget=Decimal("100000000"),
        )
    )
    db_session.flush()
    return company, _make_key(db_session, company)


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _spend(client: TestClient, key: str, *, amount: str) -> dict[str, object]:
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "ops",
            "entity": "us",
            "vendor": "somesaas",
            "amount": amount,
            "currency": "USD",
            "reason": "seat",
        },
        headers=_headers(key),
    ).json()
    return client.post(  # type: ignore[no-any-return]
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_headers(key),
    ).json()


def _tax_rows(db: Session, tx_id: str) -> list[TaxRecord]:
    return list(
        db.execute(
            select(TaxRecord).where(TaxRecord.transaction_id == uuid.UUID(tx_id))
        ).scalars()
    )


def _credit(db: Session, tx_id: str, code: str) -> Decimal:
    lines = db.execute(
        select(JournalEntry).where(
            JournalEntry.transaction_id == uuid.UUID(tx_id),
            JournalEntry.account_code == code,
        )
    ).scalars()
    return sum((ln.credit for ln in lines), Decimal("0"))


def _exceptions(db: Session, tx_id: str) -> list[ReconciliationException]:
    return list(
        db.execute(
            select(ReconciliationException).where(
                ReconciliationException.transaction_id == uuid.UUID(tx_id)
            )
        ).scalars()
    )


def test_taxable_state_tx_self_assesses_use_tax(
    client: TestClient, db_session: Session
) -> None:
    """TX (taxable, ~5.0% effective): use tax accrued to a payable, not reclaimable."""
    company, key = _make_us_company(db_session, us_state="TX")
    body = _spend(client, key, amount="100")
    tx_id = str(body["id"])

    assert body["tax"]["tax_type"] == "SALES_TAX"
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("5.0000")  # 100 * 5%
    assert body["tax"]["is_reclaimable"] is False
    assert body["tax"]["self_invoice_generated"] is False
    # Wallet pays the vendor 100; a US Use Tax Payable (2400) leg carries the 5.
    assert _credit(db_session, tx_id, "1010") == Decimal("100.0000")
    assert _credit(db_session, tx_id, "2400") == Decimal("5.0000")
    row = _tax_rows(db_session, tx_id)[0]
    assert row.tax_head == "US_USE_TAX"
    assert row.applied_rule_source == "seed:appendix-a"  # audit trail
    assert row.applied_rule_id is not None
    assert _exceptions(db_session, tx_id) == []  # confident → no flag


def test_exempt_state_ca_books_no_tax(client: TestClient, db_session: Session) -> None:
    """CA (exempt): zero tax, a plain Dr Expense / Cr Wallet pair, no flag."""
    company, key = _make_us_company(db_session, us_state="CA")
    body = _spend(client, key, amount="100")
    tx_id = str(body["id"])
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("0")
    assert _credit(db_session, tx_id, "1010") == Decimal("100.0000")
    assert _credit(db_session, tx_id, "2400") == Decimal("0")
    assert _exceptions(db_session, tx_id) == []


def test_split_state_ia_is_b2b_exempt(client: TestClient, db_session: Session) -> None:
    """IA (split state) resolves to its B2B branch — exempt, zero tax."""
    company, key = _make_us_company(db_session, us_state="IA")
    body = _spend(client, key, amount="100")
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("0")
    assert _exceptions(db_session, str(body["id"])) == []


def test_disputed_state_vt_routes_to_exception(
    client: TestClient, db_session: Session
) -> None:
    """VT (disputed): never auto-classified — 0 tax + a tax-review recon flag."""
    company, key = _make_us_company(db_session, us_state="VT")
    body = _spend(client, key, amount="100")
    tx_id = str(body["id"])
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("0")  # 6% NOT applied
    exc = _exceptions(db_session, tx_id)
    assert len(exc) == 1
    assert exc[0].exception_type == ReconExceptionType.TAX_UNCLASSIFIED
    assert exc[0].evidence_json["reason"] == "us_tax_review"


def test_unconfigured_state_routes_to_exception(
    client: TestClient, db_session: Session
) -> None:
    """A US entity with no state is flagged, not silently zero-taxed."""
    company, key = _make_us_company(db_session, us_state=None)
    body = _spend(client, key, amount="100")
    tx_id = str(body["id"])
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("0")
    exc = _exceptions(db_session, tx_id)
    assert len(exc) == 1
    assert exc[0].exception_type == ReconExceptionType.TAX_UNCLASSIFIED
    assert exc[0].evidence_json["reason"] == "us_tax_review"
