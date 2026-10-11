"""Authorizations router — the verdict list behind the Transactions chips (D5).

`Transaction.status` has exactly one member, `completed`, so a captures-only
list cannot show a blocked spend. Blocked, flagged and held spends are
`authorizations` rows (`VerdictStatus`), which had no list endpoint until now.

**One chip, one endpoint, one status value**. `status`
takes a single value rather than a comma list precisely so pagination stays
correct: a client-side union over paginated pages silently reports "3 of 3"
while page 2 holds four more.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import AuthContext, get_db, require_nav
from lucnt_api.models import Agent, Authorization, Entity
from lucnt_api.models.authorization import VerdictStatus
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.transaction import (
    AuthorizationListResponse,
    AuthorizationSummary,
)
from lucnt_api.services.permissions import scope_agents

router = APIRouter(prefix="/authorizations", tags=["authorizations"])


@router.get("", response_model=AuthorizationListResponse)
def list_authorizations(
    ctx: AuthContext = Depends(require_nav("transactions")),
    db: Session = Depends(get_db),
    status_filter: VerdictStatus | None = Query(
        default=None, alias="status", description="Filter by verdict status"
    ),
    agent: str | None = Query(default=None, description="Filter by agent slug"),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate()),
) -> AuthorizationListResponse:
    """List policy verdicts for the current company, newest first.

    Joined to `agents` so the response can carry the agent's name and glyph
    without the dashboard issuing a lookup per row, and so `agent_owner` scoping
    happens in this one query rather than after the fact.
    """
    company = ctx.company
    stmt = (
        select(Authorization, Agent)
        .join(Agent, Agent.id == Authorization.agent_id)
        .where(Authorization.company_id == company.id)
    )
    stmt = scope_agents(stmt, ctx.perms, ctx.user.id if ctx.user else None)

    if status_filter is not None:
        stmt = stmt.where(Authorization.status == status_filter)
    if agent is not None:
        stmt = stmt.where(Agent.slug == agent)
    if entity is not None:
        entity_row = db.execute(
            select(Entity).where(Entity.company_id == company.id, Entity.code == entity)
        ).scalar_one_or_none()
        if entity_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {entity}")
        stmt = stmt.where(Authorization.entity_id == entity_row.id)

    total = count_total(db, stmt)
    rows = db.execute(
        stmt.order_by(Authorization.created_at.desc())
        .offset(page.offset)
        .limit(page.limit)
    ).all()
    return AuthorizationListResponse(
        items=[
            AuthorizationSummary(
                id=auth.id,
                agent_id=auth.agent_id,
                agent_name=agent_row.name,
                agent_glyph=agent_row.glyph,
                entity_id=auth.entity_id,
                vendor=auth.vendor,
                amount=auth.amount,
                currency=auth.currency,
                reason=auth.reason,
                intent=auth.intent,
                project=auth.project,
                status=auth.status.value,
                verdict_code=auth.verdict_code,
                reason_text=auth.reason_text,
                created_at=auth.created_at,
            )
            for auth, agent_row in rows
        ],
        total=total,
    )
