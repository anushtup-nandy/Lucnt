"""SelfInvoice ORM model: the buyer's reverse-charge self-invoice (CGST r.46).

The artifact of record behind `tax_records.self_invoice_generated`.
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
    # r.46 invoice number, deterministic per txn (no per-entity sequence yet).
    invoice_number: Mapped[str] = mapped_column(String(64))
    supplier_name: Mapped[str] = mapped_column(String(255))  # the foreign vendor
    recipient_entity_code: Mapped[str] = mapped_column(String(50))  # the buyer (self)
    # All amounts in the entity's functional currency.
    taxable_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    legal_reference: Mapped[str] = mapped_column(Text)
