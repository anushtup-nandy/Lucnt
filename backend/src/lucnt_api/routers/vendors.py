"""Vendors router — the place-of-supply registry (PRODUCTION_PLAN T1-3/T1-9).

A vendor's country/state/registration decide RCM and the CGST/SGST/IGST head split
at capture. Spends auto-create unenriched vendor rows (`services/vendors.py`); a
finance admin enriches them here so future spends are taxed by place of supply
rather than the currency fallback. Mutations are admin-scoped and audited (T0-12/
T0-14); reads are company-scoped.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_admin,
)
from lucnt_api.models import Company, Vendor
from lucnt_api.schemas.vendor import (
    VendorCreate,
    VendorListResponse,
    VendorRead,
    VendorUpdate,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.tax_reclass import reflag_divergent_history
from lucnt_api.services.vendors import normalize_vendor_name

router = APIRouter(prefix="/vendors", tags=["vendors"])

# Fields whose change can alter a spend's tax regime — a change to any of them
# re-checks the vendor's booked history for divergence (TE-1c).
_PLACE_OF_SUPPLY_FIELDS = frozenset({"country", "state", "gst_registered_in"})


def _require_vendor(db: Session, company: Company, vendor_id: uuid.UUID) -> Vendor:
    """Fetch a company-scoped vendor (404 if unknown — cross-tenant safe)."""
    vendor = db.execute(
        select(Vendor).where(
            Vendor.id == vendor_id, Vendor.company_id == company.id
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown vendor")
    return vendor


@router.get("", response_model=VendorListResponse)
def list_vendors(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> VendorListResponse:
    """List the company's vendors, newest first, paginated."""
    stmt = select(Vendor).where(Vendor.company_id == company.id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Vendor.created_at.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )
    return VendorListResponse(
        items=[VendorRead.model_validate(row) for row in rows], total=total
    )


@router.get("/{vendor_id}", response_model=VendorRead)
def get_vendor(
    vendor_id: uuid.UUID,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> VendorRead:
    """One vendor's detail."""
    return VendorRead.model_validate(_require_vendor(db, company, vendor_id))


@router.post("", response_model=VendorRead, status_code=status.HTTP_201_CREATED)
def create_vendor(
    request: VendorCreate,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> VendorRead:
    """Register a vendor with its place of supply (admin, audited).

    A 409 if a vendor with the same (normalized) name already exists — enrich that
    one via `PATCH /vendors/{id}` instead.
    """
    normalized = normalize_vendor_name(request.name)
    if db.execute(
        select(Vendor.id).where(
            Vendor.company_id == company.id,
            Vendor.normalized_name == normalized,
        )
    ).scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"vendor {request.name!r} already exists"
        )

    vendor = Vendor(
        company_id=company.id,
        name=request.name,
        normalized_name=normalized,
        country=request.country,
        state=request.state,
        gstin=request.gstin,
        gst_registered_in=request.gst_registered_in,
        trc_on_file=request.trc_on_file,
        trc_expiry=request.trc_expiry,
        form_10f_on_file=request.form_10f_on_file,
    )
    db.add(vendor)
    db.flush()
    record_audit(
        db,
        ctx.api_key,
        action="vendor.create",
        entity_type="vendor",
        entity_id=vendor.id,
        before=None,
        after={
            "name": vendor.name,
            "country": vendor.country,
            "state": vendor.state,
            "gst_registered_in": vendor.gst_registered_in,
        },
    )
    db.commit()
    return VendorRead.model_validate(vendor)


@router.patch("/{vendor_id}", response_model=VendorRead)
def update_vendor(
    vendor_id: uuid.UUID,
    request: VendorUpdate,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> VendorRead:
    """Enrich a vendor's place of supply (admin, audited).

    Only fields present in the request are updated (PATCH semantics), so a spend's
    auto-created row can be filled in without disturbing the rest.
    """
    vendor = _require_vendor(db, company, vendor_id)
    before = {
        "country": vendor.country,
        "state": vendor.state,
        "gst_registered_in": list(vendor.gst_registered_in),
    }
    updated = request.model_dump(exclude_unset=True)
    for field, value in updated.items():
        setattr(vendor, field, value)
    db.flush()
    # TE-1c: if place of supply changed, re-check the vendor's already-booked spends
    # and flag any whose regime now diverges — never rewriting history.
    if _PLACE_OF_SUPPLY_FIELDS & updated.keys():
        reflag_divergent_history(db, company.id, vendor)
    record_audit(
        db,
        ctx.api_key,
        action="vendor.update",
        entity_type="vendor",
        entity_id=vendor.id,
        before=before,
        after={
            "country": vendor.country,
            "state": vendor.state,
            "gst_registered_in": list(vendor.gst_registered_in),
        },
    )
    db.commit()
    return VendorRead.model_validate(vendor)
