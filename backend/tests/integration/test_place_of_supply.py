"""Integration tests for place-of-supply tax + the vendor registry (T1-3/T1-9).

An India entity's spend is taxed by *place of supply* (the vendor registry), not
currency: an intra-state domestic supply splits into CGST + SGST, an import/inter-
state supply is IGST reverse charge, and an unenriched vendor falls back to the
currency heuristic (today's behaviour). Asserts the split lands in the ledger:
per-head tax records and per-head ITC/payable journal legs.
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
    Agent,
    ApiKey,
    ApiKeyMode,
    ApiKeyScope,
    Company,
    Entity,
    FxRate,
    JournalEntry,
    ReconciliationException,
    ReconExceptionStatus,
    ReconExceptionType,
    TaxRate,
    TaxRecord,
    Transaction,
)
from lucnt_api.security import generate_api_key, hash_api_key


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _make_key(
    db_session: Session, company: Company, *, scope: ApiKeyScope = ApiKeyScope.ADMIN
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


def _make_in_company(db_session: Session) -> tuple[Company, Entity, str]:
    """An India company (entity in Karnataka) + a clean USD->INR=100 rate + a key."""
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
    db_session.flush()  # populate entity.id before the agent references it
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
    # A clean rate: USD->INR = 100 (one INR is 0.01 USD), effective past the baseline.
    db_session.add(
        FxRate(
            currency="INR",
            usd_per_unit=Decimal("0.01"),
            effective_from=date(2026, 1, 2),
            source="test",
        )
    )
    db_session.flush()
    return company, entity, _make_key(db_session, company)


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _spend(
    client: TestClient,
    key: str,
    *,
    vendor: str,
    amount: str,
    currency: str,
    **extra: object,
) -> dict[str, object]:
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "research", "entity": "in", "vendor": vendor,
            "amount": amount, "currency": currency, "reason": "t", **extra,
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
        )
        .scalars()
        .all()
    )


def _debit(db: Session, tx_id: str, code: str) -> Decimal:
    lines = db.execute(
        select(JournalEntry).where(
            JournalEntry.transaction_id == uuid.UUID(tx_id),
            JournalEntry.account_code == code,
        )
    ).scalars()
    return sum((ln.debit for ln in lines), Decimal("0"))


def _credit(db: Session, tx_id: str, code: str) -> Decimal:
    lines = db.execute(
        select(JournalEntry).where(
            JournalEntry.transaction_id == uuid.UUID(tx_id),
            JournalEntry.account_code == code,
        )
    ).scalars()
    return sum((ln.credit for ln in lines), Decimal("0"))


def test_vendor_auto_created_unenriched_on_spend(
    client: TestClient, db_session: Session
) -> None:
    """A spend to an unknown vendor auto-creates an unregistered, unenriched row."""
    company, _, key = _make_in_company(db_session)
    _spend(client, key, vendor="MysteryCorp", amount="118", currency="INR")
    vendors = client.get("/vendors", headers=_headers(key)).json()
    row = next(v for v in vendors["items"] if v["name"] == "MysteryCorp")
    assert row["country"] is None
    assert row["is_registered"] is False


def test_vendor_crud_create_enrich_and_conflicts(
    client: TestClient, db_session: Session
) -> None:
    company, _, key = _make_in_company(db_session)
    created = client.post(
        "/vendors",
        json={
            "name": "Zoho", "country": "in", "state": "ka",
            "gst_registered_in": ["in"],
        },
        headers=_headers(key),
    )
    assert created.status_code == 201
    assert created.json()["country"] == "IN"  # normalised upper-case
    assert created.json()["gst_registered_in"] == ["IN"]
    assert created.json()["is_registered"] is True  # derived from the list

    # A duplicate (normalized) name is a 409 — enrich via PATCH instead.
    dup = client.post("/vendors", json={"name": "zoho"}, headers=_headers(key))
    assert dup.status_code == 409

    vendor_id = created.json()["id"]
    patched = client.patch(
        f"/vendors/{vendor_id}",
        json={"state": "MH", "gst_registered_in": []},
        headers=_headers(key),
    )
    assert patched.status_code == 200
    assert patched.json()["state"] == "MH"
    assert patched.json()["is_registered"] is False
    assert patched.json()["gst_registered_in"] == []
    assert patched.json()["country"] == "IN"  # untouched by the partial update

    # A runtime key cannot mutate the registry (T0-12).
    runtime = _make_key(db_session, company, scope=ApiKeyScope.RUNTIME)
    forbidden = client.post(
        "/vendors", json={"name": "Other"}, headers=_headers(runtime)
    )
    assert forbidden.status_code == 403


def test_vendor_withholding_docs_crud(
    client: TestClient, db_session: Session
) -> None:
    """TRC / Form 10F onboarding fields round-trip through create + patch (TE-3)."""
    company, _, key = _make_in_company(db_session)
    created = client.post(
        "/vendors",
        json={
            "name": "OpenAI", "country": "US",
            "trc_on_file": True, "trc_expiry": "2027-03-31",
            "form_10f_on_file": True,
        },
        headers=_headers(key),
    ).json()
    assert created["trc_on_file"] is True
    assert created["trc_expiry"] == "2027-03-31"
    assert created["form_10f_on_file"] is True

    patched = client.patch(
        f"/vendors/{created['id']}",
        json={"form_10f_on_file": False},
        headers=_headers(key),
    ).json()
    assert patched["form_10f_on_file"] is False
    assert patched["trc_on_file"] is True  # untouched by the partial update


def test_intra_state_domestic_splits_cgst_sgst(
    client: TestClient, db_session: Session
) -> None:
    """A registered same-state vendor -> CGST + SGST: two tax rows, two ITC legs."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={
            "name": "Zoho", "country": "IN", "state": "KA",
            "gst_registered_in": ["IN"],
        },
        headers=_headers(key),
    )
    body = _spend(client, key, vendor="Zoho", amount="118", currency="INR")
    tx_id = str(body["id"])

    heads = {r.tax_head: r for r in _tax_rows(db_session, tx_id)}
    assert set(heads) == {"CGST", "SGST"}
    assert heads["CGST"].tax_amount == Decimal("9.0000")
    assert heads["SGST"].tax_amount == Decimal("9.0000")
    assert all(r.self_invoice_generated is False for r in heads.values())  # domestic

    # Two per-head ITC legs (1410 CGST, 1420 SGST); no payable (forward charge).
    assert _debit(db_session, tx_id, "1410") == Decimal("9.0000")
    assert _debit(db_session, tx_id, "1420") == Decimal("9.0000")
    assert _debit(db_session, tx_id, "1400") == Decimal("0")  # legacy unused


