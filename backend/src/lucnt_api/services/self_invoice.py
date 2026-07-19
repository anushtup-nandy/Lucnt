"""Self-invoice generator (Module 3 / PRODUCTION_PLAN T1-7).

Builds the reverse-charge self-invoice record for an RCM transaction. Pure: it
returns an unsaved `SelfInvoice`; the caller adds it to the session. Called only
when `TaxTreatment.self_invoice_required` is true, so the persisted
`tax_records.self_invoice_generated` flag is only ever set with a real artifact
behind it (no fabricated compliance flag).
"""

from __future__ import annotations

from lucnt_api.models import Entity, SelfInvoice, Transaction
from lucnt_api.services.tax_engine import TaxTreatment


def build_self_invoice(
    transaction: Transaction, entity: Entity, tax: TaxTreatment
) -> SelfInvoice:
    """Build the r.46 self-invoice record for a reverse-charge transaction.

    Args:
        transaction: The captured RCM transaction (must already have an ``id``).
        entity: The buyer's legal entity (the self-invoice recipient).
        tax: The reverse-charge `TaxTreatment` (amounts in functional currency).

    Returns:
        An unsaved `SelfInvoice` (taxable value + self-assessed tax, functional ccy).
    """
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
