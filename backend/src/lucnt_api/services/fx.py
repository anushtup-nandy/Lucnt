"""FX conversion into an entity's functional currency (USD-anchored, effective-dated).

Fail-closed: a missing rate raises rather than booking at 1.0, which would silently
corrupt the ledger, tax and budgets.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.models import FxRate

_ONE = Decimal("1")
# The ledger's money scale (transactions/journal columns are Numeric(18, 4)).
_LEDGER_SCALE = Decimal("0.0001")


class FxRateUnavailableError(Exception):
    """No effective FX rate exists for a currency on/before a date (fail-closed)."""

    def __init__(self, currency: str, on_date: date) -> None:
        """Record the currency and date that had no effective rate."""
        self.currency = currency
        self.on_date = on_date
        super().__init__(
            f"no FX rate for {currency!r} effective on or before {on_date.isoformat()}"
        )


def _usd_per_unit(db: Session, currency: str, on_date: date) -> Decimal:
    """USD value of one ``currency`` unit at the latest effective date <= on_date."""
    value = db.execute(
        select(FxRate.usd_per_unit)
        .where(
            FxRate.currency == currency.upper(),
            FxRate.effective_from <= on_date,
        )
        .order_by(FxRate.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()
    if value is None:
        raise FxRateUnavailableError(currency.upper(), on_date)
    return Decimal(value)


def resolve_rate(
    db: Session,
    *,
    paid_currency: str,
    functional_currency: str,
    on_date: date,
) -> Decimal:
    """Units of ``functional_currency`` per one ``paid_currency``, crossed via USD.

    Raises:
        FxRateUnavailableError: if either currency has no rate on/before ``on_date``.
    """
    if paid_currency.upper() == functional_currency.upper():
        return _ONE
    usd_paid = _usd_per_unit(db, paid_currency, on_date)
    usd_functional = _usd_per_unit(db, functional_currency, on_date)
    return usd_paid / usd_functional


def convert(
    db: Session,
    *,
    amount: Decimal,
    paid_currency: str,
    functional_currency: str,
    on_date: date,
) -> tuple[Decimal, Decimal]:
    """Convert ``amount`` to the functional currency.

    Returns:
        ``(exchange_rate, local_amount)``; amount quantized half-up to 4 dp.

    Raises:
        FxRateUnavailableError: if a rate is missing.
    """
    rate = resolve_rate(
        db,
        paid_currency=paid_currency,
        functional_currency=functional_currency,
        on_date=on_date,
    )
    local_amount = (amount * rate).quantize(_LEDGER_SCALE, rounding=ROUND_HALF_UP)
    return rate, local_amount
