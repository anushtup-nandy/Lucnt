"""WithholdingRecord ORM model: India s.195 withholding on a foreign remittance.

A record means the remittance proceeded; any block lives on the verdict.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class WithholdingRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """s.195 withholding computed for one captured India → non-resident payment."""

    __tablename__ = "withholding_records"
    __table_args__ = (
        # Read back per transaction.
        Index("ix_withholding_records_transaction_id", "transaction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # Basis points (1500 = 15%); amount is in functional currency.
    rate_bps: Mapped[int] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    # DTAA treaty rate (valid TRC + Form 10F on file) vs the domestic default.
    treaty_rate_applied: Mapped[bool] = mapped_column(default=False)
    # 15CA / 15CB document ids; null until generated.
    form_15ca_id: Mapped[str | None] = mapped_column(String(50), default=None)
    form_15cb_id: Mapped[str | None] = mapped_column(String(50), default=None)
    legal_reference: Mapped[str] = mapped_column(Text, default="")
