"""Unit tests for the pure tax engine (no DB)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lucnt_api.services.tax_engine import UnknownTaxRegimeError, compute_tax


def test_india_imported_service_is_reverse_charge() -> None:
    """A USD spend by an INR entity is imported -> 18% RCM on top, reclaimable."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="INR",
    )
    assert t.tax_type == "GST_RCM"
    assert t.rate == Decimal("0.18")
    assert t.tax_amount == Decimal("18.00")  # on top of the paid amount
    assert t.is_reclaimable is True
    assert t.reclaim_amount == Decimal("18.00")
    assert t.reverse_charge is True
    assert t.self_invoice_required is True
    assert t.currency == "INR"  # tax is denominated in the functional currency


def test_singapore_imported_service_is_reverse_charge() -> None:
    """A USD spend by an SGD entity is imported -> 9% RCM."""
    t = compute_tax(
        tax_regime="SG-GST",
        functional_amount=Decimal("200"),
        paid_currency="USD",
        entity_currency="SGD",
    )
    assert t.tax_type == "GST_RCM"
    assert t.rate == Decimal("0.09")
    assert t.tax_amount == Decimal("18.00")
    assert t.reverse_charge is True


def test_domestic_gst_is_extracted_from_inclusive_amount() -> None:
    """A domestic (same-currency) spend has GST embedded: 118 incl. -> 18 tax."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("118"),
        paid_currency="INR",
        entity_currency="INR",
    )
    assert t.tax_type == "GST"
    assert t.tax_amount == Decimal("18.00")  # 118 * 0.18 / 1.18
    assert t.reverse_charge is False
    assert t.self_invoice_required is False
    assert t.is_reclaimable is True


def test_us_sales_tax_self_assesses_nothing() -> None:
    """US regime books no self-assessed tax in the MVP."""
    t = compute_tax(
        tax_regime="US-Sales-Tax",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="USD",
    )
    assert t.tax_type == "SALES_TAX"
    assert t.tax_amount == Decimal("0")
    assert t.is_reclaimable is False
    assert t.reverse_charge is False
    assert t.currency == "USD"


def test_india_blocked_itc_for_travel() -> None:
    """IN travel/hotel spend still owes RCM GST but cannot reclaim it (T1-14)."""
    imported = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="INR",
        expense_category="travel",
    )
    assert imported.reverse_charge is True  # output GST still self-assessed
    assert imported.tax_amount == Decimal("18.0000")
    assert imported.is_reclaimable is False  # but input credit is barred
    assert imported.reclaim_amount == Decimal("0")
    assert "s.17(5)" in imported.legal_reference

    domestic = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("118"),
        paid_currency="INR",
        entity_currency="INR",
        expense_category="travel",
    )
    assert domestic.is_reclaimable is False
    assert domestic.reclaim_amount == Decimal("0")


def test_non_blocked_category_still_reclaims() -> None:
    """A non-blocked category (software) reclaims ITC as normal (T1-14)."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="INR",
        expense_category="software",
    )
    assert t.is_reclaimable is True
    assert t.reclaim_amount == Decimal("18.0000")


def test_unknown_regime_raises() -> None:
    """An unrecognised regime fails loud (T1-8), never silently books zero tax."""
    with pytest.raises(UnknownTaxRegimeError):
        compute_tax(
            tax_regime="XX-Unknown",
            functional_amount=Decimal("50"),
            paid_currency="EUR",
            entity_currency="EUR",
        )


def test_tax_is_carried_at_four_places() -> None:
    """Tax is carried at the ledger's 4dp scale (T1-13): 12.345 * 0.18 = 2.2221."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("12.345"),
        paid_currency="USD",
        entity_currency="INR",
    )
    assert t.tax_amount == Decimal("2.2221")
