"""Tax-rate resolver tests against the migration-seeded tax_rates (T1-12).

Integration, not pure-unit: the resolver reads `tax_rates`, whose statutory rows
(IN/SG/US) are seeded by migration `b1c2d3e4f5a6`, so they exist in the migrated
test DB. Effective-dating (SG GST 7%→8%→9%) is the finding under test — a rate
change must not back-apply to a historical supply.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from lucnt_api.services.tax_rates import TaxRateUnavailableError, resolve_rate


def test_india_standard_rate(db_session: Session) -> None:
    rate = resolve_rate(db_session, regime="IN-GST", on_date=date(2026, 6, 1))
    assert rate == Decimal("0.18")


def test_us_rate_is_zero_not_missing(db_session: Session) -> None:
    """US resolves to 0 (seeded), so the money path never fails closed on US."""
    rate = resolve_rate(db_session, regime="US-Sales-Tax", on_date=date(2026, 6, 1))
    assert rate == Decimal("0")


def test_singapore_gst_effective_dating(db_session: Session) -> None:
    """SG GST 7%→8%→9% is picked by transaction date, never back-applied (T1-12)."""
    assert resolve_rate(
        db_session, regime="SG-GST", on_date=date(2020, 6, 1)
    ) == Decimal("0.07")
    assert resolve_rate(
        db_session, regime="SG-GST", on_date=date(2023, 6, 1)
    ) == Decimal("0.08")
    assert resolve_rate(
        db_session, regime="SG-GST", on_date=date(2026, 6, 1)
    ) == Decimal("0.09")


def test_before_earliest_effective_fails_closed(db_session: Session) -> None:
    """A date before any rate exists is fail-closed, never a guessed rate."""
    with pytest.raises(TaxRateUnavailableError):
        resolve_rate(db_session, regime="IN-GST", on_date=date(2000, 1, 1))


def test_unknown_regime_fails_closed(db_session: Session) -> None:
    with pytest.raises(TaxRateUnavailableError):
        resolve_rate(db_session, regime="ZZ-Unknown", on_date=date(2026, 6, 1))


def test_unknown_category_fails_closed(db_session: Session) -> None:
    """A slab that is not configured is fail-closed, never a guessed rate."""
    with pytest.raises(TaxRateUnavailableError):
        resolve_rate(
            db_session, regime="IN-GST", category="gst_99", on_date=date(2026, 6, 1)
        )


def test_india_gst_slabs_resolve(db_session: Session) -> None:
    """The India GST slabs seeded for T1-10 resolve to their statutory rates."""
    assert resolve_rate(
        db_session, regime="IN-GST", category="gst_5", on_date=date(2026, 6, 1)
    ) == Decimal("0.05")
    assert resolve_rate(
        db_session, regime="IN-GST", category="gst_28", on_date=date(2026, 6, 1)
    ) == Decimal("0.28")