def test_tax_record_stamps_applied_rule(
    client: TestClient, db_session: Session
) -> None:
    """Each tax record links to the tax_rates row that produced it (TE-6)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={
            "name": "Zoho", "country": "IN", "state": "KA",
            "gst_registered_in": ["IN"],
        },
        headers=_headers(key),
    )
    body = _spend(client, key, vendor="Zoho", amount="118", currency="INR")
    rows = _tax_rows(db_session, str(body["id"]))
    assert rows  # CGST + SGST
    assert all(r.applied_rule_id is not None for r in rows)
    assert all(r.applied_rule_source == "seed" for r in rows)
    # The linked rule is the IN-GST standard 18% rate row.
    rule = db_session.get(TaxRate, rows[0].applied_rule_id)
    assert rule is not None
    assert rule.regime == "IN-GST"
    assert rule.rate == Decimal("0.18")


def test_import_books_igst_reverse_charge(
    client: TestClient, db_session: Session
) -> None:
    """A foreign vendor -> IGST reverse charge, booked to the IGST accounts (T1-9)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "OpenAI", "country": "US", "gst_registered_in": ["US"]},
        headers=_headers(key),
    )
    # USD 100 -> INR 10000 @ the clean rate; IGST 18% on top = 1800.
    body = _spend(client, key, vendor="OpenAI", amount="100", currency="USD")
    tx_id = str(body["id"])

    rows = _tax_rows(db_session, tx_id)
    assert [r.tax_head for r in rows] == ["IGST"]
    assert rows[0].tax_amount == Decimal("1800.0000")
    assert rows[0].self_invoice_generated is True  # RCM self-invoice
    assert _debit(db_session, tx_id, "1430") == Decimal("1800.0000")  # ITC IGST
    assert _credit(db_session, tx_id, "2230") == Decimal("1800.0000")  # payable IGST


