"""Jurisdiction-aware tax engine (IN GST, SG GST, US sales/use tax).

Pure: returns a `TaxTreatment`. Tax is computed on the functional-currency amount;
import vs domestic comes from the vendor registry, never currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# 4dp ledger scale; 2dp rounding only at return/invoice aggregation so
# token-scale micro-amounts don't vanish to zero.
_FOUR_PLACES = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")

# Unknown regimes must fail loud, never silently book zero tax.
KNOWN_TAX_REGIMES: frozenset[str] = frozenset({"IN-GST", "SG-GST", "US-Sales-Tax"})

# ITC blocked under CGST s.17(5): the GST becomes a cost. Keyed by expense
# category, not rate slab, so it does not belong in `tax_rates`.
_BLOCKED_ITC_CATEGORIES: dict[str, frozenset[str]] = {
    "IN-GST": frozenset(
        {
            "travel",
            "hotel",
            "motor_vehicles",
            "food_beverage",
            "employee_benefits",
            "works_contract",
        }
    ),
}
_BLOCKED_ITC_NOTE = " — input tax credit blocked under CGST s.17(5)"


class UnknownTaxRegimeError(Exception):
    """The entity's `tax_regime` is not one the engine understands."""

    def __init__(self, regime: str) -> None:
        """Record the offending regime."""
        self.regime = regime
        super().__init__(
            f"unknown tax regime {regime!r}; expected one of "
            f"{sorted(KNOWN_TAX_REGIMES)}"
        )


@dataclass(frozen=True)
class TaxComponent:
    """One tax head of a treatment, persisted as its own `TaxRecord`.

    Components sum exactly to the treatment totals so journal and GSTR-3B agree.
    """

    head: str  # CGST | SGST | IGST | GST | NONE
    rate: Decimal
    tax_amount: Decimal
    reclaim_amount: Decimal


@dataclass(frozen=True)
class TaxTreatment:
    """Tax verdict for one transaction; summary fields aggregate ``components``."""

    tax_type: str  # GST_RCM | GST | SALES_TAX | NONE
    jurisdiction: str
    rate: Decimal
    tax_amount: Decimal
    is_reclaimable: bool
    reclaim_amount: Decimal
    legal_reference: str
    reverse_charge: bool
    self_invoice_required: bool
    currency: str  # always the entity's functional currency
    # Wallet cash out; ``functional + tax`` only for tax-exclusive domestic supply.
    gross_amount: Decimal
    components: tuple[TaxComponent, ...]
    # "exception" ⇒ conservative booking + recon exception (unknown supply place).
    confidence: str = "auto"


@dataclass(frozen=True)
class _RegimeRule:
    """Constant per-regime metadata; the rate is effective-dated and injected."""

    jurisdiction: str
    legal_reference_imported: str
    legal_reference_domestic: str


_REGIMES: dict[str, _RegimeRule] = {
    "IN-GST": _RegimeRule(
        jurisdiction="India",
        legal_reference_imported=(
            "IGST Act 2017 s.5(3) — reverse charge on imported services; "
            "self-invoice per CGST Rules r.46"
        ),
        legal_reference_domestic=(
            "CGST/SGST Act 2017 — domestic supply; input tax credit per s.16"
        ),
    ),
    "SG-GST": _RegimeRule(
        jurisdiction="Singapore",
        legal_reference_imported=(
            "GST Act (Cap. 117A) s.14 — reverse charge on imported services"
        ),
        legal_reference_domestic=(
            "GST Act (Cap. 117A) — standard-rated domestic supply; input tax per s.19"
        ),
    ),
}


def _quantize(amount: Decimal) -> Decimal:
    return amount.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _is_itc_blocked(tax_regime: str, expense_category: str | None) -> bool:
    if expense_category is None:
        return False
    blocked = _BLOCKED_ITC_CATEGORIES.get(tax_regime, frozenset())
    return expense_category.strip().lower() in blocked


def _head_mode(
    *,
    tax_regime: str,
    imported: bool,
    place_of_supply_known: bool,
    vendor_state: str | None,
    entity_state: str | None,
) -> str:
    """GST head split: ``"GST"`` | ``"IGST"`` | ``"CGST_SGST"`` (India only splits).

    Imports are IGST; same-state domestic is CGST+SGST; unknown state stays "GST".
    """
    if not place_of_supply_known or tax_regime != "IN-GST":
        return "GST"
    if imported:
        return "IGST"
    if vendor_state is not None and entity_state is not None:
        same_state = vendor_state.strip().upper() == entity_state.strip().upper()
        return "CGST_SGST" if same_state else "IGST"
    return "GST"


