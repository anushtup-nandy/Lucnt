"""TaxRate ORM model — effective-dated statutory tax rates (PRODUCTION_PLAN T1-12).

The single source of truth for the statutory rate applied at capture. Global
reference data (not company-scoped): one row per ``(regime, category,
effective_from)`` giving the rate in force from that date onward. The rate for a
supply is the latest ``effective_from <= transaction_date``, resolved by
`services/tax_rates.py` — exactly the effective-dated pattern `fx_rates` uses for
currency, so a rate change (SG GST 7%→8%→9%) never back-applies to a historical
supply.

``category`` carries the rate slab (``"standard"`` today; IN GST 0/5/12/18/28 and
inclusive/exclusive land in T1-10). The rate is **never caller-supplied** — an
agent cannot game its own tax by choosing a rate; it states facts (amount,
currency, vendor), the system resolves the rate.
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
        # One rate per (regime, category, jurisdiction) per effective date; the
        # resolver orders by effective_from to pick the one in force on the txn date.
        # `jurisdiction` is in the key because many US per-state rows share
        # (regime, category, effective_from) and differ only by state (TE-2).
        UniqueConstraint(
            "regime",
            "category",
            "jurisdiction",
            "effective_from",
            name="uq_tax_rates_regime_category_jurisdiction_effective_from",
        ),
        # A rate is a non-negative fraction. Unlike an FX rate it may be exactly 0
        # (a zero-rated/exempt slab, or the US no-self-assessed-tax MVP row).
        CheckConstraint(
            "rate >= 0 AND rate <= 1", name="ck_tax_rates_rate_fraction"
        ),
    )

    # The tax regime the rate belongs to (e.g. "IN-GST", "SG-GST", "US-Sales-Tax").
    regime: Mapped[str] = mapped_column(String(20))
    # The rate slab within the regime ("standard" today; GST 0/5/12/18/28 in T1-10).
    category: Mapped[str] = mapped_column(String(20), default="standard")
    # The sub-regime jurisdiction, for regimes that vary below the regime level: a
    # US state as ``"US-TX"`` (TE-2). ``None`` for regime-level rows (IN/SG GST),
    # which the resolver matches with ``jurisdiction IS NULL``.
    jurisdiction: Mapped[str | None] = mapped_column(String(20), default=None)
    # How the rate is applied (TE-2): "standard" (charged/self-assessed at `rate`),
    # "use_tax" (self-accrued), "exempt"/"no_tax" (zero), or "disputed" (never
    # auto-applied — the engine routes it to the recon exception queue).
    rule_type: Mapped[str] = mapped_column(String(20), default="standard")
    # The rate as a fraction (0.18 = 18%). NUMERIC, never FLOAT (money math).
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    # The rate applies to supplies dated on/after this date.
    effective_from: Mapped[date] = mapped_column(Date)
    # The rate stops applying on this date (append-only close-out, TE-6): a change
    # closes the old row (sets `effective_to`) and inserts a new one, never mutating
    # history. ``None`` ⇒ still in force.
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)
    # Provenance for audit ("seed", a statutory reference, "manual").
    source: Mapped[str] = mapped_column(String(50), default="seed")
