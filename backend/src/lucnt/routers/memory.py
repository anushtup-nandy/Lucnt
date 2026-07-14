"""Memory router (Phase 5, M8) — an agent's learned financial patterns.

Reads the `memories` table that `POST /intelligence/recompute` populates (one
memory per (agent, type)); read-only, computed nowhere on the request path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import Agent, Company, Memory
from lucnt.schemas.intelligence import MemoryListResponse, MemoryResponse

router = APIRouter(prefix="/memory", tags=["memory"])


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
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    agent: str | None = Query(default=None, description="Filter by agent slug"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> MemoryListResponse:
    """List learned patterns for the company's agents, newest-learned first."""
    stmt = select(Memory).where(Memory.company_id == company.id)
    if agent is not None:
        agent_row = db.execute(
            select(Agent).where(
                Agent.company_id == company.id, Agent.slug == agent
            )
        ).scalar_one_or_none()
        if agent_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {agent}")
        stmt = stmt.where(Memory.agent_id == agent_row.id)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Memory.updated_at.desc()).offset(offset).limit(limit)
        )
        .scalars()
        .all()
    )
    return MemoryListResponse(
        items=[_to_response(row) for row in rows], total=total
    )
