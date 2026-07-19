"""Unit tests for the pure tax engine (no DB).

The statutory ``rate`` is injected (T1-12) — resolved from the effective-dated
`tax_rates` table on the money path, so these unit tests state the rate under
test explicitly. Effective-dated resolution itself is covered in
`tests/integration/test_tax_rates.py`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from lucnt_api.services.tax_engine import UnknownTaxRegimeError, compute_tax

_IN_RATE = Decimal("0.18")
_SG_RATE = Decimal("0.09")
_ZERO = Decimal("0")


def test_india_imported_service_is_reverse_charge() -> None:
    """A USD spend by an INR entity is imported -> 18% RCM on top, reclaimable."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="INR",
        rate=_IN_RATE,
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
        rate=_SG_RATE,
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
        rate=_IN_RATE,
    )
    assert t.tax_type == "GST"
    assert t.tax_amount == Decimal("18.00")  # 118 * 0.18 / 1.18
    assert t.reverse_charge is False
    assert t.self_invoice_required is False
    assert t.is_reclaimable is True


def test_us_sales_tax_self_assesses_nothing() -> None:
    """US regime books no self-assessed tax in the MVP (rate injected but ignored)."""
    t = compute_tax(
        tax_regime="US-Sales-Tax",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="USD",
        rate=_ZERO,
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
        rate=_IN_RATE,
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
        rate=_IN_RATE,
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
        rate=_IN_RATE,
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
            rate=_ZERO,
        )


def test_tax_is_carried_at_four_places() -> None:
    """Tax is carried at the ledger's 4dp scale (T1-13): 12.345 * 0.18 = 2.2221."""
    t = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("12.345"),
        paid_currency="USD",
        entity_currency="INR",
        rate=_IN_RATE,
    )
    assert t.tax_amount == Decimal("2.2221")


def test_injected_rate_overrides_regime_default() -> None:
    """The engine applies whatever rate it is given (T1-12) — e.g. SG at 8%."""
    t = compute_tax(
        tax_regime="SG-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="SGD",
        rate=Decimal("0.08"),  # the pre-2024 SG rate
    )
    assert t.rate == Decimal("0.08")
    assert t.tax_amount == Decimal("8.0000")


# --- Place of supply + CGST/SGST/IGST split (T1-3 / T1-9) --------------------


def test_place_of_supply_import_is_igst_rcm() -> None:
    """India entity, foreign vendor -> import -> RCM, single IGST component (T1-3)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="US", entity_country="IN",
    )
    # Decided by place of supply, not currency (paid INR would say "domestic").
    assert t.reverse_charge is True
    assert t.tax_type == "GST_RCM"
    assert [c.head for c in t.components] == ["IGST"]
    assert t.components[0].tax_amount == Decimal("18.0000")  # on top


def test_place_of_supply_intra_state_splits_cgst_sgst() -> None:
    """India intra-state domestic -> CGST + SGST, each half, summing to total (T1-9)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="KA", vendor_registered=True,
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is False
    assert [c.head for c in t.components] == ["CGST", "SGST"]
    assert t.components[0].tax_amount == Decimal("9.0000")
    assert t.components[1].tax_amount == Decimal("9.0000")
    assert sum((c.tax_amount for c in t.components), Decimal("0")) == t.tax_amount


def test_place_of_supply_inter_state_is_igst() -> None:
    """India inter-state domestic -> single IGST component (T1-9)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="MH", vendor_registered=True,
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is False
    assert [c.head for c in t.components] == ["IGST"]


def test_domestic_unregistered_vendor_is_reverse_charge() -> None:
    """India domestic from an unregistered vendor -> RCM under CGST s.9(4) (T1-3)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="KA", vendor_registered=False,
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is True  # buyer self-assesses
    assert t.tax_amount == Decimal("18.0000")  # on top (tax-exclusive), not embedded
    assert "s.9(4)" in t.legal_reference
    assert [c.head for c in t.components] == ["CGST", "SGST"]  # intra-state split


def test_currency_fallback_when_vendor_country_unknown() -> None:
    """No vendor country -> currency heuristic + single unsplit GST head (unchanged)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
        entity_country="IN",  # entity known but vendor country unknown -> fallback
    )
    assert t.reverse_charge is True  # USD != INR -> import by currency
    assert [c.head for c in t.components] == ["GST"]  # legacy unsplit head


# --- Tax-inclusive/exclusive + rate slabs (T1-10) ---------------------------


def test_domestic_tax_inclusive_extracts_gross_is_amount() -> None:
    """The inclusive default extracts embedded GST; gross == the paid amount."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
    )
    assert t.tax_amount == Decimal("18.00")
    assert t.gross_amount == Decimal("118")


def test_domestic_tax_exclusive_adds_gst_on_top() -> None:
    """A tax-exclusive amount adds GST on top — no phantom extraction (T1-10)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        tax_inclusive=False,
    )
    assert t.tax_amount == Decimal("18.0000")  # 100 * 0.18 on top, not extracted
    assert t.gross_amount == Decimal("118.0000")  # buyer paid net + GST
    assert t.reverse_charge is False


def test_slab_rate_extracts_at_that_rate() -> None:
    """A 5% slab extracts 5% embedded GST, not 18% (T1-10 wrong-rate fix)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("105"),
        paid_currency="INR", entity_currency="INR", rate=Decimal("0.05"),
    )
    assert t.tax_amount == Decimal("5.0000")  # 105 * 0.05/1.05, vs ~16 at 18%
