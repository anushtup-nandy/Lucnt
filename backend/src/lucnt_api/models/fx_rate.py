"""FxRate ORM model: USD-anchored, effective-dated FX reference rates (global).

Never caller-supplied, so an agent can't game its own conversion (budget/tax).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class FxRate(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The USD value of one unit of a currency, effective from a date."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint(
            "currency", "effective_from", name="uq_fx_rates_currency_effective_from"
        ),
        # DB is the last line of defence for writers that bypass the app.
        CheckConstraint("usd_per_unit > 0", name="ck_fx_rates_usd_per_unit_positive"),
    )

    # ISO-4217; "USD" is the anchor (usd_per_unit == 1).
    currency: Mapped[str] = mapped_column(String(3))
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    # Applies to transactions dated on/after this.
    effective_from: Mapped[date] = mapped_column(Date)
    # Provenance for audit ("seed", a provider name, "manual").
    source: Mapped[str] = mapped_column(String(50), default="seed")
