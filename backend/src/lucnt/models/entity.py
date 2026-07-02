"""Legal Entity ORM model — one per jurisdiction/subsidiary within a company."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Entity(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A legal entity (subsidiary/jurisdiction) belonging to a company."""

    __tablename__ = "entities"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    accounting_std: Mapped[str] = mapped_column(String(50))
    erp: Mapped[str | None] = mapped_column(String(50), default=None)
    tax_regime: Mapped[str] = mapped_column(String(50))
    # NUMERIC, never FLOAT -- FX rate is money math (sdk/PLAN.md: Decimal not float).
    to_usd_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("1.0"))
    timezone: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=True)
