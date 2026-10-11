"""Re-flag booked spends when a vendor's place of supply changes.

History is immutable: divergent transactions go to the `TAX_UNCLASSIFIED` recon
queue for a human. Compares only `tax_type`, not head split or reclaimability.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt_api.models import (
    Entity,
    ReconciliationException,
    ReconExceptionType,
    TaxRecord,
    Transaction,
    Vendor,
)
from lucnt_api.services.tax_engine import compute_tax
from lucnt_api.services.tax_rates import (
    STANDARD_CATEGORY,
    TaxRateUnavailableError,
    resolve_rate,
)

_ZERO = Decimal("0")


def reflag_divergent_history(db: Session, company_id: uuid.UUID, vendor: Vendor) -> int:
    """Flag spends whose booked regime disagrees with the enriched vendor.

    Recomputes at each transaction's own supply date; idempotent per transaction.
    Adds exceptions without committing.

    Returns:
        The number of transactions newly flagged.
    """
    txns = list(
        db.execute(
            select(Transaction).where(
                Transaction.company_id == company_id,
                func.lower(func.trim(Transaction.vendor)) == vendor.normalized_name,
                # Reversals are contras of an original; the original carries the flag.
                Transaction.reverses_transaction_id.is_(None),
            )
        ).scalars()
    )

    flagged = 0
    for txn in txns:
        entity = db.get(Entity, txn.entity_id)
        if entity is None:  # FK-guaranteed; defensive
            continue
        try:
            rate = resolve_rate(
                db,
                regime=entity.tax_regime,
                category=txn.tax_category or STANDARD_CATEGORY,
                on_date=txn.transaction_date.date(),
            )
        except TaxRateUnavailableError:
            # Cannot recompute without a rate; leave as-is.
            continue

        recomputed = compute_tax(
            tax_regime=entity.tax_regime,
            functional_amount=txn.local_amount,
            paid_currency=txn.paid_currency,
            entity_currency=entity.currency,
            rate=rate,
            expense_category=txn.category,
            tax_inclusive=txn.tax_inclusive,
            vendor_country=vendor.country,
            vendor_state=vendor.state,
            vendor_registered_in=tuple(vendor.gst_registered_in),
            entity_country=entity.country,
            entity_state=entity.gst_state,
        )

        booked_type = db.execute(
            select(TaxRecord.tax_type)
            .where(TaxRecord.transaction_id == txn.id)
            .limit(1)
        ).scalar_one_or_none()
        if booked_type is None or booked_type == recomputed.tax_type:
            continue  # nothing booked, or the regime is unchanged

        already = db.execute(
            select(ReconciliationException.id).where(
                ReconciliationException.transaction_id == txn.id,
                ReconciliationException.exception_type
                == ReconExceptionType.TAX_UNCLASSIFIED,
            )
        ).scalar_one_or_none()
        if already is not None:
            continue

        db.add(
            ReconciliationException(
                company_id=company_id,
                transaction_id=txn.id,
                exception_type=ReconExceptionType.TAX_UNCLASSIFIED,
                amount_at_stake=_ZERO,
                currency=txn.paid_currency,
                evidence_json={
                    "reason": "vendor_reclassified",
                    "vendor": txn.vendor,
                    "booked_tax_type": booked_type,
                    "recomputed_tax_type": recomputed.tax_type,
                    "detail": (
                        "vendor place of supply changed; the booked tax no longer "
                        "matches — review and post a correcting adjustment if needed"
                    ),
                },
                fix_notes=(
                    f"Vendor {txn.vendor!r} reclassified {booked_type} -> "
                    f"{recomputed.tax_type}; review this spend's tax (history is "
                    "not auto-rewritten)."
                ),
            )
        )
        flagged += 1

    return flagged
