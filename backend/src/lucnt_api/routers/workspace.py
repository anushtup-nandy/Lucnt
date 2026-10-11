"""Workspace reads: legal entities and team members.

Two small collections behind the Settings page, plus `GET /entities`, which the
workspace switcher in the top bar needs on every page. Both are
configuration-shaped — bounded by how many entities and people a customer has,
not by ledger volume — so they use the wide page bound.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_db,
    require_nav,
)
from lucnt_api.models import Entity
from lucnt_api.models.user import User
from lucnt_api.pagination import (
    WIDE_MAX_PAGE_SIZE,
    WIDE_PAGE_SIZE,
    Page,
    count_total,
    paginate,
)
from lucnt_api.schemas.auth import UserListResponse, UserResponse
from lucnt_api.schemas.workspace import EntityListResponse, EntityResponse

router = APIRouter(tags=["workspace"])


@router.get("/entities", response_model=EntityListResponse)
def list_entities(
    # Deliberately NOT gated on the `settings` nav item: the entity switcher is
    # in the top bar of every page, so every role needs to read this list.
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    page: Page = Depends(paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)),
) -> EntityListResponse:
    """List the company's legal entities, for the switcher and Settings."""
    stmt = select(Entity).where(Entity.company_id == ctx.company.id)
    total = count_total(db, stmt)
    rows = (
        db.execute(stmt.order_by(Entity.code).offset(page.offset).limit(page.limit))
        .scalars()
        .all()
    )
    return EntityListResponse(
        items=[EntityResponse.model_validate(row) for row in rows], total=total
    )


@router.get("/users", response_model=UserListResponse)
def list_users(
    ctx: AuthContext = Depends(require_nav("settings")),
    db: Session = Depends(get_db),
    page: Page = Depends(paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)),
) -> UserListResponse:
    """List the company's team members.

    Name, email, role and active flag only — no password hash, no last-login
    timestamp, nothing the Settings table does not render.
    """
    stmt = select(User).where(User.company_id == ctx.company.id)
    total = count_total(db, stmt)
    rows = (
        db.execute(stmt.order_by(User.name).offset(page.offset).limit(page.limit))
        .scalars()
        .all()
    )
    return UserListResponse(
        items=[
            UserResponse(
                id=row.id,
                email=row.email,
                name=row.name,
                role=row.role.value,
                is_active=row.is_active,
            )
            for row in rows
        ],
        total=total,
    )
