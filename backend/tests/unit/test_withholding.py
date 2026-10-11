"""Pure India s.195 withholding engine tests; rates/threshold injected."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from lucnt_api.services.withholding import determine_withholding

_TREATY = Decimal("0.15")
_DOMESTIC = Decimal("0.20")
_THRESHOLD = Decimal("500000")
_TODAY = date(2026, 7, 22)


def _wh(**kw: object) -> object:
    """determine_withholding with the standard rates/threshold + sensible defaults."""
    params = {
        "buyer_country": "IN",
        "vendor_country": "US",
        "functional_amount": Decimal("1000"),
        "on_date": _TODAY,
        "trc_on_file": False,
        "trc_expiry": None,
        "form_10f_on_file": False,
        "treaty_rate": _TREATY,
        "domestic_rate": _DOMESTIC,
        "cb_threshold": _THRESHOLD,
    }
    params.update(kw)
    return determine_withholding(**params)  # type: ignore[arg-type]


def test_not_india_buyer_does_not_apply() -> None:
    """s.195 is India-specific — a non-IN buyer owes no withholding here."""
    t = _wh(buyer_country="US")
    assert t.applies is False
    assert t.amount == Decimal("0")


def test_domestic_vendor_does_not_apply() -> None:
    """A resident (India) vendor is not a s.195 non-resident payment."""
    assert _wh(vendor_country="IN").applies is False


def test_unenriched_vendor_does_not_apply() -> None:
    """An unknown vendor country cannot be a non-resident payment (caught elsewhere)."""
    assert _wh(vendor_country=None).applies is False


def test_no_docs_uses_domestic_rate() -> None:
    """Without a valid TRC + Form 10F the higher domestic default rate applies."""
    t = _wh(functional_amount=Decimal("1000"))
    assert t.applies is True
    assert t.treaty_rate_applied is False
    assert t.rate == _DOMESTIC
    assert t.amount == Decimal("200.0000")  # 1000 * 20%


def test_valid_docs_use_treaty_rate() -> None:
    """A valid, unexpired TRC + Form 10F earns the lower DTAA treaty rate."""
    t = _wh(
        functional_amount=Decimal("1000"),
        trc_on_file=True,
        form_10f_on_file=True,
        trc_expiry=date(2027, 3, 31),
    )
    assert t.treaty_rate_applied is True
    assert t.rate == _TREATY
    assert t.amount == Decimal("150.0000")  # 1000 * 15%
    assert t.blocked is False


def test_expired_trc_falls_back_to_domestic() -> None:
    """An expired TRC does not earn the treaty rate."""
    t = _wh(trc_on_file=True, form_10f_on_file=True, trc_expiry=date(2025, 1, 1))
    assert t.treaty_rate_applied is False
    assert t.rate == _DOMESTIC


def test_missing_docs_above_threshold_blocks() -> None:
    """Above the 15CB threshold without valid docs → held for documentation."""
    t = _wh(functional_amount=Decimal("600000"))
    assert t.applies is True
    assert t.blocked is True
    assert "15CB" in t.reason or "documentation" in t.reason


def test_missing_docs_below_threshold_does_not_block() -> None:
    """Below the threshold the (domestic) rate simply applies — no block."""
    t = _wh(functional_amount=Decimal("10000"))
    assert t.applies is True
    assert t.blocked is False
    assert t.rate == _DOMESTIC


def test_valid_docs_above_threshold_not_blocked() -> None:
    """Valid docs → no block even above the 15CB threshold."""
    t = _wh(
        functional_amount=Decimal("600000"),
        trc_on_file=True,
        form_10f_on_file=True,
    )
    assert t.blocked is False
    assert t.treaty_rate_applied is True