def _components(
    head_mode: str, rate: Decimal, tax_amount: Decimal, reclaim_amount: Decimal
) -> tuple[TaxComponent, ...]:
    """Split totals into heads; SGST takes the rounding remainder so sums are exact."""
    if head_mode == "CGST_SGST":
        cgst_tax = _quantize(tax_amount / _TWO)
        cgst_reclaim = _quantize(reclaim_amount / _TWO)
        half_rate = rate / _TWO
        return (
            TaxComponent("CGST", half_rate, cgst_tax, cgst_reclaim),
            TaxComponent(
                "SGST", half_rate, tax_amount - cgst_tax, reclaim_amount - cgst_reclaim
            ),
        )
    head = "IGST" if head_mode == "IGST" else "GST"
    return (TaxComponent(head, rate, tax_amount, reclaim_amount),)


def _customs_treatment(
    functional_amount: Decimal, entity_currency: str
) -> TaxTreatment:
    """Cross-border goods: customs is not automated, so book no tax and flag it."""
    return TaxTreatment(
        tax_type="CUSTOMS",
        jurisdiction="Customs (imported goods)",
        rate=_ZERO,
        tax_amount=_ZERO,
        is_reclaimable=False,
        reclaim_amount=_ZERO,
        legal_reference=(
            "imported goods — customs duty / border tax applies; manual handling "
            "(customs path not automated)"
        ),
        reverse_charge=False,
        self_invoice_required=False,
        currency=entity_currency,
        gross_amount=functional_amount,
        components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
        confidence="exception",
    )


def _us_treatment(
    *,
    functional_amount: Decimal,
    entity_currency: str,
    rate: Decimal,
    us_rule_type: str | None,
    us_jurisdiction: str | None,
) -> TaxTreatment:
    """US sales/use tax for the buyer's state.

    Unconfigured/disputed → exception; exempt/no_tax → zero; standard/use_tax →
    self-assessed, non-reclaimable use tax booked like reverse charge.
    """
    state = us_jurisdiction or "US"

    if us_rule_type is None or us_rule_type == "disputed":
        reason = (
            f"US {state}: sales-tax status disputed — confirm before applying"
            if us_rule_type == "disputed"
            else f"US {state}: no sales/use-tax rule configured for this state"
        )
        return TaxTreatment(
            tax_type="SALES_TAX",
            jurisdiction=state,
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=reason,
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
            gross_amount=functional_amount,
            components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
            confidence="exception",
        )

    if us_rule_type in ("exempt", "no_tax"):
        label = "exempt from" if us_rule_type == "exempt" else "not subject to"
        return TaxTreatment(
            tax_type="SALES_TAX",
            jurisdiction=state,
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=f"US {state}: SaaS {label} state sales tax",
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
            gross_amount=functional_amount,
            components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
        )

    tax = _quantize(functional_amount * rate)
    return TaxTreatment(
        tax_type="SALES_TAX",
        jurisdiction=state,
        rate=rate,
        tax_amount=tax,
        is_reclaimable=False,
        reclaim_amount=_ZERO,
        legal_reference=(
            f"US {state}: self-assessed use tax at {rate} on a no-nexus B2B "
            "purchase (confirm rate/nexus with a CPA)"
        ),
        reverse_charge=True,
        self_invoice_required=False,
        currency=entity_currency,
        gross_amount=functional_amount,
        components=(TaxComponent("US_USE_TAX", rate, tax, _ZERO),),
    )


