"""SelfInvoice ORM model — the reverse-charge self-invoice record.

Under GST reverse charge the buyer must issue a self-invoice for the imported
service (CGST Rules r.46). Previously `tax_records.self_invoice_generated` was set
`True` without any artifact ever existing — a fabricated compliance flag. This
table is the artifact of record: one structured self-invoice per RCM transaction,
so the flag is only ever set truthfully. A rendered PDF is a presentation concern
deferred to the M3 invoice hook; the structured record is the compliance data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class SelfInvoice(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A reverse-charge self-invoice issued by the buyer for an imported service."""

    __tablename__ = "self_invoices"
    __table_args__ = (
        # One self-invoice per transaction (idempotent on a capture replay).
        UniqueConstraint("transaction_id", name="uq_self_invoices_transaction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # Buyer-generated invoice number (r.46). A per-entity statutory sequence is a
    # follow-up; this deterministic number is unique per transaction.
    invoice_number: Mapped[str] = mapped_column(String(64))
    supplier_name: Mapped[str] = mapped_column(String(255))  # the foreign vendor
    recipient_entity_code: Mapped[str] = mapped_column(String(50))  # the buyer (self)
    # All amounts in the entity's functional currency.
    taxable_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    legal_reference: Mapped[str] = mapped_column(Text)
