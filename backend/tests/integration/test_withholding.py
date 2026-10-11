"""Integration tests for the India s.195 withholding gate.

An India entity paying a foreign (non-resident) vendor owes s.195 withholding
beside GST. Missing the treaty documentation (TRC / Form 10F) above the 15CB
threshold blocks the remittance at authorize (`human_hold`) — governance before
money moves; otherwise the withholding is recorded at capture (treaty vs domestic
rate). Rates are provisional (config) — confirm with a CA.
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
    WithholdingRecord,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_in_company(db_session: Session) -> tuple[Company, str]:
    """An India entity + a big-budget agent + a live admin key."""
    company = Company(name="Bharat AI", slug=f"in-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    entity = Entity(
        company_id=company.id,
        name="Bharat AI Pvt Ltd",
        code="in",
        full_name="Bharat AI Private Limited",
        currency="INR",
        accounting_std="Ind-AS",
        tax_regime="IN-GST",
        country="IN",
        gst_state="KA",
        timezone="Asia/Kolkata",
    )
    db_session.add(entity)
    db_session.flush()
    db_session.add(
        Agent(
            company_id=company.id,
            entity_id=entity.id,
            name="Research",
            slug="research",
            glyph="x",
            role="Research",
            monthly_budget=Decimal("100000000"),
        )
    )
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
    return company, plaintext


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _make_foreign_vendor(
    client: TestClient, key: str, name: str, *, with_docs: bool
) -> None:
    """A foreign (US) vendor, optionally with a valid TRC + Form 10F on file."""
    client.post(
        "/vendors",
        json={
            "name": name,
            "country": "US",
            "gst_registered_in": [],
            "trc_on_file": with_docs,
            "trc_expiry": "2027-03-31" if with_docs else None,
            "form_10f_on_file": with_docs,
        },
        headers=_headers(key),
    )


def _authorize(
    client: TestClient, key: str, *, vendor: str, amount: str
) -> dict[str, object]:
    return client.post(  # type: ignore[no-any-return]
        "/transactions/authorize",
        json={
            "agent": "research",
            "entity": "in",
            "vendor": vendor,
            "amount": amount,
            "currency": "INR",
            "reason": "remittance",
        },
        headers=_headers(key),
    ).json()


def _capture(client: TestClient, key: str, authorization_id: str) -> dict[str, object]:
    return client.post(  # type: ignore[no-any-return]
        "/transactions/capture",
        json={"authorization_id": authorization_id, "source": "sdk"},
        headers=_headers(key),
    ).json()


def _withholding(db: Session, tx_id: str) -> WithholdingRecord | None:
    return db.execute(
        select(WithholdingRecord).where(
            WithholdingRecord.transaction_id == uuid.UUID(tx_id)
        )
    ).scalar_one_or_none()


def test_authorize_blocks_missing_docs_above_threshold(
    client: TestClient, db_session: Session
) -> None:
    """IN→foreign, no TRC/10F, above the 15CB threshold → held before money moves."""
    company, key = _make_in_company(db_session)
    _make_foreign_vendor(client, key, "OpenAI", with_docs=False)
    verdict = _authorize(client, key, vendor="OpenAI", amount="600000")
    assert verdict["status"] == "human_hold"
    assert verdict["code"] == "withholding_docs_required"


def test_authorize_approves_with_valid_docs(
    client: TestClient, db_session: Session
) -> None:
    """With a valid TRC + Form 10F the treaty position is certified — not blocked."""
    company, key = _make_in_company(db_session)
    _make_foreign_vendor(client, key, "OpenAI", with_docs=True)
    verdict = _authorize(client, key, vendor="OpenAI", amount="600000")
    assert verdict["status"] == "approved"


def test_authorize_below_threshold_not_blocked(
    client: TestClient, db_session: Session
) -> None:
    """Below the 15CB threshold the domestic rate applies but the payment proceeds."""
    company, key = _make_in_company(db_session)
    _make_foreign_vendor(client, key, "OpenAI", with_docs=False)
    verdict = _authorize(client, key, vendor="OpenAI", amount="10000")
    assert verdict["status"] == "approved"


def test_capture_records_domestic_withholding(
    client: TestClient, db_session: Session
) -> None:
    """A captured IN→foreign remittance records the withholding (domestic rate)."""
    company, key = _make_in_company(db_session)
    _make_foreign_vendor(client, key, "OpenAI", with_docs=False)
    verdict = _authorize(client, key, vendor="OpenAI", amount="10000")
    body = _capture(client, key, str(verdict["authorization_id"]))
    wh = _withholding(db_session, str(body["id"]))
    assert wh is not None
    assert wh.rate_bps == 2000  # 20% domestic default
    assert wh.treaty_rate_applied is False
    assert wh.amount == Decimal("2000.0000")  # 10000 * 20%


def test_capture_records_treaty_withholding(
    client: TestClient, db_session: Session
) -> None:
    """With valid docs the capture records the lower DTAA treaty rate."""
    company, key = _make_in_company(db_session)
    _make_foreign_vendor(client, key, "OpenAI", with_docs=True)
    verdict = _authorize(client, key, vendor="OpenAI", amount="10000")
    body = _capture(client, key, str(verdict["authorization_id"]))
    wh = _withholding(db_session, str(body["id"]))
    assert wh is not None
    assert wh.rate_bps == 1500  # 15% treaty
    assert wh.treaty_rate_applied is True


def test_domestic_vendor_no_withholding(
    client: TestClient, db_session: Session
) -> None:
    """A domestic (India) vendor is not a s.195 payment — no withholding record."""
    company, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "Zoho", "country": "IN", "gst_registered_in": ["IN"]},
        headers=_headers(key),
    )
    verdict = _authorize(client, key, vendor="Zoho", amount="10000")
    body = _capture(client, key, str(verdict["authorization_id"]))
    assert _withholding(db_session, str(body["id"])) is None
