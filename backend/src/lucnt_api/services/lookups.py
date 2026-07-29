"""Company-scoped lookups shared by routers (`CLAUDE.md` §2.3, DRY).

`entity_or_404` was written inline in `routers/transactions.py`, again in
`routers/agents.py`, and would have been written four more times by the P7.0
`entity=` filters. Three or more call sites means it belongs in one place —
especially this one, whose whole job is the `company_id` predicate that makes it
safe. A copy that forgets that predicate is a cross-tenant read.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.models import Agent, Company, Entity

__all__ = ["entity_or_404", "agent_or_404"]


def entity_or_404(db: Session, company: Company, code: str) -> Entity:
    """Look up a legal entity by code, scoped to the current company."""
    entity = db.execute(
        select(Entity).where(Entity.company_id == company.id, Entity.code == code)
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {code}")
    return entity


def agent_or_404(db: Session, company: Company, slug: str) -> Agent:
    """Look up an agent by slug, scoped to the current company."""
    agent = db.execute(
        select(Agent).where(Agent.company_id == company.id, Agent.slug == slug)
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {slug}")
    return agent
