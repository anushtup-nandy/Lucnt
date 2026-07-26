"""Agents router — read-only in Phase 1 (backend/PLAN.md §4).

`PATCH /agents/{id}` is spec'd "owner-scoped," which needs JWT human-user auth
(root PLAN.md Phase 0, still unchecked) to know the current user's
`owner_user_id`. It's deferred rather than shipped as a fake owner check.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import get_current_company, get_db
from lucnt_api.models import Agent, Company, Entity
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.agent import AgentDetail, AgentListResponse

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=AgentListResponse)
def list_agents(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate()),
) -> AgentListResponse:
    """List agents for the current company, filterable/paginated."""
    stmt = select(Agent).where(Agent.company_id == company.id)
    if entity is not None:
        entity_row = db.execute(
            select(Entity).where(Entity.company_id == company.id, Entity.code == entity)
        ).scalar_one_or_none()
        if entity_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {entity}")
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
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> AgentDetail:
    """Full detail for a single agent."""
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.company_id == company.id)
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown agent")
    return AgentDetail.model_validate(agent)
