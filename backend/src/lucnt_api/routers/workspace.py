"""Workspace reads: legal entities and team members (wide page bound)."""

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
    # Not nav-gated: every role's top-bar entity switcher reads this.
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
    """List the company's team members (no password hash or last-login)."""
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
