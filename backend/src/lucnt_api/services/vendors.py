"""Vendor registry get-or-create used by the capture path.

New vendors are unenriched, so the tax engine uses its currency fallback. The
insert is savepoint-isolated so a race never 500s after the vendor was paid.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.models import Vendor


def normalize_vendor_name(name: str) -> str:
    """The lookup key for a vendor name (trimmed, lower-cased)."""
    return name.strip().lower()


def resolve_or_create_vendor(db: Session, company_id: uuid.UUID, name: str) -> Vendor:
    """Return the vendor by normalized name, creating an unenriched one.

    Does not commit; the caller's capture commits it with the ledger write.
    """
    normalized = normalize_vendor_name(name)
    existing = db.execute(
        select(Vendor).where(
            Vendor.company_id == company_id,
            Vendor.normalized_name == normalized,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    vendor = Vendor(
        company_id=company_id,
        name=name,
        normalized_name=normalized,
    )
    try:
        with db.begin_nested():
            db.add(vendor)
            db.flush()
        return vendor
    except IntegrityError:
        # Lost the race to a concurrent capture: adopt the committed row.
        return db.execute(
            select(Vendor).where(
                Vendor.company_id == company_id,
                Vendor.normalized_name == normalized,
            )
        ).scalar_one()
