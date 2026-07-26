"""WithholdingRecord ORM model — India s.195 withholding on a payment (TE-3).

The compliance record beside `tax_records` (GST): the second, independent regime a
foreign remittance from an India entity owes. The authorize-time **block** lives on
the authorization's verdict, not here — a persisted record means the remittance
proceeded, so a record is never itself blocked. Form ids are nullable: the
15CA/15CB documents are generated later (deferred, like the self-invoice was before
T1-7). The double-entry GL leg (a TDS-payable liability + any gross-up) is deferred
pending CA guidance (PRODUCTION_PLAN TE-3).
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
        # Unindexed FK (T3-1) — records are read back per transaction.
        Index("ix_withholding_records_transaction_id", "transaction_id"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # The applied rate in basis points (1500 = 15%) + the functional-currency amount.
    rate_bps: Mapped[int] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    # DTAA treaty rate (valid TRC + Form 10F on file) vs the domestic default.
    treaty_rate_applied: Mapped[bool] = mapped_column(default=False)
    # 15CA (self-declaration) / 15CB (CA certificate) document ids — nullable until
    # the documents are actually generated (deferred, TE-3).
    form_15ca_id: Mapped[str | None] = mapped_column(String(50), default=None)
    form_15cb_id: Mapped[str | None] = mapped_column(String(50), default=None)
    legal_reference: Mapped[str] = mapped_column(Text, default="")
