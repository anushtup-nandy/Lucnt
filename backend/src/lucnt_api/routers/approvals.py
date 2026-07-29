"""Approval queue (M6 approver persona) — makes review/human_hold verdicts real.

PRODUCTION_PLAN T0-11: the policy engine can return `review`/`human_hold`, but
until now nothing could act on them — no endpoint listed them and `capture`
rejected anything not `approved`, so two of four verdict types were dead ends.
This router lists pending holds and lets an approver approve (budget re-checked)
or reject them; an approved hold becomes capturable.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_nav,
    require_write,
)
from lucnt_api.models import Agent, Authorization, Company, Entity, VerdictStatus
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.approval import (
    ApprovalActionResponse,
    ApprovalItem,
    ApprovalListResponse,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.spend_window import (
    committed_spend,
    now_in_entity_tz,
    period_starts,
    reserved_spend,
)

# Router-level nav gate (P7.0): a human principal may only reach a domain
# present in `perms.nav` — the same tuple the sidebar renders from, so a
# hidden page and a reachable endpoint cannot drift apart. No-op for API keys.
router = APIRouter(
    prefix="/approvals",
    tags=["approvals"],
    dependencies=[Depends(require_nav("approvals"))],
)

_PENDING = (VerdictStatus.REVIEW, VerdictStatus.HUMAN_HOLD)


def _resolve_pending(
    db: Session, company: Company, approval_id: uuid.UUID
) -> Authorization:
    """Fetch a company-scoped authorization that is still awaiting approval."""
    authorization = db.execute(
        select(Authorization).where(
            Authorization.id == approval_id,
            Authorization.company_id == company.id,
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown approval")
    if authorization.status not in _PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"authorization is {authorization.status.value}, not pending approval",
        )
    return authorization


@router.get("", response_model=ApprovalListResponse)
def list_approvals(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    page: Page = Depends(paginate()),
) -> ApprovalListResponse:
    """List holds awaiting approval (review + human_hold) for the current company.

    Paginated (T3-3): the queue grows with how long holds go unattended, which is
    exactly the situation in which an unbounded response is least welcome.
    """
    stmt = select(Authorization).where(
        Authorization.company_id == company.id,
        Authorization.status.in_(_PENDING),
    )
    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Authorization.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return ApprovalListResponse(
        items=[ApprovalItem.model_validate(row) for row in rows], total=total
    )


@router.post("/{approval_id}/approve", response_model=ApprovalActionResponse)
def approve(
    approval_id: uuid.UUID,
    company: Company = Depends(require_write("can_approve")),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> ApprovalActionResponse:
    """Approve a pending hold — after a budget re-check — so it can be captured."""
    authorization = _resolve_pending(db, company, approval_id)

    # Lock the agent and re-check the budget atomically (T0-1): approving several
    # pending holds must not collectively bust the budget the engine deferred on.
    agent = db.execute(
        select(Agent).where(Agent.id == authorization.agent_id).with_for_update()
    ).scalar_one()
    entity = db.get(Entity, authorization.entity_id)
    if entity is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization references a missing entity",
        )
    month_start, _ = period_starts(now_in_entity_tz(entity))
    # Value this hold in the entity's functional currency for the re-check (T1-2);
    # fall back to the paid amount only for a legacy hold with no FX snapshot.
    hold_functional = authorization.local_amount or authorization.amount
    projected = (
        committed_spend(db, agent.id, month_start)
        + reserved_spend(db, agent.id)
        + hold_functional
    )
    if projected > agent.monthly_budget:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"approving would exceed the agent's monthly budget "
            f"({projected} > {agent.monthly_budget})",
        )

    prior_status = authorization.status.value
    authorization.status = VerdictStatus.APPROVED
    authorization.verdict_code = "ok"
    record_audit(
        db,
        ctx,
        action="approval.approve",
        entity_type="authorization",
        entity_id=authorization.id,
        before={"status": prior_status},
        after={"status": VerdictStatus.APPROVED.value},
    )
    db.commit()
    return ApprovalActionResponse(
        id=authorization.id, status=VerdictStatus.APPROVED.value
    )


@router.post("/{approval_id}/reject", response_model=ApprovalActionResponse)
def reject(
    approval_id: uuid.UUID,
    company: Company = Depends(require_write("can_approve")),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> ApprovalActionResponse:
    """Reject a pending hold — it becomes blocked and can never be captured."""
    authorization = _resolve_pending(db, company, approval_id)
    prior_status = authorization.status.value
    authorization.status = VerdictStatus.BLOCKED
    authorization.verdict_code = "rejected"
    authorization.reason_text = "rejected by approver"
    record_audit(
        db,
        ctx,
        action="approval.reject",
        entity_type="authorization",
        entity_id=authorization.id,
        before={"status": prior_status},
        after={"status": VerdictStatus.BLOCKED.value},
    )
    db.commit()
    return ApprovalActionResponse(
        id=authorization.id, status=VerdictStatus.BLOCKED.value
    )