def test_oidar_vendor_registered_in_india_is_forward_charge(
    client: TestClient, db_session: Session
) -> None:
    """A foreign vendor GST-registered in India books forward-charge IGST (TE-1).

    Registration in the buyer's country, not home country, decides: an ITC IGST
    leg (1430) is booked, but *no* payable (2230) and no self-invoice — the OIDAR
    supplier charged the tax forward. Home country (US) and currency (USD) both
    say "import", yet the India registration makes it domestic forward charge.
    """
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "OpenAI", "country": "US", "gst_registered_in": ["IN"]},
        headers=_headers(key),
    )
    body = _spend(client, key, vendor="OpenAI", amount="118", currency="USD")
    tx_id = str(body["id"])

    rows = _tax_rows(db_session, tx_id)
    assert [r.tax_head for r in rows] == ["IGST"]
    assert rows[0].self_invoice_generated is False  # forward charge, no self-invoice
    # USD 118 -> INR 11800; IGST extracted from the inclusive amount = 1800.
    assert rows[0].tax_amount == Decimal("1800.0000")
    assert _debit(db_session, tx_id, "1430") == Decimal("1800.0000")  # ITC IGST
    assert _credit(db_session, tx_id, "2230") == Decimal("0")  # no payable (forward)


def test_imported_goods_route_to_customs_not_rcm(
    client: TestClient, db_session: Session
) -> None:
    """A cross-border goods spend routes to customs + a review flag, not RCM (TE-4)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "HardwareCo", "country": "US", "gst_registered_in": []},
        headers=_headers(key),
    )
    body = _spend(
        client, key, vendor="HardwareCo", amount="100", currency="USD",
        supply_type="goods",
    )
    tx_id = str(body["id"])
    assert body["tax"]["tax_type"] == "CUSTOMS"
    assert body["tax"]["reverse_charge"] is False  # not services RCM
    assert body["tax"]["self_invoice_generated"] is False
    assert Decimal(body["tax"]["tax_amount"]) == Decimal("0")
    exc = _exceptions_for(db_session, tx_id)
    assert len(exc) == 1
    assert exc[0].evidence_json["reason"] == "goods_customs_review"


def test_domestic_goods_use_normal_gst(
    client: TestClient, db_session: Session
) -> None:
    """Domestic goods (same country) are taxed as normal GST, not customs (TE-4)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "LocalHW", "country": "IN", "gst_registered_in": ["IN"]},
        headers=_headers(key),
    )
    body = _spend(
        client, key, vendor="LocalHW", amount="118", currency="INR",
        supply_type="goods",
    )
    assert body["tax"]["tax_type"] == "GST"  # domestic GST, not CUSTOMS
    assert _exceptions_for(db_session, str(body["id"])) == []


def test_case_a_foreign_vendor_billing_inr_is_rcm(
    client: TestClient, db_session: Session
) -> None:
    """TE-1 Case A: a foreign vendor billing in INR is still imported RCM (money path).

    The exact P0 the currency heuristic got wrong — INR billing said "domestic",
    silently dropping the RCM liability. Place of supply (US vendor, not registered
    in IN) correctly books IGST reverse charge with a self-invoice.
    """
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "ForeignCorp", "country": "US", "gst_registered_in": []},
        headers=_headers(key),
    )
    # Billed in the buyer's own currency — the old heuristic's false-negative case.
    body = _spend(client, key, vendor="ForeignCorp", amount="118", currency="INR")
    tx_id = str(body["id"])
    assert body["tax"]["tax_type"] == "GST_RCM"
    assert body["tax"]["reverse_charge"] is True
    assert body["tax"]["self_invoice_generated"] is True
    rows = _tax_rows(db_session, tx_id)
    assert [r.tax_head for r in rows] == ["IGST"]
    assert rows[0].tax_amount == Decimal("21.2400")  # 118 * 0.18 on top


def test_unenriched_vendor_is_unclassified_and_flagged(
    client: TestClient, db_session: Session
) -> None:
    """An unenriched vendor is booked conservatively and flagged for review (TE-1b).

    No ITC leg, no self-invoice, the whole amount expensed, tax_type UNCLASSIFIED —
    and a `tax_unclassified` recon exception opened, never a silent currency guess.
    """
    company, _, key = _make_in_company(db_session)
    body = _spend(client, key, vendor="LocalCorp", amount="118", currency="INR")
    tx_id = str(body["id"])

    assert body["tax"]["tax_type"] == "UNCLASSIFIED"
    assert body["tax"]["self_invoice_generated"] is False
    rows = _tax_rows(db_session, tx_id)
    assert [r.tax_head for r in rows] == ["NONE"]
    assert rows[0].tax_amount == Decimal("0")
    assert _debit(db_session, tx_id, "1400") == Decimal("0")  # no phantom ITC
    assert _credit(db_session, tx_id, "1010") == Decimal("118.0000")  # whole amount out

    # The money path opened a recon exception for the unclassified vendor.
    exc = db_session.execute(
        select(ReconciliationException).where(
            ReconciliationException.transaction_id == uuid.UUID(tx_id)
        )
    ).scalar_one()
    assert exc.exception_type == ReconExceptionType.TAX_UNCLASSIFIED
    assert exc.status == ReconExceptionStatus.OPEN
    assert exc.amount_at_stake == Decimal("0")  # a config gap, not a recovery


