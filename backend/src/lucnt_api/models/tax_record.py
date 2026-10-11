"""TaxRecord ORM model: the tax treatment computed for a transaction at capture."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class TaxRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The tax treatment (type, rate, amount, reclaim, self-invoice) for a txn."""

    __tablename__ = "tax_records"
    __table_args__ = (
        # Tax reclass and capture-replay look records up by transaction.
        Index("ix_tax_records_transaction_id", "transaction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # e.g. "GST_RCM" (reverse charge), "GST" (domestic), "SALES_TAX", "NONE".
    tax_type: Mapped[str] = mapped_column(String(50))
    # "CGST"/"SGST"/"IGST" (India), "GST" (unsplit), "NONE" (US). A split domestic
    # supply writes CGST + SGST rows; needed for GSTR-3B.
    tax_head: Mapped[str] = mapped_column(String(10), default="GST")
    jurisdiction: Mapped[str] = mapped_column(String(50))
    # Fraction (0.18 = 18%).
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    is_reclaimable: Mapped[bool] = mapped_column(default=False)
    reclaim_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0")
    )
    # Functional currency: tax is assessed on the FX-converted local amount.
    currency: Mapped[str] = mapped_column(String(3))
    legal_reference: Mapped[str] = mapped_column(Text)
    self_invoice_generated: Mapped[bool] = mapped_column(default=False)
    # Source `tax_rates` row, so an auditor can trace "why this rate". Null when no
    # rate applies (UNCLASSIFIED, CUSTOMS, US-unconfigured).
    applied_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tax_rates.id"), default=None
    )
    applied_rule_source: Mapped[str | None] = mapped_column(String(50), default=None)