def compute_tax(
    *,
    tax_regime: str,
    functional_amount: Decimal,
    paid_currency: str,
    entity_currency: str,
    rate: Decimal,
    expense_category: str | None = None,
    tax_inclusive: bool = True,
    vendor_country: str | None = None,
    vendor_state: str | None = None,
    vendor_registered_in: tuple[str, ...] | None = None,
    entity_country: str | None = None,
    entity_state: str | None = None,
    us_rule_type: str | None = None,
    us_jurisdiction: str | None = None,
    supply_type: str = "service",
) -> TaxTreatment:
    """Compute the tax treatment for a captured spend.

    Args:
        tax_regime: The entity's regime (e.g. ``"IN-GST"``, ``"US-Sales-Tax"``).
        functional_amount: Amount in the entity's functional currency.
        paid_currency: ISO code actually paid (unused for place of supply).
        entity_currency: The entity's functional currency.
        rate: Effective-dated statutory rate as a fraction (0.18 = 18%).
        expense_category: Drives blocked ITC (e.g. IN travel).
        tax_inclusive: Domestic amount includes GST; ignored for reverse charge.
        vendor_country: Vendor ISO country; ``None`` ⇒ UNCLASSIFIED exception.
        vendor_state: Vendor state (India), for intra- vs inter-state.
        vendor_registered_in: Countries the vendor is GST-registered in; RCM
            applies when the buyer's country is absent.
        entity_country: The entity's ISO country.
        entity_state: The entity's state (India).
        us_rule_type: Buyer state's US rule; ``None``/disputed ⇒ exception.
        us_jurisdiction: Resolved US jurisdiction (e.g. ``"US-TX"``).
        supply_type: ``"service"`` or ``"goods"``; cross-border goods ⇒ customs.

    Returns:
        A `TaxTreatment` in ``entity_currency`` with per-head ``components``.

    Raises:
        UnknownTaxRegimeError: ``tax_regime`` is not in `KNOWN_TAX_REGIMES`.
    """
    if tax_regime not in KNOWN_TAX_REGIMES:
        raise UnknownTaxRegimeError(tax_regime)

    # Cross-border goods are customs, never services reverse charge.
    if (
        supply_type == "goods"
        and vendor_country is not None
        and entity_country is not None
        and vendor_country.strip().upper() != entity_country.strip().upper()
    ):
        return _customs_treatment(functional_amount, entity_currency)

    rule = _REGIMES.get(tax_regime)

    if rule is None:
        return _us_treatment(
            functional_amount=functional_amount,
            entity_currency=entity_currency,
            rate=rate,
            us_rule_type=us_rule_type,
            us_jurisdiction=us_jurisdiction,
        )

    # Unknown place of supply: don't guess from currency. Book conservatively
    # (no ITC, no RCM, whole amount expensed) and flag for review.
    if vendor_country is None or entity_country is None:
        return TaxTreatment(
            tax_type="UNCLASSIFIED",
            jurisdiction=rule.jurisdiction,
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=(
                f"{rule.jurisdiction}: place of supply unknown — vendor unenriched; "
                "tax deferred pending classification"
            ),
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
            gross_amount=functional_amount,
            components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
            confidence="exception",
        )

    # Independent axes: ``imported`` drives the head split; registration in the
    # buyer's country is the legal reverse-charge test.
    buyer = entity_country.strip().upper()
    imported = vendor_country.strip().upper() != buyer
    registered = {c.strip().upper() for c in (vendor_registered_in or ())}
    registered_in_buyer_country = buyer in registered

    blocked = _is_itc_blocked(tax_regime, expense_category)
    head_mode = _head_mode(
        tax_regime=tax_regime,
        imported=imported,
        place_of_supply_known=True,
        vendor_state=vendor_state,
        entity_state=entity_state,
    )

    # RCM: vendor not registered in buyer's country AND (cross-border OR India
    # domestic-unregistered, CGST s.9(4)). Registered OIDAR vendors charge forward.
    reverse_charge = (not registered_in_buyer_country) and (
        imported or tax_regime == "IN-GST"
    )

    if reverse_charge:
        # GST is a payable to government, not cash to the vendor.
        tax = _quantize(functional_amount * rate)
        gross = functional_amount
        legal_reference = (
            rule.legal_reference_imported
            if imported
            else f"{rule.jurisdiction} reverse charge — unregistered supplier, "
            "CGST s.9(4)"
        )
        tax_type = "GST_RCM"
        self_invoice_required = True
    elif tax_inclusive:
        tax = _quantize(functional_amount * rate / (_ONE + rate))
        gross = functional_amount
        legal_reference = rule.legal_reference_domestic
        tax_type = "GST"
        self_invoice_required = False
    else:
        # Vendor charged GST on top; extracting from a net figure is phantom ITC.
        tax = _quantize(functional_amount * rate)
        gross = functional_amount + tax
        legal_reference = (
            rule.legal_reference_domestic + " — amount tax-exclusive, GST added on top"
        )
        tax_type = "GST"
        self_invoice_required = False

    reclaim = _ZERO if blocked else tax
    if blocked:
        legal_reference += _BLOCKED_ITC_NOTE

    return TaxTreatment(
        tax_type=tax_type,
        jurisdiction=rule.jurisdiction,
        rate=rate,
        tax_amount=tax,
        is_reclaimable=not blocked,
        reclaim_amount=reclaim,
        legal_reference=legal_reference,
        reverse_charge=reverse_charge,
        self_invoice_required=self_invoice_required,
        currency=entity_currency,
        gross_amount=gross,
        components=_components(head_mode, rate, tax, reclaim),
    )
