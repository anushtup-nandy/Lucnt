"""Effective-dated statutory tax-rate resolution.

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

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lucnt_api.models import TaxRate

# The default rate slab. GST slabs (0/5/12/18/28) and inclusive/exclusive land
# as additional categories; every regime seeds at least "standard".
STANDARD_CATEGORY = "standard"
# The US regime whose rows are keyed per-state by `US-{state}` jurisdiction.
US_REGIME = "US-Sales-Tax"


@dataclass(frozen=True)
class UsTaxRule:
    """A US per-state sales/use-tax rule in force on a date."""

    jurisdiction: str  # e.g. "US-TX"
    # standard | use_tax | exempt | no_tax | disputed (see `TaxRate.rule_type`).
    rule_type: str
    rate: Decimal
    # The `tax_rates` row this came from + its provenance, for the audit trail.
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
    """The statutory rate for a regime-level ``regime``/``category`` on ``on_date``.

    Picks the row in force (latest ``effective_from <= on_date`` whose
    ``effective_to`` is null or after ``on_date``). Matches only **regime-level**
    rows (``jurisdiction IS NULL``) — India/Singapore GST; US per-state rows are
    resolved by `resolve_us_rule`.

    Raises:
        TaxRateUnavailableError: if no rate is configured on/before ``on_date``
            (fail-closed — a missing rate stops the spend, never guesses).
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
    """The in-force regime-level `TaxRate` row (for the applied-rule trail).

    Same match as `resolve_rate` (regime-level, ``jurisdiction IS NULL``, effective
    window) but returns the whole row so the caller can stamp `applied_rule_id` /
    `applied_rule_source` on the resulting tax records.
    """
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
    """The US sales/use-tax rule for a state in force on ``on_date``, or None.

    Keyed by the ``US-{state}`` jurisdiction. Returns ``None`` (rather than raising)
    when the state has no configured rule — the caller routes that to the recon
    exception queue rather than booking a silent zero.
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
