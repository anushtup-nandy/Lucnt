"""Developer reads: API keys and the audit event log (frontend/PLAN.md §10.2).

Both are gated on the `developers` nav domain, so only roles that can see the
page can call the endpoints behind it.

`GET /api-keys` returns **prefix and metadata only**. Nothing else is possible:
`api_keys.key_hash` is a SHA-256 digest, so a plaintext key exists exactly once,
in the response to whoever created it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import AuthContext, get_db, require_nav
from lucnt_api.models import ApiKey, AuditLog
from lucnt_api.pagination import (
    WIDE_MAX_PAGE_SIZE,
    WIDE_PAGE_SIZE,
    Page,
    count_total,
    paginate,
)
from lucnt_api.schemas.workspace import (
    ApiKeyListResponse,
    ApiKeySummary,
    AuditLogEntry,
    AuditLogListResponse,
)

router = APIRouter(tags=["developers"])


@router.get("/api-keys", response_model=ApiKeyListResponse)
def list_api_keys(
    ctx: AuthContext = Depends(require_nav("developers")),
    db: Session = Depends(get_db),
    page: Page = Depends(paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)),
) -> ApiKeyListResponse:
    """List the company's API keys, masked to prefix + metadata."""
    stmt = select(ApiKey).where(ApiKey.company_id == ctx.company.id)
    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(ApiKey.created_at.desc()).offset(page.offset).limit(page.limit)
        )
        .scalars()
        .all()
    )
    return ApiKeyListResponse(
        items=[
            ApiKeySummary(
                id=row.id,
                name=row.name,
                key_prefix=row.key_prefix,
                mode=row.mode.value,
                scope=row.scope.value,
                is_active=row.is_active,
                last_used_at=row.last_used_at,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
def list_audit_logs(
    ctx: AuthContext = Depends(require_nav("developers")),
    db: Session = Depends(get_db),
    action: str | None = Query(default=None, max_length=100),
    page: Page = Depends(paginate()),
) -> AuditLogListResponse:
    """The append-only governance trail, newest first."""
    stmt = select(AuditLog).where(AuditLog.company_id == ctx.company.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(AuditLog.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return AuditLogListResponse(
        items=[AuditLogEntry.model_validate(row) for row in rows], total=total
    )
