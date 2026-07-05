"""Transactions router — the authorize→capture spine (backend/PLAN.md §4).

`authorize()` runs the full policy engine (`services/policy_engine.py`, Phase 3):
an always-on monthly-budget invariant plus every active company/agent-scoped
policy (merchant lists, velocity, amount thresholds, time-of-day), reduced
most-restrictive-wins to a verdict + machine-readable `code`. `capture()` commits
the spend and books tax + double-entry journal (Phase 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import (
    Agent,
    Authorization,
    Company,
    Entity,
    Policy,
    PolicyScopeType,
    TaxRecord,
    Transaction,
    TransactionSource,
    TransactionStatus,
    VerdictStatus,
)
from lucnt.schemas.transaction import (
    CaptureRequest,
    JournalLine,
    SpendRequest,
    TaxInfo,
    TransactionDetail,
    TransactionListResponse,
    Verdict,
)
from lucnt.schemas.transaction import Transaction as TransactionSchema
from lucnt.services.chart_of_accounts import ensure_default_accounts
from lucnt.services.journal_generator import generate_journal
from lucnt.services.policy_engine import SpendContext, evaluate_spend
from lucnt.services.tax_engine import compute_tax

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


def _now_in_entity_tz(entity: Entity) -> datetime:
    """Current time in the entity's local timezone (UTC if the tz string is bad)."""
    tz: tzinfo
    try:
        tz = ZoneInfo(entity.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return datetime.now(tz)


def _fetch_active_policies(db: Session, company: Company, agent: Agent) -> list[Policy]:
    """Active policies applicable to this agent: company-wide + agent-scoped (M5)."""
    stmt = select(Policy).where(
        Policy.company_id == company.id,
        Policy.active.is_(True),
        or_(
            Policy.scope_type == PolicyScopeType.COMPANY,
            and_(
                Policy.scope_type == PolicyScopeType.AGENT,
                Policy.scope_id == agent.id,
            ),
        ),
    )
    return list(db.execute(stmt).scalars().all())


@router.post("/authorize", response_model=Verdict)
def authorize(
    request: SpendRequest,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> Verdict:
    """Evaluate the policy engine on a spend before any money moves (M5)."""
    agent = _resolve_agent(db, company, request.agent)
    entity = _resolve_entity(db, company, request.entity)

    policies = _fetch_active_policies(db, company, agent)
    now = _now_in_entity_tz(entity)

    def recent_count(window: timedelta) -> int:
        """Committed spends by this agent within `window` ending now (for velocity)."""
        return db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.agent_id == agent.id,
                Transaction.transaction_date >= now - window,
            )
        ).scalar_one()

    ctx = SpendContext(
        amount=request.amount,
        currency=request.currency,
        vendor=request.vendor,
        now=now,
        agent_monthly_budget=agent.monthly_budget,
        agent_spent=agent.spent_ytd,
        agent_spent_today=agent.spent_today,
        recent_count=recent_count,
    )
    decision = evaluate_spend(policies, ctx)

    # Bump per-policy telemetry: hit for every rule that fired, block for the
    # ones that returned a blocking verdict (drives the "blocked N times" UI).
    for policy in policies:
        if policy.id in decision.fired_policy_ids:
            policy.hit_count += 1
        if policy.id in decision.blocking_policy_ids:
            policy.block_count += 1

    policy_id = next(
        iter(decision.blocking_policy_ids or decision.fired_policy_ids), None
    )

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
        status=decision.status,
        policy_id=policy_id,
        reason_text=decision.reason,
    )
    db.add(authorization)
    db.commit()
    db.refresh(authorization)

    return Verdict(
        authorization_id=authorization.id,
        status=decision.status.value,
        code=decision.code.value,
        reason=decision.reason,
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

    # Phase 2 (Books): compute tax + double-entry journal synchronously, in the
    # same transaction as the capture, keyed off the persisted `transactions`
    # row (lane-agnostic — CLAUDE.local.md). The card/invoice rails book
    # through this exact path once they land.
    entity = db.get(Entity, authorization.entity_id)
    assert entity is not None, "authorizations.entity_id is FK-constrained"
    tax = compute_tax(
        tax_regime=entity.tax_regime,
        paid_amount=transaction.paid_amount,
        paid_currency=transaction.paid_currency,
        entity_currency=entity.currency,
    )
    db.add(
        TaxRecord(
            transaction_id=transaction.id,
            tax_type=tax.tax_type,
            jurisdiction=tax.jurisdiction,
            rate=tax.rate,
            tax_amount=tax.tax_amount,
            is_reclaimable=tax.is_reclaimable,
            reclaim_amount=tax.reclaim_amount,
            legal_reference=tax.legal_reference,
            self_invoice_generated=tax.self_invoice_required,
        )
    )
    accounts = ensure_default_accounts(db, company.id)
    journal_lines = generate_journal(transaction, entity, tax, accounts)
    db.add_all(journal_lines)

    # Build the response payload before commit; expire_on_commit would otherwise
    # force a reload per attribute below.
    journal_payload = [
        JournalLine(
            account_name=line.account_name,
            account_code=line.account_code,
            debit=line.debit,
            credit=line.credit,
            ledger=line.ledger,
        )
        for line in journal_lines
    ]
    tax_payload = TaxInfo(
        tax_type=tax.tax_type,
        jurisdiction=tax.jurisdiction,
        rate=tax.rate,
        tax_amount=tax.tax_amount,
        is_reclaimable=tax.is_reclaimable,
        reclaim_amount=tax.reclaim_amount,
        legal_reference=tax.legal_reference,
        reverse_charge=tax.reverse_charge,
        self_invoice_generated=tax.self_invoice_required,
    )
    result = TransactionSchema(
        id=transaction.id,
        authorization_id=transaction.authorization_id,
        status=transaction.status.value,
        vendor=transaction.vendor,
        amount=transaction.paid_amount,
        currency=transaction.paid_currency,
        journal=journal_payload,
        tax=tax_payload,
    )
    db.commit()
    return result


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
