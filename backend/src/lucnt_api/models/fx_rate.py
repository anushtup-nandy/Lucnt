"""FxRate ORM model — USD-anchored, effective-dated FX reference rates.

The single source of truth for currency conversion at capture (PRODUCTION_PLAN
T1-1 / T1-5, and effective-dating T1-12). Global reference data (not
company-scoped): one row per ``(currency, effective_from)`` giving the USD value
of one unit of that currency from that date onward. The rate for any pair is
cross-computed via USD by `services/fx.py`, which picks the latest
``effective_from <= transaction_date``.

The rate is **never caller-supplied** — a spending agent cannot game its own
conversion (and thus its budget/tax). The design principle: the caller states
facts it knows (amount, currency); the system makes the judgment (the rate).
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
        # One rate per currency per effective date; the resolver orders by
        # effective_from to pick the one in force on the transaction date.
        UniqueConstraint(
            "currency", "effective_from", name="uq_fx_rates_currency_effective_from"
        ),
        # A rate is strictly positive — the DB is the last line of defence for a
        # future writer (a feed importer, a manual insert) that bypasses the app.
        CheckConstraint("usd_per_unit > 0", name="ck_fx_rates_usd_per_unit_positive"),
    )

    # ISO-4217 code of the priced currency (e.g. "INR"). "USD" is the anchor
    # (``usd_per_unit == 1.0``).
    currency: Mapped[str] = mapped_column(String(3))
    # USD value of one unit of ``currency``. NUMERIC, never FLOAT (money math).
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    # The rate applies to transactions dated on/after this date.
    effective_from: Mapped[date] = mapped_column(Date)
    # Provenance for audit ("seed", a provider name, "manual").
    source: Mapped[str] = mapped_column(String(50), default="seed")
