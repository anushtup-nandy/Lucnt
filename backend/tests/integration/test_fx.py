"""FX resolver tests against the migration-seeded fx_rates.

Integration, not pure-unit: the resolver reads `fx_rates`, and the baseline rows
(USD/INR/SGD/EUR/GBP) are seeded by migration `b8d3f1a2c4e5`, so they exist in
the migrated test DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.models import FxRate
from lucnt_api.services.fx import FxRateUnavailableError, convert, resolve_rate

_ON = date(2026, 6, 1)


def _latest_usd_per_unit(db: Session, currency: str) -> Decimal:
    """Read the seeded USD-per-unit for a currency (to derive expected values)."""
    return db.execute(
        select(FxRate.usd_per_unit)
        .where(FxRate.currency == currency, FxRate.effective_from <= _ON)
        .order_by(FxRate.effective_from.desc())
        .limit(1)
    ).scalar_one()


def test_same_currency_is_identity(db_session: Session) -> None:
    rate = resolve_rate(
        db_session, paid_currency="INR", functional_currency="INR", on_date=_ON
    )
    assert rate == Decimal("1")


def test_cross_rate_via_usd(db_session: Session) -> None:
    rate = resolve_rate(
        db_session, paid_currency="USD", functional_currency="INR", on_date=_ON
    )
    expected = _latest_usd_per_unit(db_session, "USD") / _latest_usd_per_unit(
        db_session, "INR"
    )
    assert rate == expected
    assert rate > Decimal("1")  # one USD is worth many INR


def test_currency_case_insensitive(db_session: Session) -> None:
    upper = resolve_rate(
        db_session, paid_currency="USD", functional_currency="SGD", on_date=_ON
    )
    lower = resolve_rate(
        db_session, paid_currency="usd", functional_currency="sgd", on_date=_ON
    )
    assert upper == lower


def test_convert_quantizes_to_ledger_scale(db_session: Session) -> None:
    rate, local = convert(
        db_session,
        amount=Decimal("100"),
        paid_currency="USD",
        functional_currency="INR",
        on_date=_ON,
    )
    assert local == (Decimal("100") * rate).quantize(Decimal("0.0001"))
    assert local > Decimal("0")


def test_unknown_currency_fails_closed(db_session: Session) -> None:
    with pytest.raises(FxRateUnavailableError):
        resolve_rate(
            db_session, paid_currency="JPY", functional_currency="INR", on_date=_ON
        )


def test_effective_dating_picks_latest_on_or_before(db_session: Session) -> None:
    db_session.add(
        FxRate(
            currency="JPY",
            usd_per_unit=Decimal("0.0060"),
            effective_from=date(2026, 1, 1),
            source="test",
        )
    )
    db_session.add(
        FxRate(
            currency="JPY",
            usd_per_unit=Decimal("0.0070"),
            effective_from=date(2026, 5, 1),
            source="test",
        )
    )
    db_session.flush()

    # On 2026-06-01 the 2026-05-01 rate is in force.
    later = resolve_rate(
        db_session,
        paid_currency="JPY",
        functional_currency="USD",
        on_date=date(2026, 6, 1),
    )
    assert later == Decimal("0.0070") / _latest_usd_per_unit(db_session, "USD")

    # On 2026-02-01 the earlier 2026-01-01 rate is in force.
    earlier = resolve_rate(
        db_session,
        paid_currency="JPY",
        functional_currency="USD",
        on_date=date(2026, 2, 1),
    )
    assert earlier == Decimal("0.0060") / _latest_usd_per_unit(db_session, "USD")
