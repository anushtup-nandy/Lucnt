"""TaxRecord ORM model — the tax treatment computed for one transaction (Phase 2).

Produced by `services/tax_engine.py` at capture time. Captures the jurisdiction
verdict: rate, amount, whether it is reclaimable input credit, and whether a
reverse-charge self-invoice must be generated.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class TaxRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The tax treatment (type, rate, amount, reclaim, self-invoice) for a txn."""

    __tablename__ = "tax_records"

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # e.g. "GST_RCM" (reverse charge), "GST" (domestic), "SALES_TAX", "NONE".
    tax_type: Mapped[str] = mapped_column(String(50))
    jurisdiction: Mapped[str] = mapped_column(String(50))
    # NUMERIC, never FLOAT. rate is a fraction (0.18 = 18%).
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    is_reclaimable: Mapped[bool] = mapped_column(default=False)
    reclaim_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0")
    )
    legal_reference: Mapped[str] = mapped_column(Text)
    self_invoice_generated: Mapped[bool] = mapped_column(default=False)
