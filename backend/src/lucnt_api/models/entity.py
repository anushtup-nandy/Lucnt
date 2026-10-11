"""Legal Entity ORM model — one per jurisdiction/subsidiary within a company."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Entity(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A legal entity (subsidiary/jurisdiction) belonging to a company."""

    __tablename__ = "entities"
    __table_args__ = (
        # Code is the authorize()/statement lookup key; a duplicate makes
        # scalar_one_or_none() raise, a 500 on the spend path.
        Index("uq_entities_company_code", "company_id", "code", unique=True),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    # IFRS/Ind-AS/US-GAAP; reserved for FX consolidation, unused until then.
    accounting_std: Mapped[str] = mapped_column(String(50))
    erp: Mapped[str | None] = mapped_column(String(50), default=None)
    tax_regime: Mapped[str] = mapped_column(String(50))
    # Recipient's place of supply: `country` decides import vs domestic;
    # `gst_state` decides CGST+SGST (intra-state) vs IGST for India.
    country: Mapped[str | None] = mapped_column(String(2), default=None)
    gst_state: Mapped[str | None] = mapped_column(String(10), default=None)
    # Buyer's US state for `US-{state}` sales/use-tax; if missing, tax routes to
    # the recon exception queue rather than a silent 0.
    us_state: Mapped[str | None] = mapped_column(String(2), default=None)
    to_usd_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("1.0"))
    timezone: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=True)
