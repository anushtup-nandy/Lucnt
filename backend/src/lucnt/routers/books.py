"""Books router — read-only financial statements (backend/PLAN.md §4, Module 2/4).

Every figure derives from `journal_entries` joined to `transactions` (for
company/entity scope) and `accounts` (for category). Nothing here writes; the
journal is produced at capture time by `services/journal_generator.py`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import (
    Account,
    AccountCategory,
    Company,
    Entity,
    JournalEntry,
    Transaction,
)
from lucnt.schemas.books import (
    BalanceSheetResponse,
    BookLine,
    CashFlowResponse,
    GeneralLedgerResponse,
    GLAccount,
    JournalGroup,
    JournalLineRead,
    JournalsResponse,
    ProfitAndLossResponse,
    TrialBalanceResponse,
    TrialBalanceRow,
)
from lucnt.services.chart_of_accounts import WALLET_CODE

router = APIRouter(prefix="/books", tags=["books"])

_ZERO = Decimal("0")
_RETAINED_EARNINGS_CODE = "3900"
_RETAINED_EARNINGS_NAME = "Retained Earnings"


@dataclass(frozen=True)
class _Aggregate:
    """Per-account debit/credit totals across all posted journal lines."""

    account_code: str
    account_name: str
    category: str
    debit: Decimal
    credit: Decimal


def _resolve_entity_id(
    db: Session, company: Company, code: str | None
) -> uuid.UUID | None:
    """Resolve an optional entity code to its id, company-scoped (404 if unknown)."""
    if code is None:
        return None
    entity = db.execute(
        select(Entity).where(Entity.company_id == company.id, Entity.code == code)
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {code}")
    return entity.id


def _aggregates(
    db: Session, company: Company, entity_id: uuid.UUID | None
) -> list[_Aggregate]:
    """Sum debit/credit per account over the company's (optionally entity's) journal."""
    stmt = (
        select(
            JournalEntry.account_code,
            JournalEntry.account_name,
            Account.category,
            func.coalesce(func.sum(JournalEntry.debit), _ZERO).label("total_debit"),
            func.coalesce(func.sum(JournalEntry.credit), _ZERO).label("total_credit"),
        )
        .join(Transaction, Transaction.id == JournalEntry.transaction_id)
        .join(
            Account,
            (Account.company_id == Transaction.company_id)
            & (Account.account_code == JournalEntry.account_code),
        )
        .where(Transaction.company_id == company.id)
        .group_by(
            JournalEntry.account_code,
            JournalEntry.account_name,
            Account.category,
        )
        .order_by(JournalEntry.account_code)
    )
    if entity_id is not None:
        stmt = stmt.where(Transaction.entity_id == entity_id)

    return [
        _Aggregate(
            account_code=row.account_code,
            account_name=row.account_name,
            category=_category_str(row.category),
            debit=row.total_debit,
            credit=row.total_credit,
        )
        for row in db.execute(stmt)
    ]


def _category_str(category: AccountCategory | str) -> str:
    """Normalise an account category to its string value."""
    return category.value if isinstance(category, AccountCategory) else str(category)


def _gl_account(code: str, category: str, lines: list[JournalEntry]) -> GLAccount:
    """Assemble one general-ledger account section from its postings."""
    total_debit = sum((ln.debit for ln in lines), _ZERO)
    total_credit = sum((ln.credit for ln in lines), _ZERO)
    return GLAccount(
        account_code=code,
        account_name=lines[0].account_name,
        category=category,
        total_debit=total_debit,
        total_credit=total_credit,
        balance=total_debit - total_credit,
        entries=[
            JournalLineRead(
                account_code=ln.account_code,
                account_name=ln.account_name,
                debit=ln.debit,
                credit=ln.credit,
                ledger=ln.ledger,
            )
            for ln in lines
        ],
    )


def _net_profit(aggregates: list[_Aggregate]) -> Decimal:
    """Income (credit-normal) minus expenses (debit-normal) across the ledger."""
    income = sum(
        (a.credit - a.debit for a in aggregates if a.category == "income"), _ZERO
    )
    expenses = sum(
        (a.debit - a.credit for a in aggregates if a.category == "expense"), _ZERO
    )
    return income - expenses


@router.get("/journals", response_model=JournalsResponse)
def get_journals(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JournalsResponse:
    """Journal explorer: postings grouped by transaction, newest first."""
    entity_id = _resolve_entity_id(db, company, entity)

    posted = select(JournalEntry.transaction_id)
    base = select(Transaction).where(
        Transaction.company_id == company.id, Transaction.id.in_(posted)
    )
    if entity_id is not None:
        base = base.where(Transaction.entity_id == entity_id)

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    transactions = (
        db.execute(
            base.order_by(Transaction.created_at.desc()).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )

    txn_ids = [txn.id for txn in transactions]
    lines_by_txn: dict[uuid.UUID, list[JournalEntry]] = defaultdict(list)
    if txn_ids:
        for line in (
            db.execute(
                select(JournalEntry).where(JournalEntry.transaction_id.in_(txn_ids))
            )
            .scalars()
            .all()
        ):
            lines_by_txn[line.transaction_id].append(line)

    items = [
        JournalGroup(
            transaction_id=txn.id,
            vendor=txn.vendor,
            date=txn.transaction_date,
            total_debit=sum((ln.debit for ln in lines_by_txn[txn.id]), _ZERO),
            total_credit=sum((ln.credit for ln in lines_by_txn[txn.id]), _ZERO),
            lines=[
                JournalLineRead(
                    account_code=ln.account_code,
                    account_name=ln.account_name,
                    debit=ln.debit,
                    credit=ln.credit,
                    ledger=ln.ledger,
                )
                for ln in lines_by_txn[txn.id]
            ],
        )
        for txn in transactions
    ]
    return JournalsResponse(items=items, total=total)


@router.get("/gl", response_model=GeneralLedgerResponse)
def get_general_ledger(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> GeneralLedgerResponse:
    """General ledger: every posted account with its postings and running balance."""
    entity_id = _resolve_entity_id(db, company, entity)

    stmt = (
        select(JournalEntry, Account.category)
        .join(Transaction, Transaction.id == JournalEntry.transaction_id)
        .join(
            Account,
            (Account.company_id == Transaction.company_id)
            & (Account.account_code == JournalEntry.account_code),
        )
        .where(Transaction.company_id == company.id)
        .order_by(JournalEntry.account_code, JournalEntry.created_at)
    )
    if entity_id is not None:
        stmt = stmt.where(Transaction.entity_id == entity_id)

    grouped: dict[str, list[JournalEntry]] = defaultdict(list)
    category_by_code: dict[str, str] = {}
    for line, category in db.execute(stmt):
        grouped[line.account_code].append(line)
        category_by_code[line.account_code] = _category_str(category)

    accounts = [
        _gl_account(code, category_by_code[code], lines)
        for code, lines in sorted(grouped.items())
    ]
    return GeneralLedgerResponse(accounts=accounts)


@router.get("/tb", response_model=TrialBalanceResponse)
def get_trial_balance(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> TrialBalanceResponse:
    """Trial balance: debit/credit totals per account. `balanced` must be True."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    rows = [
        TrialBalanceRow(
            account_code=a.account_code,
            account_name=a.account_name,
            category=a.category,
            debit=a.debit,
            credit=a.credit,
        )
        for a in aggregates
    ]
    total_debit = sum((a.debit for a in aggregates), _ZERO)
    total_credit = sum((a.credit for a in aggregates), _ZERO)
    return TrialBalanceResponse(
        rows=rows,
        total_debit=total_debit,
        total_credit=total_credit,
        balanced=total_debit == total_credit,
    )


