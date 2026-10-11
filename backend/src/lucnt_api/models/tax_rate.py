"""TaxRate ORM model: effective-dated statutory tax rates (global reference data).

Effective-dated so a rate change never back-applies; never caller-supplied.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class TaxRate(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The statutory tax rate for a regime/category, effective from a date."""

    __tablename__ = "tax_rates"
    __table_args__ = (
        # `jurisdiction` is in the key: US per-state rows differ only by state.
        UniqueConstraint(
            "regime",
            "category",
            "jurisdiction",
            "effective_from",
            name="uq_tax_rates_regime_category_jurisdiction_effective_from",
        ),
        # Unlike FX, may be exactly 0 (zero-rated/exempt slab).
        CheckConstraint("rate >= 0 AND rate <= 1", name="ck_tax_rates_rate_fraction"),
    )

    # e.g. "IN-GST", "SG-GST", "US-Sales-Tax".
    regime: Mapped[str] = mapped_column(String(20))
    # Rate slab within the regime.
    category: Mapped[str] = mapped_column(String(20), default="standard")
    # e.g. "US-TX"; None for regime-level rows (IN/SG GST).
    jurisdiction: Mapped[str | None] = mapped_column(String(20), default=None)
    # "standard", "use_tax", "exempt"/"no_tax", or "disputed" (never auto-applied;
    # routed to the recon exception queue).
    rule_type: Mapped[str] = mapped_column(String(20), default="standard")
    # Fraction (0.18 = 18%).
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    effective_from: Mapped[date] = mapped_column(Date)
    # Append-only close-out: a change closes the old row and inserts a new one.
    # None = still in force.
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)
    # Provenance for audit ("seed", a statutory reference, "manual").
    source: Mapped[str] = mapped_column(String(50), default="seed")
