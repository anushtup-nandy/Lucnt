"""Effective-dated statutory tax-rate resolution, keyed by transaction date.

Fail-closed: a missing rate raises rather than guessing. Resolved at the router
and injected into the pure tax engine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lucnt_api.models import TaxRate

# Every regime seeds at least this category.
STANDARD_CATEGORY = "standard"
# Rows keyed per-state by `US-{state}` jurisdiction.
US_REGIME = "US-Sales-Tax"


@dataclass(frozen=True)
class UsTaxRule:
    """A US per-state sales/use-tax rule in force on a date."""

    jurisdiction: str  # e.g. "US-TX"
    rule_type: str
    rate: Decimal
    rule_id: uuid.UUID
    source: str


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
    """The regime-level statutory rate in force on ``on_date`` (not US per-state).

    Raises:
        TaxRateUnavailableError: if no rate is configured on/before ``on_date``.
    """
    return Decimal(
        resolve_rate_row(db, regime=regime, category=category, on_date=on_date).rate
    )


def resolve_rate_row(
    db: Session,
    *,
    regime: str,
    category: str = STANDARD_CATEGORY,
    on_date: date,
) -> TaxRate:
    """Like `resolve_rate` but returns the row, for the applied-rule audit trail."""
    row = db.execute(
        select(TaxRate)
        .where(
            TaxRate.regime == regime,
            TaxRate.category == category,
            TaxRate.jurisdiction.is_(None),
            TaxRate.effective_from <= on_date,
            or_(TaxRate.effective_to.is_(None), TaxRate.effective_to > on_date),
        )
        .order_by(TaxRate.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise TaxRateUnavailableError(regime, category, on_date)
    return row


def resolve_us_rule(
    db: Session,
    *,
    us_state: str,
    on_date: date,
    category: str = STANDARD_CATEGORY,
) -> UsTaxRule | None:
    """The US sales/use-tax rule for a state on ``on_date``, or None.

    ``None`` lets the caller route to the recon queue instead of booking zero.
    """
    jurisdiction = f"US-{us_state.strip().upper()}"
    row = db.execute(
        select(TaxRate.id, TaxRate.rule_type, TaxRate.rate, TaxRate.source)
        .where(
            TaxRate.regime == US_REGIME,
            TaxRate.category == category,
            TaxRate.jurisdiction == jurisdiction,
            TaxRate.effective_from <= on_date,
            or_(TaxRate.effective_to.is_(None), TaxRate.effective_to > on_date),
        )
        .order_by(TaxRate.effective_from.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return UsTaxRule(
        jurisdiction=jurisdiction,
        rule_type=row.rule_type,
        rate=Decimal(row.rate),
        rule_id=row.id,
        source=row.source,
    )
