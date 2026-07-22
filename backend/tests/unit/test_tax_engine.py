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
        vendor_country="US", entity_country="IN",  # foreign vendor -> import
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
        vendor_country="US", entity_country="SG",  # foreign vendor -> import
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
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
    )
    assert t.tax_type == "GST"
    assert t.tax_amount == Decimal("18.00")  # 118 * 0.18 / 1.18
    assert t.reverse_charge is False
    assert t.self_invoice_required is False
    assert t.is_reclaimable is True


def test_us_unconfigured_state_is_exception() -> None:
    """US with no state rule → 0 tax + confidence=exception (routes to recon, TE-2)."""
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
    assert t.confidence == "exception"  # unconfigured → flagged, not a silent 0


def test_us_taxable_state_self_assesses_use_tax() -> None:
    """A taxable US state (rule standard) self-accrues use tax at the rate (TE-2)."""
    t = compute_tax(
        tax_regime="US-Sales-Tax", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="USD", rate=Decimal("0.05"),
        us_rule_type="standard", us_jurisdiction="US-TX",
    )
    assert t.tax_type == "SALES_TAX"
    assert t.tax_amount == Decimal("5.0000")  # 100 * 5%
    assert t.reverse_charge is True  # self-assessed payable (use tax)
    assert t.is_reclaimable is False  # US tax is a cost, not a VAT credit
    assert t.reclaim_amount == _ZERO
    assert t.self_invoice_required is False
    assert [c.head for c in t.components] == ["US_USE_TAX"]
    assert t.confidence == "auto"


def test_us_exempt_state_is_zero() -> None:
    """An exempt US state books no tax (TE-2)."""
    t = compute_tax(
        tax_regime="US-Sales-Tax", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="USD", rate=_ZERO,
        us_rule_type="exempt", us_jurisdiction="US-CA",
    )
    assert t.tax_amount == _ZERO
    assert t.reverse_charge is False
    assert t.confidence == "auto"


def test_us_disputed_state_routes_to_exception() -> None:
    """A disputed US state (VT) is never auto-applied — routed to recon (TE-2)."""
    t = compute_tax(
        tax_regime="US-Sales-Tax", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="USD", rate=Decimal("0.06"),
        us_rule_type="disputed", us_jurisdiction="US-VT",
    )
    assert t.tax_amount == _ZERO  # the 6% rate is NOT applied
    assert t.confidence == "exception"
    assert "disputed" in t.legal_reference


def test_imported_goods_route_to_customs_not_rcm() -> None:
    """A cross-border goods supply → customs, never services reverse charge (TE-4)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
        vendor_country="US", entity_country="IN", vendor_registered_in=(),
        supply_type="goods",
    )
    assert t.tax_type == "CUSTOMS"
    assert t.reverse_charge is False  # not services RCM
    assert t.tax_amount == _ZERO
    assert t.confidence == "exception"  # routed to manual customs handling


def test_domestic_goods_taxed_normally() -> None:
    """Domestic goods (same country) fall through to the normal treatment (TE-4)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
        supply_type="goods",
    )
    assert t.tax_type == "GST"  # domestic GST, not customs


def test_india_blocked_itc_for_travel() -> None:
    """IN travel/hotel spend still owes RCM GST but cannot reclaim it (T1-14)."""
    imported = compute_tax(
        tax_regime="IN-GST",
        functional_amount=Decimal("100"),
        paid_currency="USD",
        entity_currency="INR",
        rate=_IN_RATE,
        expense_category="travel",
        vendor_country="US", entity_country="IN",  # foreign vendor -> import
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
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
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
        vendor_country="US", entity_country="IN",  # foreign vendor -> import
    )
    assert t.is_reclaimable is True
    assert t.reclaim_amount == Decimal("18.0000")


def test_full_s17_5_set_blocks_itc_beyond_travel() -> None:
    """The full s.17(5) set blocks ITC — hotel, motor vehicles, F&B, etc. (TE-5)."""
    for category in (
        "hotel",
        "motor_vehicles",
        "food_beverage",
        "employee_benefits",
        "works_contract",
    ):
        t = compute_tax(
            tax_regime="IN-GST", functional_amount=Decimal("100"),
            paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
            vendor_country="US", entity_country="IN", vendor_registered_in=(),
            expense_category=category,
        )
        assert t.is_reclaimable is False, category  # GST owed but not reclaimable
        assert t.reclaim_amount == _ZERO, category
        assert "s.17(5)" in t.legal_reference


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
        vendor_country="US", entity_country="IN",  # foreign vendor -> import
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
        vendor_country="US", entity_country="SG",  # foreign vendor -> import
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
        vendor_country="IN", vendor_state="KA", vendor_registered_in=("IN",),
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
        vendor_country="IN", vendor_state="MH", vendor_registered_in=("IN",),
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is False
    assert [c.head for c in t.components] == ["IGST"]


