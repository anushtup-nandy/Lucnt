"""Transactions router — the authorize→capture spine (backend/PLAN.md §4).

Phase 1 scope: the verdict is a single budget-cap check against
`agents.monthly_budget`/`spent_ytd`. The full policy engine (merchant allow/
blocklist, velocity, time-of-day, a `policies` table) is Phase 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import (
    Agent,
    Authorization,
    Company,
    Entity,
    Transaction,
    TransactionSource,
    TransactionStatus,
    VerdictStatus,
)
from lucnt.schemas.transaction import (
    CaptureRequest,
    SpendRequest,
    TransactionDetail,
    TransactionListResponse,
    Verdict,
)
from lucnt.schemas.transaction import Transaction as TransactionSchema

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _resolve_agent(db: Session, company: Company, slug: str) -> Agent:
    """Look up an agent by slug, scoped to the current company."""
    agent = db.execute(
        select(Agent).where(Agent.company_id == company.id, Agent.slug == slug)
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {slug}")
    return agent


def _resolve_entity(db: Session, company: Company, code: str) -> Entity:
    """Look up a legal entity by code, scoped to the current company."""
    entity = db.execute(
        select(Entity).where(Entity.company_id == company.id, Entity.code == code)
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {code}")
    return entity


def _evaluate_budget_cap(
    agent: Agent, amount: Decimal
) -> tuple[VerdictStatus, str | None]:
    """The Phase 1 verdict: block only if projected spend exceeds the monthly cap.

    Superseded by the full policy engine in Phase 3 (root PLAN.md §4).
    """
    projected = agent.spent_ytd + amount
    if projected > agent.monthly_budget:
        return VerdictStatus.BLOCKED, (
            f"projected spend {projected} exceeds monthly budget {agent.monthly_budget}"
        )
    return VerdictStatus.APPROVED, None


@router.post("/authorize", response_model=Verdict)
def authorize(
    request: SpendRequest,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> Verdict:
    """Evaluate policy on a spend before any money moves."""
    agent = _resolve_agent(db, company, request.agent)
    entity = _resolve_entity(db, company, request.entity)
    verdict_status, reason_text = _evaluate_budget_cap(agent, request.amount)

    authorization = Authorization(
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        vendor=request.vendor,
        amount=request.amount,
        currency=request.currency,
        reason=request.reason,
        intent=request.intent,
        project=request.project,
        model_used=request.model,
        tokens_consumed=request.tokens,
        status=verdict_status,
        reason_text=reason_text,
    )
    db.add(authorization)
    db.commit()
    db.refresh(authorization)

    return Verdict(
        authorization_id=authorization.id,
        status=verdict_status.value,
        reason=reason_text,
    )


@router.post("/capture", response_model=TransactionSchema)
def capture(
    request: CaptureRequest,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> TransactionSchema:
    """Commit an approved authorization to the ledger."""
    if request.source != "sdk":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"source={request.source!r} capture lands in Phase 6.5 (card/wallet rail)",
        )

    authorization = db.execute(
        select(Authorization).where(
            Authorization.id == request.authorization_id,
            Authorization.company_id == company.id,
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown authorization")
    if authorization.status != VerdictStatus.APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot capture a {authorization.status.value} authorization",
        )

    agent = db.get(Agent, authorization.agent_id)
    assert agent is not None, "authorizations.agent_id is FK-constrained to agents.id"
    currency = authorization.currency or "USD"

    transaction = Transaction(
        company_id=company.id,
        agent_id=authorization.agent_id,
        entity_id=authorization.entity_id,
        authorization_id=authorization.id,
        source=TransactionSource.SDK,
        vendor=authorization.vendor,
        item_description=authorization.reason,
        paid_amount=authorization.amount,
        paid_currency=currency,
        local_amount=authorization.amount,
        local_currency=currency,
        project=authorization.project,
        intent=authorization.intent,
        model_used=authorization.model_used,
        tokens_consumed=authorization.tokens_consumed,
        status=TransactionStatus.COMPLETED,
        approval_method="auto",
        reason_text=authorization.reason_text,
        transaction_date=datetime.now(timezone.utc),
    )
    db.add(transaction)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Authorization already captured"
        ) from exc

    agent.spent_ytd += transaction.paid_amount
    agent.spent_today += transaction.paid_amount
    db.commit()
    db.refresh(transaction)

    return TransactionSchema(
        id=transaction.id,
        authorization_id=transaction.authorization_id,
        status=transaction.status.value,
        vendor=transaction.vendor,
        amount=transaction.paid_amount,
        currency=transaction.paid_currency,
    )


@router.get("", response_model=TransactionListResponse)
def list_transactions(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    agent: str | None = Query(default=None, description="Filter by agent slug"),
    vendor: str | None = Query(default=None, description="Filter by vendor"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TransactionListResponse:
    """List committed transactions for the current company, filterable/paginated."""
    stmt = select(Transaction).where(Transaction.company_id == company.id)
    if agent is not None:
        agent_row = _resolve_agent(db, company, agent)
        stmt = stmt.where(Transaction.agent_id == agent_row.id)
    if vendor is not None:
        stmt = stmt.where(Transaction.vendor == vendor)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(stmt.order_by(Transaction.created_at.desc()).offset(offset).limit(limit))
        .scalars()
        .all()
    )
    return TransactionListResponse(
        items=[TransactionDetail.model_validate(row) for row in rows], total=total
    )


@router.get("/{transaction_id}", response_model=TransactionDetail)
def get_transaction(
    transaction_id: uuid.UUID,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> TransactionDetail:
    """Full detail for a single transaction."""
    transaction = db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.company_id == company.id
        )
    ).scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction")
    return TransactionDetail.model_validate(transaction)
