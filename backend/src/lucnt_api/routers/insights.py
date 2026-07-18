"""Insights router (Phase 5, M8) — the "your CFO, always on" recommendations.

Reads the `insights` table that `POST /intelligence/recompute` populates; it never
computes on the request path. `POST /insights/{id}/apply` marks an insight acted on
(applied insights survive the next recompute as a record of the action taken).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import get_current_company, get_db, require_admin
from lucnt_api.models import Company, Insight, InsightType
from lucnt_api.schemas.intelligence import InsightResponse, InsightsListResponse

router = APIRouter(prefix="/insights", tags=["insights"])


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
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> InsightsListResponse:
    """List the company's insights, newest first, with an unapplied headline count."""
    stmt = select(Insight).where(Insight.company_id == company.id)
    if insight_type is not None:
        stmt = stmt.where(Insight.insight_type == insight_type)
    if not include_applied:
        stmt = stmt.where(Insight.is_applied.is_(False))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Insight.created_at.desc()).offset(offset).limit(limit)
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
