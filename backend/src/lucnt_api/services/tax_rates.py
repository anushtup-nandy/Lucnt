"""Effective-dated statutory tax-rate resolution (PRODUCTION_PLAN T1-12).

Rates live in the `tax_rates` table (regime, category, effective_from). The rate
in force for a supply is the latest ``effective_from <=`` the transaction date, so
a rate change (Singapore GST 7%→8%→9%) never back-applies to a historical supply.
Keyed by transaction date, exactly like `services/fx.py` is for currency.

**Fail-closed by design** (like FX): an unconfigured ``(regime, category)`` on a
date raises `TaxRateUnavailableError` rather than guessing a rate. The rate is
resolved at the money-path boundary (the capture/authorize router) and injected
into the pure `services/tax_engine.py`, which stays SQLAlchemy-free.

All rate math is `Decimal`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.models import TaxRate

# The default rate slab. GST slabs (0/5/12/18/28) and inclusive/exclusive land in
# T1-10 as additional categories; every regime seeds at least "standard".
STANDARD_CATEGORY = "standard"


class TaxRateUnavailableError(Exception):
    """No effective tax rate exists for a regime/category on/before a date."""

    def __init__(self, regime: str, category: str, on_date: date) -> None:
        """Record the regime/category/date that had no effective rate."""
        self.regime = regime
        self.category = category
        self.on_date = on_date
        super().__init__(
            f"no tax rate for regime {regime!r} category {category!r} effective "
            f"on or before {on_date.isoformat()}"
        )


def resolve_rate(
    db: Session,
    *,
    regime: str,
    category: str = STANDARD_CATEGORY,
    on_date: date,
) -> Decimal:
    """The statutory rate for ``regime``/``category`` in force on ``on_date``.

    Picks the row with the latest ``effective_from <= on_date``.

    Raises:
        TaxRateUnavailableError: if no rate is configured on/before ``on_date``
            (fail-closed — a missing rate stops the spend, never guesses).
    """
    value = db.execute(
        select(TaxRate.rate)
        .where(
            TaxRate.regime == regime,
            TaxRate.category == category,
            TaxRate.effective_from <= on_date,
        )
        .order_by(TaxRate.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()
    if value is None:
        raise TaxRateUnavailableError(regime, category, on_date)
    return Decimal(value)
