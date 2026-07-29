"""Agents router — read-only (backend/PLAN.md §4).

**Owner scoping (F-5, P7.0).** The `agent_owner` persona sees only agents whose
`owner_user_id` is theirs, and that narrowing happens **in the SQL** — see
`scope_agents`. Filtering after the query would still have read the other
owner's rows out of the database, and would make `total` count rows the caller
may not see. F-5 closes on the cross-owner leak test, not on a hidden nav item.

`PATCH /agents/{id}` remains out of scope (`frontend/PLAN.md` §14): the auth this
phase adds unblocks it, but nothing in P7 needs it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import AuthContext, get_db, require_nav
from lucnt_api.models import Agent
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.agent import AgentDetail, AgentListResponse
from lucnt_api.services.lookups import entity_or_404
from lucnt_api.services.permissions import scope_agents

# Router-level nav gate (P7.0): a human principal may only reach a domain
# present in `perms.nav` — the same tuple the sidebar renders from, so a
# hidden page and a reachable endpoint cannot drift apart. No-op for API keys.
router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_nav("agents"))],
)


@router.get("", response_model=AgentListResponse)
def list_agents(
    ctx: AuthContext = Depends(require_nav("agents")),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate()),
) -> AgentListResponse:
    """List agents for the current company, filterable/paginated and owner-scoped."""
    stmt = select(Agent).where(Agent.company_id == ctx.company.id)
    stmt = scope_agents(stmt, ctx.perms, ctx.user.id if ctx.user else None)
    if entity is not None:
        entity_row = entity_or_404(db, ctx.company, entity)
        stmt = stmt.where(Agent.entity_id == entity_row.id)

    total = count_total(db, stmt)
    rows = (
        db.execute(stmt.order_by(Agent.name).offset(page.offset).limit(page.limit))
        .scalars()
        .all()
    )
    return AgentListResponse(
        items=[AgentDetail.model_validate(row) for row in rows], total=total
    )


@router.get("/{agent_id}", response_model=AgentDetail)
def get_agent(
    agent_id: uuid.UUID,
    ctx: AuthContext = Depends(require_nav("agents")),
    db: Session = Depends(get_db),
) -> AgentDetail:
    """Full detail for a single agent.

    A scoped-out agent is a 404, not a 403: telling an `agent_owner` that a given
    id exists but belongs to someone else is itself a disclosure.
    """
    stmt = select(Agent).where(Agent.id == agent_id, Agent.company_id == ctx.company.id)
    stmt = scope_agents(stmt, ctx.perms, ctx.user.id if ctx.user else None)
    agent = db.execute(stmt).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown agent")
    return AgentDetail.model_validate(agent)