@router.get("/pl", response_model=ProfitAndLossResponse)
def get_profit_and_loss(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> ProfitAndLossResponse:
    """Profit & loss: income (credit-normal) less expenses (debit-normal)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    income = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "income"
    ]
    expenses = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.debit - a.credit)
        for a in aggregates
        if a.category == "expense"
    ]
    total_income = sum((line.amount for line in income), _ZERO)
    total_expenses = sum((line.amount for line in expenses), _ZERO)
    return ProfitAndLossResponse(
        income=income,
        expenses=expenses,
        total_income=total_income,
        total_expenses=total_expenses,
        net_profit=total_income - total_expenses,
    )


@router.get("/bs", response_model=BalanceSheetResponse)
def get_balance_sheet(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> BalanceSheetResponse:
    """Balance sheet: assets = liabilities + equity (incl. retained earnings)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    assets = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.debit - a.credit)
        for a in aggregates
        if a.category == "asset"
    ]
    liabilities = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "liability"
    ]
    equity = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "equity"
    ]
    # Undistributed P&L rolls into equity as retained earnings — the line that
    # makes assets == liabilities + equity hold under double entry.
    retained = _net_profit(aggregates)
    equity.append(
        BookLine(
            account_code=_RETAINED_EARNINGS_CODE,
            account_name=_RETAINED_EARNINGS_NAME,
            amount=retained,
        )
    )

    total_assets = sum((line.amount for line in assets), _ZERO)
    total_liabilities = sum((line.amount for line in liabilities), _ZERO)
    total_equity = sum((line.amount for line in equity), _ZERO)
    return BalanceSheetResponse(
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
        balanced=total_assets == total_liabilities + total_equity,
    )


@router.get("/cf", response_model=CashFlowResponse)
def get_cash_flow(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> CashFlowResponse:
    """Cash flow: net movement on cash/wallet accounts (MVP: all operating)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    operating = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 amount=a.debit - a.credit)
        for a in aggregates
        if a.account_code == WALLET_CODE
    ]
    return CashFlowResponse(
        operating=operating,
        net_cash_flow=sum((line.amount for line in operating), _ZERO),
    )
