"""Jurisdiction-aware tax engine (backend/PLAN.md §5, Module 2).

Given an entity's tax regime and the transaction amount **in the entity's
functional currency**, compute the tax treatment. Pure and side-effect-free: it
returns a `TaxTreatment` value; persistence and journalling happen elsewhere.

MVP scope — India GST, Singapore GST, US sales tax:

- **Imported services** (paid in a foreign currency, i.e. a foreign vendor that
  bills no local tax) under IN/SG GST trigger the **reverse charge (RCM)**: the
  buyer self-assesses tax *on top of* the tax-exclusive amount, claims it back as
  input credit, and must issue a self-invoice.
- **Domestic services** carry GST *embedded* in the (tax-inclusive) amount; the
  engine extracts it and records the reclaimable input credit.
- **US sales tax**: no federal VAT and state sales/use-tax nexus is not evaluated
  in the MVP, so no tax is self-assessed (rate 0).

**Currency (PRODUCTION_PLAN T1-4).** Tax is computed on ``functional_amount`` —
the paid amount already converted to the entity's functional currency by
`services/fx.py` at capture — never on the raw foreign amount. Computing an INR
entity's RCM on a USD figure understated GST by the whole FX factor. The
``paid_currency`` vs ``entity_currency`` comparison still decides *import vs
domestic* (T1-3 refines this to place-of-supply in Sprint 2b); the *amount* the
rate applies to is always functional.

All money math is `Decimal`; tax is quantized to two places, ROUND_HALF_UP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_TWO_PLACES = Decimal("0.01")
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class TaxTreatment:
    """The tax verdict for one transaction — persisted as a `TaxRecord`."""

    tax_type: str  # GST_RCM | GST | SALES_TAX | NONE
    jurisdiction: str
    rate: Decimal
    tax_amount: Decimal
    is_reclaimable: bool
    reclaim_amount: Decimal
    legal_reference: str
    reverse_charge: bool
    self_invoice_required: bool
    # The functional (entity) currency ``tax_amount``/``reclaim_amount`` are
    # denominated in — tax is always assessed in functional currency (T1-4).
    currency: str


@dataclass(frozen=True)
class _RegimeRule:
    """Static config for one GST regime (reverse-charge on imports)."""

    jurisdiction: str
    rate: Decimal
    legal_reference_imported: str
    legal_reference_domestic: str


_REGIMES: dict[str, _RegimeRule] = {
    "IN-GST": _RegimeRule(
        jurisdiction="India",
        rate=Decimal("0.18"),
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
        rate=Decimal("0.09"),
        legal_reference_imported=(
            "GST Act (Cap. 117A) s.14 — reverse charge on imported services"
        ),
        legal_reference_domestic=(
            "GST Act (Cap. 117A) — standard-rated domestic supply; input tax per s.19"
        ),
    ),
}


def _quantize(amount: Decimal) -> Decimal:
    """Round a money amount to two decimal places, half-up (standard tax rounding)."""
    return amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_tax(
    *,
    tax_regime: str,
    functional_amount: Decimal,
    paid_currency: str,
    entity_currency: str,
) -> TaxTreatment:
    """Compute the tax treatment for a captured spend.

    Args:
        tax_regime: The entity's regime (e.g. ``"IN-GST"``, ``"US-Sales-Tax"``).
        functional_amount: The transaction amount **in the entity's functional
            currency** (already FX-converted from the paid amount, T1-4).
        paid_currency: ISO code of what was actually paid — used only to decide
            import vs domestic.
        entity_currency: ISO code of the entity's functional currency.

    Returns:
        A `TaxTreatment`; ``tax_amount`` is 0 for regimes with no self-assessed
        tax (US / unknown). ``currency`` is always ``entity_currency``.
    """
    imported = paid_currency.upper() != entity_currency.upper()
    rule = _REGIMES.get(tax_regime)

    if rule is None:
        # US sales tax and any unrecognised regime: no tax self-assessed in MVP.
        is_us = tax_regime == "US-Sales-Tax"
        return TaxTreatment(
            tax_type="SALES_TAX" if is_us else "NONE",
            jurisdiction="United States" if is_us else tax_regime,
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=(
                "US: no federal VAT; state sales/use-tax nexus not evaluated (MVP)"
                if is_us
                else f"No tax rule configured for regime {tax_regime!r} (MVP)"
            ),
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
        )

    if imported:
        # Reverse charge: tax self-assessed on top of the tax-exclusive amount,
        # in the functional currency.
        tax = _quantize(functional_amount * rule.rate)
        return TaxTreatment(
            tax_type="GST_RCM",
            jurisdiction=rule.jurisdiction,
            rate=rule.rate,
            tax_amount=tax,
            is_reclaimable=True,
            reclaim_amount=tax,
            legal_reference=rule.legal_reference_imported,
            reverse_charge=True,
            self_invoice_required=True,
            currency=entity_currency,
        )

    # Domestic: GST is embedded in the tax-inclusive (functional) amount.
    tax = _quantize(functional_amount * rule.rate / (_ONE + rule.rate))
    return TaxTreatment(
        tax_type="GST",
        jurisdiction=rule.jurisdiction,
        rate=rule.rate,
        tax_amount=tax,
        is_reclaimable=True,
        reclaim_amount=tax,
        legal_reference=rule.legal_reference_domestic,
        reverse_charge=False,
        self_invoice_required=False,
        currency=entity_currency,
    )
