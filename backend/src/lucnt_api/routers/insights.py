"""Insights router (Phase 5, M8) — the "your CFO, always on" recommendations.

Reads the `insights` table that `POST /intelligence/recompute` populates; it never
computes on the request path. `POST /insights/{id}/apply` marks an insight acted on
(applied insights survive the next recompute as a record of the action taken).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    get_current_company,
    get_db,
    require_admin,
    require_nav,
)
from lucnt_api.models import Agent, Company, Insight, InsightType
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.intelligence import InsightResponse, InsightsListResponse
from lucnt_api.services.lookups import entity_or_404

# Router-level nav gate (P7.0): a human principal may only reach a domain
# present in `perms.nav` — the same tuple the sidebar renders from, so a
# hidden page and a reachable endpoint cannot drift apart. No-op for API keys.
router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(require_nav("insights"))],
)


def _to_response(insight: Insight) -> InsightResponse:
    """Map an Insight ORM row to its API response (enum -> str)."""
    return InsightResponse(
        id=insight.id,
        agent_id=insight.agent_id,
        insight_type=insight.insight_type.value,
        title=insight.title,
        body=insight.body,
        value_text=insight.value_text,
        action_label=insight.action_label,
        action_payload=insight.action_payload,
        is_applied=insight.is_applied,
        applied_at=insight.applied_at,
        is_dismissed=insight.is_dismissed,
        dismissed_at=insight.dismissed_at,
        created_at=insight.created_at,
    )


@router.get("", response_model=InsightsListResponse)
def list_insights(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    insight_type: InsightType | None = Query(
        default=None, description="Filter by insight type"
    ),
    include_applied: bool = Query(
        default=False, description="Include already-applied insights"
    ),
    include_dismissed: bool = Query(
        default=False, description="Include dismissed insights"
    ),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate()),
) -> InsightsListResponse:
    """List the company's insights, newest first, with an unapplied headline count.

    Dismissed insights are hidden by default (they are resolved — T2-10); pass
    `include_dismissed=true` to see them.
    """
    stmt = select(Insight).where(Insight.company_id == company.id)
    if insight_type is not None:
        stmt = stmt.where(Insight.insight_type == insight_type)
    if not include_applied:
        stmt = stmt.where(Insight.is_applied.is_(False))
    if not include_dismissed:
        stmt = stmt.where(Insight.is_dismissed.is_(False))
    if entity is not None:
        # `insights.agent_id` is nullable: a company-wide insight belongs to no
        # agent and therefore to no entity. Those are kept in every entity's view
        # rather than vanishing when the switcher moves — they are true of the
        # company regardless of which entity you are looking at.
        entity_row = entity_or_404(db, company, entity)
        stmt = stmt.outerjoin(Agent, Agent.id == Insight.agent_id).where(
            or_(Insight.agent_id.is_(None), Agent.entity_id == entity_row.id)
        )

    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Insight.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    unapplied_count = db.execute(
        select(func.count())
        .select_from(Insight)
        .where(
            Insight.company_id == company.id,
            Insight.is_applied.is_(False),
            Insight.is_dismissed.is_(False),
        )
    ).scalar_one()
    return InsightsListResponse(
        items=[_to_response(row) for row in rows],
        total=total,
        unapplied_count=int(unapplied_count),
    )


@router.post("/{insight_id}/apply", response_model=InsightResponse)
def apply_insight(
    insight_id: uuid.UUID,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
) -> InsightResponse:
    """Mark an insight as acted on (idempotent — re-applying is a no-op)."""
    insight = db.execute(
        select(Insight).where(
            Insight.id == insight_id, Insight.company_id == company.id
        )
    ).scalar_one_or_none()
    if insight is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown insight")

    if not insight.is_applied:
        insight.is_applied = True
        insight.applied_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(insight)
    return _to_response(insight)


@router.post("/{insight_id}/dismiss", response_model=InsightResponse)
def dismiss_insight(
    insight_id: uuid.UUID,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
) -> InsightResponse:
    """Dismiss an insight so recompute stops regenerating it (idempotent — T2-10)."""
    insight = db.execute(
        select(Insight).where(
            Insight.id == insight_id, Insight.company_id == company.id
        )
    ).scalar_one_or_none()
    if insight is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown insight")

    if not insight.is_dismissed:
        insight.is_dismissed = True
        insight.dismissed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(insight)
    return _to_response(insight)