def test_domestic_unregistered_vendor_is_reverse_charge() -> None:
    """India domestic from an unregistered vendor -> RCM under CGST s.9(4) (T1-3)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="KA", vendor_registered_in=(),
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is True  # buyer self-assesses
    assert t.tax_amount == Decimal("18.0000")  # on top (tax-exclusive), not embedded
    assert "s.9(4)" in t.legal_reference
    assert [c.head for c in t.components] == ["CGST", "SGST"]  # intra-state split


def test_foreign_vendor_registered_in_buyer_country_is_forward_charge() -> None:
    """OIDAR: a foreign vendor GST-registered in India charges forward IGST, not RCM.

    The key TE-1 case — registration in the buyer's country, not home country,
    decides. Currency (USD) and home country (US) both say "import"; the India
    registration makes it a domestic forward-charge IGST supply with reclaimable
    ITC and no self-invoice.
    """
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
        vendor_country="US", entity_country="IN",
        vendor_registered_in=("IN",),
    )
    assert t.reverse_charge is False
    assert t.self_invoice_required is False
    assert t.tax_type == "GST"
    assert [c.head for c in t.components] == ["IGST"]  # OIDAR charges IGST
    assert t.tax_amount == Decimal("18.00")  # extracted from inclusive 118
    assert t.is_reclaimable is True
    assert t.reclaim_amount == Decimal("18.00")


def test_domestic_vendor_registered_in_buyer_country_forward_charge() -> None:
    """TE-1 Case B: a domestic India vendor billing in USD is still domestic.

    Home country IN + registered in IN → forward-charge CGST/SGST despite the
    foreign invoice currency (which the old heuristic would have called an import).
    """
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="KA", vendor_registered_in=("IN",),
        entity_country="IN", entity_state="KA",
    )
    assert t.reverse_charge is False
    assert [c.head for c in t.components] == ["CGST", "SGST"]


def test_foreign_vendor_billing_local_currency_is_import_rcm() -> None:
    """TE-1 Case A: a foreign, IN-unregistered vendor billing in INR is still RCM.

    Currency (INR) says "domestic"; country (US) + no India registration correctly
    says imported → IGST reverse charge with a self-invoice.
    """
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="US", entity_country="IN",
        vendor_registered_in=(),
    )
    assert t.reverse_charge is True
    assert t.tax_type == "GST_RCM"
    assert t.self_invoice_required is True
    assert [c.head for c in t.components] == ["IGST"]


def test_unenriched_vendor_is_unclassified_not_guessed() -> None:
    """No vendor country -> conservative UNCLASSIFIED, never a currency guess (TE-1b).

    The old heuristic read USD != INR as "import" and self-assessed RCM. With the
    fallback retired, an unenriched vendor asserts nothing: no RCM, no ITC, no
    self-invoice, whole amount expensed, and ``confidence="exception"`` so the
    money path flags it for enrichment.
    """
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="USD", entity_currency="INR", rate=_IN_RATE,
        entity_country="IN",  # entity known but vendor country unknown
    )
    assert t.confidence == "exception"
    assert t.tax_type == "UNCLASSIFIED"
    assert t.reverse_charge is False  # no phantom RCM
    assert t.self_invoice_required is False  # no fabricated self-invoice
    assert t.tax_amount == Decimal("0")  # nothing asserted
    assert t.is_reclaimable is False  # no phantom ITC
    assert t.gross_amount == Decimal("100")  # whole amount expensed
    assert [c.head for c in t.components] == ["NONE"]


def test_classified_treatment_is_auto_confidence() -> None:
    """A normally-classified supply carries confidence="auto" (TE-1b)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", vendor_state="KA", vendor_registered_in=("IN",),
        entity_country="IN", entity_state="KA",
    )
    assert t.confidence == "auto"


# --- Tax-inclusive/exclusive + rate slabs (T1-10) ---------------------------


def test_domestic_tax_inclusive_extracts_gross_is_amount() -> None:
    """The inclusive default extracts embedded GST; gross == the paid amount."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("118"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
    )
    assert t.tax_amount == Decimal("18.00")
    assert t.gross_amount == Decimal("118")


def test_domestic_tax_exclusive_adds_gst_on_top() -> None:
    """A tax-exclusive amount adds GST on top — no phantom extraction (T1-10)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("100"),
        paid_currency="INR", entity_currency="INR", rate=_IN_RATE,
        tax_inclusive=False,
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
    )
    assert t.tax_amount == Decimal("18.0000")  # 100 * 0.18 on top, not extracted
    assert t.gross_amount == Decimal("118.0000")  # buyer paid net + GST
    assert t.reverse_charge is False


def test_slab_rate_extracts_at_that_rate() -> None:
    """A 5% slab extracts 5% embedded GST, not 18% (T1-10 wrong-rate fix)."""
    t = compute_tax(
        tax_regime="IN-GST", functional_amount=Decimal("105"),
        paid_currency="INR", entity_currency="INR", rate=Decimal("0.05"),
        vendor_country="IN", entity_country="IN", vendor_registered_in=("IN",),
    )
    assert t.tax_amount == Decimal("5.0000")  # 105 * 0.05/1.05, vs ~16 at 18%
