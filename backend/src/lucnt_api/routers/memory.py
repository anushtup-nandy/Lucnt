"""Memory router: agents' learned patterns, populated by intelligence recompute."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import AuthContext, get_db, require_nav
from lucnt_api.models import Agent, Memory
from lucnt_api.pagination import (
    WIDE_MAX_PAGE_SIZE,
    WIDE_PAGE_SIZE,
    Page,
    count_total,
    paginate,
)
from lucnt_api.schemas.intelligence import MemoryListResponse, MemoryResponse
from lucnt_api.services.lookups import agent_or_404, entity_or_404
from lucnt_api.services.permissions import scope_agents

router = APIRouter(
    prefix="/memory",
    tags=["memory"],
    dependencies=[Depends(require_nav("memory"))],
)


def _to_response(memory: Memory) -> MemoryResponse:
    """Map a Memory ORM row to its API response (enum -> str)."""
    return MemoryResponse(
        id=memory.id,
        agent_id=memory.agent_id,
        memory_type=memory.memory_type.value,
        summary=memory.summary,
        detail_text=memory.detail_text,
        metrics_json=memory.metrics_json,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.get("", response_model=MemoryListResponse)
def list_memories(
    ctx: AuthContext = Depends(require_nav("memory")),
    db: Session = Depends(get_db),
    agent: str | None = Query(default=None, description="Filter by agent slug"),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)),
) -> MemoryListResponse:
    """List learned patterns for the company's agents, newest-learned first."""
    company = ctx.company
    stmt = select(Memory).where(Memory.company_id == company.id)
    if ctx.perms is not None and ctx.perms.agent_scope == "owned":
        stmt = scope_agents(
            stmt.join(Agent, Agent.id == Memory.agent_id),
            ctx.perms,
            ctx.user.id if ctx.user else None,
        )
    if agent is not None:
        stmt = stmt.where(Memory.agent_id == agent_or_404(db, company, agent).id)
    if entity is not None:
        entity_row = entity_or_404(db, company, entity)
        stmt = stmt.join(Agent, Agent.id == Memory.agent_id).where(
            Agent.entity_id == entity_row.id
        )

    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Memory.updated_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return MemoryListResponse(items=[_to_response(row) for row in rows], total=total)