def _exceptions_for(db: Session, tx_id: str) -> list[ReconciliationException]:
    return list(
        db.execute(
            select(ReconciliationException).where(
                ReconciliationException.transaction_id == uuid.UUID(tx_id)
            )
        ).scalars()
    )


def test_enrichment_reflags_divergent_history_without_rewriting(
    client: TestClient, db_session: Session
) -> None:
    """Correcting a vendor flags its mis-booked past spends; history stays put (TE-1c).

    A spend is booked domestic GST against a vendor recorded as India-registered.
    The vendor is later corrected to a foreign, unregistered supplier (it always
    owed reverse charge). The historical spend is flagged for review — but its
    booked journal/tax records are never rewritten.
    """
    company, _, key = _make_in_company(db_session)
    created = client.post(
        "/vendors",
        json={
            "name": "Acme", "country": "IN", "state": "KA",
            "gst_registered_in": ["IN"],
        },
        headers=_headers(key),
    ).json()
    body = _spend(client, key, vendor="Acme", amount="118", currency="INR")
    tx_id = str(body["id"])
    assert body["tax"]["tax_type"] == "GST"  # booked domestic
    assert _exceptions_for(db_session, tx_id) == []  # nothing flagged yet

    # Correct the vendor: it is actually a foreign, unregistered supplier (RCM).
    client.patch(
        f"/vendors/{created['id']}",
        json={"country": "US", "gst_registered_in": []},
        headers=_headers(key),
    )

    exc = _exceptions_for(db_session, tx_id)
    assert len(exc) == 1
    assert exc[0].exception_type == ReconExceptionType.TAX_UNCLASSIFIED
    assert exc[0].evidence_json["reason"] == "vendor_reclassified"
    assert exc[0].evidence_json["booked_tax_type"] == "GST"
    assert exc[0].evidence_json["recomputed_tax_type"] == "GST_RCM"

    # History is immutable: the booked tax records are still the domestic GST split.
    rows = _tax_rows(db_session, tx_id)
    assert {r.tax_head for r in rows} == {"CGST", "SGST"}
    assert all(r.tax_type == "GST" for r in rows)


def test_enrichment_does_not_duplicate_an_existing_flag(
    client: TestClient, db_session: Session
) -> None:
    """Enriching an unclassified vendor keeps its one flag — no duplicate (TE-1c)."""
    company, _, key = _make_in_company(db_session)
    body = _spend(client, key, vendor="Mystery", amount="118", currency="INR")
    tx_id = str(body["id"])
    assert body["tax"]["tax_type"] == "UNCLASSIFIED"
    assert len(_exceptions_for(db_session, tx_id)) == 1  # flagged at capture

    vendors = client.get("/vendors", headers=_headers(key)).json()
    vid = next(v["id"] for v in vendors["items"] if v["name"] == "Mystery")
    client.patch(
        f"/vendors/{vid}",
        json={"country": "US", "gst_registered_in": []},
        headers=_headers(key),
    )

    # The capture-time flag already covers this transaction — still exactly one.
    assert len(_exceptions_for(db_session, tx_id)) == 1


def test_tax_exclusive_books_gst_on_top(
    client: TestClient, db_session: Session
) -> None:
    """A tax-exclusive domestic spend books GST on top: wallet = amount + tax."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "LocalCorp", "country": "IN", "gst_registered_in": ["IN"]},
        headers=_headers(key),
    )
    body = _spend(
        client, key, vendor="LocalCorp", amount="100", currency="INR",
        tax_inclusive=False,
    )
    tx_id = str(body["id"])
    # 100 net + 18 GST: wallet out 118, ITC 18, expense 100.
    assert _credit(db_session, tx_id, "1010") == Decimal("118.0000")  # wallet gross
    assert _debit(db_session, tx_id, "1400") == Decimal("18.0000")  # ITC (legacy head)
    rows = _tax_rows(db_session, tx_id)
    assert rows[0].tax_amount == Decimal("18.0000")


def test_rate_slab_taxes_at_that_rate(
    client: TestClient, db_session: Session
) -> None:
    """A `tax_category` slab books at that GST rate, not the 18% default (T1-10)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={"name": "LocalCorp", "country": "IN", "gst_registered_in": ["IN"]},
        headers=_headers(key),
    )
    body = _spend(
        client, key, vendor="LocalCorp", amount="105", currency="INR",
        tax_category="gst_5",
    )
    tx_id = str(body["id"])
    rows = _tax_rows(db_session, tx_id)
    assert rows[0].rate == Decimal("0.05")
    assert rows[0].tax_amount == Decimal("5.0000")  # 105 * 0.05/1.05
    assert _debit(db_session, tx_id, "1400") == Decimal("5.0000")


