"""Reconciliation router: the recovery inbox.

Reads the ledger and writes `reconciliation_exceptions`; never mutates `transactions`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_admin,
    require_cheap_enough,
    require_nav,
)
from lucnt_api.models import (
    Company,
    ReconciliationException,
    ReconExceptionStatus,
    ReconExceptionType,
    Transaction,
)
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.recon import (
    FailedDeliveryRequest,
    ReconExceptionResponse,
    ReconListResponse,
    ReconResolveRequest,
    ScanResponse,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.detectors import TxnView, recurring_identity, run_detectors
from lucnt_api.services.lookups import entity_or_404
from lucnt_api.services.recovery_agent import plan_recovery

router = APIRouter(
    prefix="/recon",
    tags=["reconciliation"],
    dependencies=[Depends(require_nav("recon"))],
)

# Non-terminal statuses — an exception in one of these is still "in the inbox".
_ACTIVE_STATUSES = (ReconExceptionStatus.OPEN, ReconExceptionStatus.RECOVERING)
_TERMINAL_STATUSES = (ReconExceptionStatus.RESOLVED, ReconExceptionStatus.DISMISSED)

# Rows per round trip when streaming the ledger in `scan`.
_LEDGER_CHUNK_SIZE = 1_000


def _to_response(exc: ReconciliationException) -> ReconExceptionResponse:
    """Map a ReconciliationException ORM row to its API response (enums -> str)."""
    return ReconExceptionResponse(
        id=exc.id,
        transaction_id=exc.transaction_id,
        exception_type=exc.exception_type.value,
        status=exc.status.value,
        evidence=exc.evidence_json,
        amount_at_stake=exc.amount_at_stake,
        currency=exc.currency,
        fix_notes=exc.fix_notes,
        resolved_at=exc.resolved_at,
        created_at=exc.created_at,
        updated_at=exc.updated_at,
    )


def _active_summary(db: Session, company: Company) -> tuple[dict[str, Decimal], int]:
    """(per-currency $ at stake, open count) across active exceptions.

    Never summed across currencies; a NULL currency buckets under "".
    """
    rows = db.execute(
        select(
            ReconciliationException.currency,
            func.coalesce(func.sum(ReconciliationException.amount_at_stake), 0),
        )
        .where(
            ReconciliationException.company_id == company.id,
            ReconciliationException.status.in_(_ACTIVE_STATUSES),
        )
        .group_by(ReconciliationException.currency)
    ).all()
    at_stake = {(currency or ""): Decimal(total) for currency, total in rows}
    open_count = db.execute(
        select(func.count())
        .select_from(ReconciliationException)
        .where(
            ReconciliationException.company_id == company.id,
            ReconciliationException.status == ReconExceptionStatus.OPEN,
        )
    ).scalar_one()
    return at_stake, int(open_count)


@router.get("", response_model=ReconListResponse)
def list_exceptions(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    status_filter: ReconExceptionStatus | None = Query(
        default=None, alias="status", description="Filter by exception status"
    ),
    exception_type: ReconExceptionType | None = Query(
        default=None, description="Filter by exception type"
    ),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    page: Page = Depends(paginate()),
) -> ReconListResponse:
    """List reconciliation exceptions, grouped by currency, highest stake first."""
    stmt = select(ReconciliationException).where(
        ReconciliationException.company_id == company.id
    )
    if status_filter is not None:
        stmt = stmt.where(ReconciliationException.status == status_filter)
    if exception_type is not None:
        stmt = stmt.where(ReconciliationException.exception_type == exception_type)
    if entity is not None:
        # Entity comes from the underlying transaction.
        entity_row = entity_or_404(db, company, entity)
        stmt = stmt.join(
            Transaction, Transaction.id == ReconciliationException.transaction_id
        ).where(Transaction.entity_id == entity_row.id)

    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(
                ReconciliationException.currency,
                ReconciliationException.amount_at_stake.desc(),
                ReconciliationException.created_at.desc(),
            )
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    at_stake, open_count = _active_summary(db, company)
    return ReconListResponse(
        items=[_to_response(row) for row in rows],
        total=total,
        total_at_stake=at_stake,
        open_count=open_count,
    )


@router.post(
    "/scan",
    response_model=ScanResponse,
    # Cost scales with ledger size, so rate-limit beyond the admin gate.
    dependencies=[Depends(require_cheap_enough)],
)
def scan(
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ScanResponse:
    """Run the detector suite over the company ledger; book new exceptions.

    Idempotent per (txn, type) and per recurring identity (`dedup_key`), so a
    dismissed recurring charge is not re-raised next month.
    """
    # Stream only the columns `TxnView` needs; ORM entities would hold the
    # whole ledger in the identity map.
    views: list[TxnView] = []
    view_by_id: dict[uuid.UUID, TxnView] = {}
    ledger = db.execute(
        select(
            Transaction.id,
            Transaction.agent_id,
            Transaction.vendor,
            Transaction.paid_amount,
            Transaction.paid_currency,
            Transaction.transaction_date,
        )
        .where(Transaction.company_id == company.id)
        .execution_options(yield_per=_LEDGER_CHUNK_SIZE)
    ).tuples()
    for (
        txn_id,
        agent_id,
        vendor,
        paid_amount,
        paid_currency,
        transaction_date,
    ) in ledger:
        view = TxnView(
            id=txn_id,
            agent_id=agent_id,
            vendor=vendor,
            paid_amount=paid_amount,
            paid_currency=paid_currency,
            transaction_date=transaction_date,
        )
        views.append(view)
        view_by_id[txn_id] = view

    # Existing (txn, type) pairs and recurring-identity dedup_keys (any status).
    existing_pairs: set[tuple[uuid.UUID, ReconExceptionType]] = set()
    existing_dedup: set[str] = set()
    for txn_id, etype, dedup in db.execute(
        select(
            ReconciliationException.transaction_id,
            ReconciliationException.exception_type,
            ReconciliationException.dedup_key,
        ).where(ReconciliationException.company_id == company.id)
    ).all():
        existing_pairs.add((txn_id, etype))
        if dedup is not None:
            existing_dedup.add(dedup)

    created = 0
    by_type: dict[str, int] = {}
    for hit in run_detectors(views):
        pair = (hit.transaction_id, hit.exception_type)
        view = view_by_id[hit.transaction_id]
        dedup_key = f"{hit.exception_type.value}:{recurring_identity(view)}"
        if pair in existing_pairs or dedup_key in existing_dedup:
            continue
        plan = plan_recovery(
            hit.exception_type,
            vendor=view.vendor,
            amount_at_stake=hit.amount_at_stake,
            currency=view.paid_currency,
        )
        exc = ReconciliationException(
            company_id=company.id,
            transaction_id=hit.transaction_id,
            exception_type=hit.exception_type,
            evidence_json={**hit.evidence, "recovery_plan": plan.as_dict()},
            amount_at_stake=hit.amount_at_stake,
            currency=view.paid_currency,
            dedup_key=dedup_key,
        )
        try:
            # SAVEPOINT: a concurrent scan may have booked the same (txn, type)
            # between our SELECT and INSERT; roll back just this row, not the pass.
            with db.begin_nested():
                db.add(exc)
                db.flush()
        except IntegrityError:
            continue
        existing_pairs.add(pair)
        existing_dedup.add(dedup_key)
        created += 1
        by_type[hit.exception_type.value] = by_type.get(hit.exception_type.value, 0) + 1

    db.commit()
    return ScanResponse(scanned=len(views), created=created, by_type=by_type)


@router.post(
    "/failed-delivery",
    response_model=ReconExceptionResponse,
    status_code=status.HTTP_201_CREATED,
)
def report_failed_delivery(
    payload: FailedDeliveryRequest,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ReconExceptionResponse:
    """Record an agent-reported failed delivery; idempotent per transaction."""
    txn = db.execute(
        select(Transaction).where(
            Transaction.id == payload.transaction_id,
            Transaction.company_id == company.id,
        )
    ).scalar_one_or_none()
    if txn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction")

    existing = _find_failed_delivery(db, txn.id)
    if existing is not None:
        return _to_response(existing)

    # Cannot claim back more than was paid.
    if (
        payload.amount_at_stake is not None
        and payload.amount_at_stake > txn.paid_amount
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "amount_at_stake exceeds the transaction's paid amount",
        )
    stake = (
        payload.amount_at_stake
        if payload.amount_at_stake is not None
        else txn.paid_amount
    )
    plan = plan_recovery(
        ReconExceptionType.FAILED_DELIVERY,
        vendor=txn.vendor,
        amount_at_stake=stake,
        currency=txn.paid_currency,
    )
    exc = ReconciliationException(
        company_id=company.id,
        transaction_id=txn.id,
        exception_type=ReconExceptionType.FAILED_DELIVERY,
        evidence_json={
            "reason": payload.reason,
            "reported_via": "sdk",
            "recovery_plan": plan.as_dict(),
        },
        amount_at_stake=stake,
        currency=txn.paid_currency,
    )
    db.add(exc)
    try:
        db.commit()
    except IntegrityError:  # a concurrent report won the unique-constraint race
        db.rollback()
        existing = _find_failed_delivery(db, txn.id)
        if existing is not None:
            return _to_response(existing)
        raise
    db.refresh(exc)
    return _to_response(exc)


def _find_failed_delivery(
    db: Session, transaction_id: uuid.UUID
) -> ReconciliationException | None:
    """The failed-delivery exception for a transaction (any status), if one exists."""
    return db.execute(
        select(ReconciliationException).where(
            ReconciliationException.transaction_id == transaction_id,
            ReconciliationException.exception_type
            == ReconExceptionType.FAILED_DELIVERY,
        )
    ).scalar_one_or_none()


@router.get("/{exception_id}", response_model=ReconExceptionResponse)
def get_exception(
    exception_id: uuid.UUID,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> ReconExceptionResponse:
    """Full detail for a single reconciliation exception."""
    exc = db.execute(
        select(ReconciliationException).where(
            ReconciliationException.id == exception_id,
            ReconciliationException.company_id == company.id,
        )
    ).scalar_one_or_none()
    if exc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown exception")
    return _to_response(exc)


@router.post("/{exception_id}/resolve", response_model=ReconExceptionResponse)
def resolve_exception(
    exception_id: uuid.UUID,
    payload: ReconResolveRequest,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> ReconExceptionResponse:
    """Move an exception to a terminal state (resolved or dismissed)."""
    exc = db.execute(
        select(ReconciliationException).where(
            ReconciliationException.id == exception_id,
            ReconciliationException.company_id == company.id,
        )
    ).scalar_one_or_none()
    if exc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown exception")
    if exc.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Exception is already {exc.status.value}",
        )

    prior_status = exc.status.value
    exc.status = (
        ReconExceptionStatus.RESOLVED
        if payload.action == "resolve"
        else ReconExceptionStatus.DISMISSED
    )
    exc.fix_notes = payload.fix_notes
    exc.resolved_at = datetime.now(UTC)
    record_audit(
        db,
        ctx,
        action=f"recon.{payload.action}",
        entity_type="reconciliation_exception",
        entity_id=exc.id,
        before={"status": prior_status},
        after={"status": exc.status.value},
    )
    db.commit()
    db.refresh(exc)
    return _to_response(exc)
