"""Vendor registry resolution (PRODUCTION_PLAN T1-3/T1-9).

`resolve_or_create_vendor` is the get-or-create the capture path uses to attach a
place-of-supply record to every spend. A new vendor is created **unregistered**
with no place-of-supply data, so the tax engine falls back to the currency
heuristic until a finance admin enriches it via the vendors CRUD — the caller
never has to change the frozen wire contract (``vendor`` stays a free string).

Concurrency-safe like `chart_of_accounts.ensure_default_accounts`: the insert is
savepoint-isolated and the committed row is adopted on the unique-constraint race,
so a first-ever spend to a vendor never 500s *after* the vendor was paid.
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
    """Return the company's vendor by normalized name, creating an unenriched one.

    Does not commit — the caller owns the transaction (the capture that resolves
    the vendor commits it together with the ledger write).
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

    # Unenriched: no country and registered nowhere (gst_registered_in defaults to
    # []). The tax engine routes such a vendor conservatively and flags it (TE-1b).
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
        # A concurrent capture created it first — adopt the committed row.
        return db.execute(
            select(Vendor).where(
                Vendor.company_id == company_id,
                Vendor.normalized_name == normalized,
            )
        ).scalar_one()