def test_unknown_slab_fails_closed(
    client: TestClient, db_session: Session
) -> None:
    """An unconfigured slab is a clean 422 at capture, never a guessed rate."""
    company, _, key = _make_in_company(db_session)
    verdict = client.post(
        "/transactions/authorize",
        json={
            "agent": "research", "entity": "in", "vendor": "LocalCorp",
            "amount": "100", "currency": "INR", "reason": "t",
            "tax_category": "gst_99",
        },
        headers=_headers(key),
    ).json()
    resp = client.post(
        "/transactions/capture",
        json={"authorization_id": verdict["authorization_id"], "source": "sdk"},
        headers=_headers(key),
    )
    assert resp.status_code == 422


def test_reversal_negates_each_head_component(
    client: TestClient, db_session: Session
) -> None:
    """Reversing a split supply writes a negated contra row per head (T1-9)."""
    company, _, key = _make_in_company(db_session)
    client.post(
        "/vendors",
        json={
            "name": "Zoho", "country": "IN", "state": "KA",
            "gst_registered_in": ["IN"],
        },
        headers=_headers(key),
    )
    body = _spend(client, key, vendor="Zoho", amount="118", currency="INR")
    tx_id = str(body["id"])
    client.post(f"/transactions/{tx_id}/reverse", headers=_headers(key))

    reversal = db_session.execute(
        select(Transaction).where(
            Transaction.reverses_transaction_id == uuid.UUID(tx_id)
        )
    ).scalar_one()
    contra = {r.tax_head: r for r in _tax_rows(db_session, str(reversal.id))}
    assert set(contra) == {"CGST", "SGST"}
    assert contra["CGST"].tax_amount == Decimal("-9.0000")
    assert contra["SGST"].tax_amount == Decimal("-9.0000")


# --- Return-cycle GST settlement (T1-11) -------------------------------------


def _leg(lines: list[dict[str, object]], code: str, side: str) -> Decimal:
    return sum(
        (Decimal(str(ln[side])) for ln in lines if ln["account_code"] == code),
        Decimal("0"),
    )


def test_tax_settlement_nets_output_against_itc(
    client: TestClient, db_session: Session
) -> None:
    """Settlement clears per-head Payable against ITC, net to Government (T1-11)."""
    company, _, key = _make_in_company(db_session)
    for name in ("OpenAI", "Expedia"):
        client.post(
            "/vendors",
            json={"name": name, "country": "US", "gst_registered_in": ["US"]},
            headers=_headers(key),
        )
    # Import IGST 1800 reclaimable + import IGST 1800 blocked (travel, no ITC).
    _spend(client, key, vendor="OpenAI", amount="100", currency="USD")
    _spend(
        client, key, vendor="Expedia", amount="100", currency="USD", category="travel"
    )

    resp = client.post(
        "/books/tax-settlement",
        json={"entity": "in", "memo": "Q1 GST"},
        headers=_headers(key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert Decimal(body["total_output_tax"]) == Decimal("3600.0000")
    assert Decimal(body["total_input_credit"]) == Decimal("1800.0000")
    assert Decimal(body["net_payable"]) == Decimal("1800.0000")
    assert Decimal(body["credit_carried_forward"]) == Decimal("0")

    lines = body["journal"]["lines"]
    assert _leg(lines, "2230", "debit") == Decimal("3600.0000")  # clear IGST payable
    assert _leg(lines, "1430", "credit") == Decimal("1800.0000")  # utilise IGST ITC
    assert _leg(lines, "2300", "credit") == Decimal("1800.0000")  # net to Government

    # Idempotent: re-settling the same (now-cleared) period is a no-op.
    again = client.post(
        "/books/tax-settlement", json={"entity": "in"}, headers=_headers(key)
    ).json()
    assert again["journal"] is None
    assert Decimal(again["net_payable"]) == Decimal("0")


def test_tax_settlement_noop_when_no_gst(
    client: TestClient, db_session: Session
) -> None:
    """Settling an entity with no accumulated GST books nothing."""
    company, _, key = _make_in_company(db_session)
    resp = client.post(
        "/books/tax-settlement", json={"entity": "in"}, headers=_headers(key)
    )
    assert resp.status_code == 201
    assert resp.json()["journal"] is None
    assert Decimal(resp.json()["net_payable"]) == Decimal("0")
