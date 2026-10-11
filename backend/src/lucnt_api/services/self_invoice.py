"""Reverse-charge self-invoice builder (pure; caller adds it to the session).

Only called when `self_invoice_required`, so the persisted flag always has a real
artifact behind it.
"""

from __future__ import annotations

from lucnt_api.models import Entity, SelfInvoice, Transaction
from lucnt_api.services.tax_engine import TaxTreatment


def build_self_invoice(
    transaction: Transaction, entity: Entity, tax: TaxTreatment
) -> SelfInvoice:
    """Build the r.46 self-invoice for a captured RCM transaction (with an id)."""
    return SelfInvoice(
        transaction_id=transaction.id,
        invoice_number=f"SI-{entity.code.upper()}-{transaction.id.hex[:12]}",
        supplier_name=transaction.vendor,
        recipient_entity_code=entity.code,
        taxable_value=transaction.local_amount,
        tax_amount=tax.tax_amount,
        currency=tax.currency,
        legal_reference=tax.legal_reference,
    )
