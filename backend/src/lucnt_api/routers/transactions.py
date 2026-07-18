"""Transactions router — the authorize→capture spine (backend/PLAN.md §4).

`authorize()` runs the full policy engine (`services/policy_engine.py`, Phase 3):
an always-on monthly-budget invariant plus every active company/agent-scoped
policy (merchant lists, velocity, amount thresholds, time-of-day), reduced
most-restrictive-wins to a verdict + machine-readable `code`. `capture()` commits
the spend and books tax + double-entry journal (Phase 2).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.config import settings
from lucnt_api.dependencies import get_current_company, get_db, require_live
from lucnt_api.models import (
    Agent,
    AgentStatus,
    Authorization,
    Company,
    Entity,
    JournalEntry,
    Policy,
    PolicyScopeType,
    TaxRecord,
    Transaction,
    TransactionSource,
    TransactionStatus,
    VerdictStatus,
)
from lucnt_api.schemas.transaction import (
    CaptureRequest,
    JournalLine,
    SpendRequest,
    TaxInfo,
    TransactionDetail,
    TransactionListResponse,
    Verdict,
)
from lucnt_api.schemas.transaction import Transaction as TransactionSchema
from lucnt_api.services.chart_of_accounts import ensure_default_accounts
from lucnt_api.services.journal_generator import generate_journal
from lucnt_api.services.policy_engine import SpendContext, evaluate_spend
from lucnt_api.services.spend_window import (
    committed_spend,
    now_in_entity_tz,
    period_starts,
    recent_activity_count,
    reserved_spend,
)
from lucnt_api.services.tax_engine import TaxTreatment, compute_tax

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _resolve_agent(
    db: Session, company: Company, slug: str, *, for_update: bool = False
) -> Agent:
    """Look up an agent by slug, scoped to the current company.

    `for_update` takes a row lock so the authorize() check-and-reserve is atomic
    against concurrent authorizes for the same agent (T0-1).
    """
    stmt = select(Agent).where(Agent.company_id == company.id, Agent.slug == slug)
    if for_update:
        stmt = stmt.with_for_update()
    agent = db.execute(stmt).scalar_one_or_none()
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


def _verdict_from(authorization: Authorization) -> Verdict:
    """Reconstruct the wire verdict from a persisted hold (idempotent replay, T0-6)."""
    return Verdict(
        authorization_id=authorization.id,
        status=authorization.status.value,
        code=authorization.verdict_code or "ok",
        reason=authorization.reason_text,
    )


def _capture_response(
    transaction: Transaction, tax: TaxTreatment, journal_lines: list[JournalEntry]
) -> TransactionSchema:
    """Assemble the capture wire response from a transaction + its tax/journal.

    Tax and journal are pure functions of the transaction + entity, so an
    idempotent capture replay rebuilds the identical response by recomputing
    them — no need to persist or re-read the derived rows (T0-6).
    """
    return TransactionSchema(
        id=transaction.id,
        authorization_id=transaction.authorization_id,
        status=transaction.status.value,
        vendor=transaction.vendor,
        amount=transaction.paid_amount,
        currency=transaction.paid_currency,
        journal=[
            JournalLine(
                account_name=line.account_name,
                account_code=line.account_code,
                debit=line.debit,
                credit=line.credit,
                ledger=line.ledger,
            )
            for line in journal_lines
        ],
        tax=TaxInfo(
            tax_type=tax.tax_type,
            jurisdiction=tax.jurisdiction,
            rate=tax.rate,
            tax_amount=tax.tax_amount,
            is_reclaimable=tax.is_reclaimable,
            reclaim_amount=tax.reclaim_amount,
            legal_reference=tax.legal_reference,
            reverse_charge=tax.reverse_charge,
            self_invoice_generated=tax.self_invoice_required,
        ),
    )


def _replay_capture(
    db: Session, company: Company, authorization_id: uuid.UUID
) -> TransactionSchema:
    """Return the existing transaction for an already-captured authorization (T0-6).

    A retried capture (its predecessor committed but the response was lost) hits
    the `authorization_id` unique constraint. Rather than 409, return the first
    capture's transaction with journal + tax rebuilt deterministically — a safe,
    idempotent no-op that never double-books.
    """
    existing = db.execute(
        select(Transaction).where(
            Transaction.authorization_id == authorization_id,
            Transaction.company_id == company.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        # The conflict was not a duplicate capture (unexpected) — surface it.
        raise HTTPException(status.HTTP_409_CONFLICT, "Capture conflict")
    entity = db.get(Entity, existing.entity_id)
    if entity is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "transaction references a missing entity",
        )
    tax = compute_tax(
        tax_regime=entity.tax_regime,
        paid_amount=existing.paid_amount,
        paid_currency=existing.paid_currency,
        entity_currency=entity.currency,
    )
    accounts = ensure_default_accounts(db, company.id)
    journal_lines = generate_journal(existing, entity, tax, accounts)
    return _capture_response(existing, tax, journal_lines)


def _controls_block(agent: Agent, entity: Entity) -> tuple[str, str] | None:
    """`(code, reason)` if agent/entity state forbids any spend, else None.

    A paused/disabled or non-live agent (T0-9) or an inactive entity (T0-10) is
    blocked before the policy engine even runs — "pause this agent" must actually
    stop spend. `is_live` fails closed here; the live/test key partition (T0-13)
    refines how test agents authorize under test keys.
    """
    if agent.status != AgentStatus.ACTIVE:
        return "agent_inactive", f"agent is {agent.status.value}, not active"
    if not agent.is_live:
        return "agent_inactive", "agent is not live"
    if not entity.is_active:
        return "entity_inactive", f"entity {entity.code!r} is inactive"
    return None


def _persist_authorization(
    db: Session,
    company: Company,
    agent: Agent,
    entity: Entity,
    request: SpendRequest,
    *,
    verdict_status: VerdictStatus,
    code: str,
    reason: str | None,
    policy_id: uuid.UUID | None,
    idempotency_key: str | None,
) -> Verdict:
    """Persist the hold and return its verdict, idempotency-race-safe (T0-6)."""
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
        policy_id=policy_id,
        reason_text=reason,
        verdict_code=code,
        idempotency_key=idempotency_key,
    )
    db.add(authorization)
    try:
        db.commit()
    except IntegrityError:
        # Lost the idempotency-key race to a concurrent authorize: return the
        # hold that won the unique index (T0-6). Any other IntegrityError here is
        # unexpected — re-raise it.
        db.rollback()
        if idempotency_key is None:
            raise
        winner = db.execute(
            select(Authorization).where(
                Authorization.company_id == company.id,
                Authorization.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        return _verdict_from(winner)
    db.refresh(authorization)
    return _verdict_from(authorization)


@router.post("/authorize", response_model=Verdict)
def authorize(
    request: SpendRequest,
    company: Company = Depends(require_live),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Verdict:
    """Evaluate the policy engine on a spend before any money moves (M5)."""
    # Idempotent replay (T0-6): a retried authorize with the same client key
    # returns the original hold's verdict, so a timed-out spend() that actually
    # committed does not mint a second hold (which, captured, would double-book).
    if idempotency_key is not None:
        prior = db.execute(
            select(Authorization).where(
                Authorization.company_id == company.id,
                Authorization.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if prior is not None:
            return _verdict_from(prior)

    # Lock the agent row so the check-and-reserve below is atomic: concurrent
    # authorizes for the same agent serialize here, and each one sees the prior's
    # hold in the reservation sum before it decides (T0-1). The lock is released
    # when this request's transaction commits.
    agent = _resolve_agent(db, company, request.agent, for_update=True)
    entity = _resolve_entity(db, company, request.entity)

    # T0-9/T0-10: a paused/disabled/non-live agent or an inactive entity cannot
    # spend — short-circuit to a blocked hold before running the policy engine.
    controls = _controls_block(agent, entity)
    if controls is not None:
        code, reason = controls
        return _persist_authorization(
            db,
            company,
            agent,
            entity,
            request,
            verdict_status=VerdictStatus.BLOCKED,
            code=code,
            reason=reason,
            policy_id=None,
            idempotency_key=idempotency_key,
        )

    policies = _fetch_active_policies(db, company, agent)
    now = now_in_entity_tz(entity)
    month_start, day_start = period_starts(now)

    # Spend measured against the budget = committed (ledger, period-resetting) +
    # reserved (outstanding approved holds). The stored spent_ytd/spent_today
    # counters are a display cache only; the invariant derives from truth here
    # so it self-resets (T0-5) and holds count against headroom (T0-1).
    reserved = reserved_spend(db, agent.id)
    spent_month = committed_spend(db, agent.id, month_start) + reserved
    spent_today = committed_spend(db, agent.id, day_start) + reserved

    def recent_count(window: timedelta) -> int:
        return recent_activity_count(db, agent.id, now - window)

    ctx = SpendContext(
        amount=request.amount,
        currency=request.currency,
        vendor=request.vendor,
        now=now,
        agent_monthly_budget=agent.monthly_budget,
        agent_spent=spent_month,
        agent_spent_today=spent_today,
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

    return _persist_authorization(
        db,
        company,
        agent,
        entity,
        request,
        verdict_status=decision.status,
        code=decision.code.value,
        reason=decision.reason,
        policy_id=policy_id,
        idempotency_key=idempotency_key,
    )


@router.post("/capture", response_model=TransactionSchema)
def capture(
    request: CaptureRequest,
    company: Company = Depends(require_live),
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
    if agent is None:  # FK-guaranteed; explicit guard, not an assert (T0-18)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization references a missing agent",
        )
    currency = authorization.currency or "USD"

    # Settlement overrides (Phase 3.5): the actual amount/model/tokens known only
    # after the spend (e.g. real LLM cost from meter()); the authorization's held
    # values stand when not provided.
    #
    # T0-3: a settlement may *correct* the estimate but not blow past it — the
    # verdict was rendered on the authorized amount and only that amount was
    # reserved against the budget at authorize(). Cap the override at
    # auth x settlement_buffer; a larger true cost must be re-authorized.
    paid_amount = (
        request.amount if request.amount is not None else authorization.amount
    )
    if request.amount is not None:
        max_settlement = authorization.amount * settings.settlement_buffer
        if request.amount > max_settlement:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"settlement {request.amount} exceeds authorized "
                f"{authorization.amount} by more than the allowed "
                f"{settings.settlement_buffer}x buffer; re-authorize the spend",
            )
    model_used = (
        request.model if request.model is not None else authorization.model_used
    )
    tokens_consumed = (
        request.tokens if request.tokens is not None else authorization.tokens_consumed
    )

    transaction = Transaction(
        company_id=company.id,
        agent_id=authorization.agent_id,
        entity_id=authorization.entity_id,
        authorization_id=authorization.id,
        source=TransactionSource.SDK,
        vendor=authorization.vendor,
        item_description=authorization.reason,
        paid_amount=paid_amount,
        paid_currency=currency,
        local_amount=paid_amount,
        local_currency=currency,
        project=authorization.project,
        intent=authorization.intent,
        model_used=model_used,
        tokens_consumed=tokens_consumed,
        status=TransactionStatus.COMPLETED,
        approval_method="auto",
        reason_text=authorization.reason_text,
        transaction_date=datetime.now(timezone.utc),
    )
    db.add(transaction)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _replay_capture(db, company, request.authorization_id)

    # T0-2: increment spend counters atomically at the DB (UPDATE ... SET
    # x = x + :amt) rather than a Python read-modify-write, so concurrent
    # captures can't lost-update each other and under-count spend. These
    # counters are a display cache; the authorize() budget invariant derives
    # committed spend from the ledger directly (T0-1/T0-5).
    db.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(
            spent_ytd=Agent.spent_ytd + transaction.paid_amount,
            spent_today=Agent.spent_today + transaction.paid_amount,
        )
        .execution_options(synchronize_session=False)
    )

    # Phase 2 (Books): compute tax + double-entry journal synchronously, in the
    # same transaction as the capture, keyed off the persisted `transactions`
    # row (lane-agnostic — CLAUDE.local.md). The card/invoice rails book
    # through this exact path once they land.
    entity = db.get(Entity, authorization.entity_id)
    if entity is None:  # FK-guaranteed; explicit guard, not an assert (T0-18)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization references a missing entity",
        )
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

    # Build the response before commit (expire_on_commit would otherwise force a
    # reload per attribute); the same builder serves the idempotent replay path.
    result = _capture_response(transaction, tax, journal_lines)
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
