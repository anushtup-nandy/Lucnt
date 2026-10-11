"""India s.195 withholding-tax engine.

A **second, independent regime check beside GST**: every India-entity → foreign
(non-resident) vendor payment owes income-tax withholding under s.195, *in
addition to* GST reverse charge. This is the one tax path that can **block a
payment** — a bank will not process the remittance without a Form 15CA (often a
CA-certified 15CB) — so the gate runs at authorize, before money moves.

Pure and SQLAlchemy-free, like the tax/policy engines: it takes value inputs and
returns a `WithholdingOutcome`. The money-path boundary supplies the vendor's
documentation (TRC / Form 10F, from the registry) and the effective rates/threshold
(from config).

**Rates are provisional (config, not hard-coded here) — confirm with a CA.** The
DTAA treaty rate applies only with a valid, unexpired Tax Residency Certificate +
Form 10F; otherwise the higher domestic default applies. Above the 15CB threshold a
CA certificate is required, so a remittance whose treaty position cannot be
certified (docs missing) is **held for documentation** rather than remitted blind.

**Not modelled here (deferred):** the double-entry GL leg (a TDS-payable liability
+ any gross-up — needs CA guidance on who bears the tax) and the actual 15CA/15CB
document generation (the record carries nullable form ids).
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

    applies: bool  # True only for an India entity paying a non-resident vendor
    rate: Decimal  # the applied rate as a fraction (0.15 = 15%)
    amount: Decimal  # withholding on the functional amount (0 when not applicable)
    treaty_rate_applied: bool  # DTAA treaty rate (valid TRC + 10F) vs domestic default
    blocked: bool  # halt the remittance pending documentation (the money-path gate)
    reason: str  # human-readable summary for the verdict / record


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

    Args:
        buyer_country: The paying entity's ISO country. Withholding applies only
            when this is ``"IN"``.
        vendor_country: The vendor's ISO country. A non-resident (``!= "IN"``) is
            the s.195 case; ``None`` (unenriched) ⇒ withholding cannot be
            determined and does not apply (the capture-time UNCLASSIFIED flag
            handles that vendor instead).
        functional_amount: The payment in the entity's functional currency.
        on_date: The transaction date, for TRC-expiry evaluation.
        trc_on_file: Whether the vendor's Tax Residency Certificate is on file.
        trc_expiry: The TRC's expiry date (``None`` ⇒ treated as unexpired).
        form_10f_on_file: Whether the vendor's Form 10F is on file.
        treaty_rate: The DTAA treaty rate (applied with a valid TRC + Form 10F).
        domestic_rate: The domestic default rate (applied when docs are missing).
        cb_threshold: The Form-15CB threshold above which missing docs block —
            all three injected from config (provisional; confirm with a CA).

    Returns:
        A `WithholdingOutcome`. ``blocked`` is True when the treaty position cannot
        be certified (docs missing/expired) and the amount is at/above the 15CB
        threshold — a remittance a bank would not process without a 15CB.
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

    # Above the 15CB threshold a CA certificate is required; without a valid TRC +
    # Form 10F the treaty position cannot be certified, so the remittance is held for
    # documentation rather than sent blind. Below the threshold the (higher domestic)
    # rate simply applies and the payment proceeds.
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
