"""India s.195 withholding on payments to non-residents (in addition to GST RCM).

Pure. Runs at authorize because a bank won't remit without Form 15CA/15CB. Rates
come from config and are provisional; confirm with a CA. GL leg not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_FOUR_PLACES = Decimal("0.0001")
_ZERO = Decimal("0")
_IN = "IN"


@dataclass(frozen=True)
class WithholdingOutcome:
    """The s.195 withholding verdict for one India→non-resident payment."""

    applies: bool
    rate: Decimal  # fraction (0.15 = 15%)
    amount: Decimal
    treaty_rate_applied: bool  # DTAA rate needs a valid TRC + Form 10F
    blocked: bool  # hold remittance pending documentation
    reason: str


def determine_withholding(
    *,
    buyer_country: str | None,
    vendor_country: str | None,
    functional_amount: Decimal,
    on_date: date,
    trc_on_file: bool,
    trc_expiry: date | None,
    form_10f_on_file: bool,
    treaty_rate: Decimal,
    domestic_rate: Decimal,
    cb_threshold: Decimal,
) -> WithholdingOutcome:
    """Compute s.195 withholding for a payment.

    An unenriched ``vendor_country`` (None) is not applicable; the capture-time
    UNCLASSIFIED flag handles it. A ``trc_expiry`` of None counts as unexpired.
    """
    if buyer_country != _IN or vendor_country is None or vendor_country.upper() == _IN:
        return WithholdingOutcome(
            applies=False,
            rate=_ZERO,
            amount=_ZERO,
            treaty_rate_applied=False,
            blocked=False,
            reason="not an India → non-resident payment; s.195 does not apply",
        )

    docs_valid = (
        trc_on_file
        and form_10f_on_file
        and (trc_expiry is None or trc_expiry >= on_date)
    )
    rate = treaty_rate if docs_valid else domestic_rate
    amount = (functional_amount * rate).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)

    # Above the 15CB threshold an uncertifiable treaty position is held, not sent;
    # below it the domestic rate applies and the payment proceeds.
    blocked = (not docs_valid) and functional_amount >= cb_threshold
    if blocked:
        reason = (
            "s.195 remittance at/above the Form 15CB threshold without a valid TRC + "
            "Form 10F on file — held for withholding documentation before remittance"
        )
    elif docs_valid:
        reason = f"s.195 treaty rate {rate} applied (valid TRC + Form 10F on file)"
    else:
        reason = (
            f"s.195 withholding at the domestic default rate {rate} "
            "(no valid TRC/Form 10F — DTAA treaty rate not available)"
        )

    return WithholdingOutcome(
        applies=True,
        rate=rate,
        amount=amount,
        treaty_rate_applied=docs_valid,
        blocked=blocked,
        reason=reason,
    )
