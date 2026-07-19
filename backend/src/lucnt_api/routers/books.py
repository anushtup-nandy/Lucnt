"""Books router — read-only financial statements (backend/PLAN.md §4, Module 2/4).

Every figure derives from `journal_entries` joined to `transactions` (for
company/entity scope) and `accounts` (for category). Nothing here writes; the
journal is produced at capture time by `services/journal_generator.py`.

**Per-currency (PRODUCTION_PLAN T1-1).** Journal lines carry a functional
currency; a statement only balances *within* one currency. Every figure is
grouped and totalled per currency, so a company spanning entities in INR/SGD/USD
gets one set of per-currency totals rather than a single meaningless mixed sum.
A consolidated (FX-translated) view is deferred to T1-6.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lucnt_api.dependencies import get_current_company, get_db
from lucnt_api.models import (
    Account,
    AccountCategory,
    Company,
    Entity,
    JournalEntry,
    Transaction,
)
from lucnt_api.schemas.books import (
    BalanceSheetResponse,
    BalanceSheetTotals,
    BookLine,
    CashFlowResponse,
    CashFlowTotals,
    CurrencyTotals,
    GeneralLedgerResponse,
    GLAccount,
    JournalGroup,
    JournalLineRead,
    JournalsResponse,
    ProfitAndLossResponse,
    ProfitAndLossTotals,
    TrialBalanceResponse,
    TrialBalanceRow,
)
from lucnt_api.services.chart_of_accounts import WALLET_CODE

router = APIRouter(prefix="/books", tags=["books"])

_ZERO = Decimal("0")
_RETAINED_EARNINGS_CODE = "3900"
_RETAINED_EARNINGS_NAME = "Retained Earnings"


@dataclass(frozen=True)
class _Aggregate:
    """Per-account, per-currency debit/credit totals across posted journal lines."""

    account_code: str
    account_name: str
    category: str
    currency: str
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
    """Sum debit/credit per (currency, account) over the company's journal."""
    stmt = (
        select(
            JournalEntry.account_code,
            JournalEntry.account_name,
            Account.category,
            JournalEntry.currency,
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
            JournalEntry.currency,
        )
        .order_by(JournalEntry.currency, JournalEntry.account_code)
    )
    if entity_id is not None:
        stmt = stmt.where(Transaction.entity_id == entity_id)

    return [
        _Aggregate(
            account_code=row.account_code,
            account_name=row.account_name,
            category=_category_str(row.category),
            currency=row.currency,
            debit=row.total_debit,
            credit=row.total_credit,
        )
        for row in db.execute(stmt)
    ]


def _by_currency(aggregates: list[_Aggregate]) -> dict[str, list[_Aggregate]]:
    """Bucket aggregates by their currency, preserving order."""
    buckets: dict[str, list[_Aggregate]] = defaultdict(list)
    for a in aggregates:
        buckets[a.currency].append(a)
    return buckets


def _category_str(category: AccountCategory | str) -> str:
    """Normalise an account category to its string value."""
    return category.value if isinstance(category, AccountCategory) else str(category)


def _gl_account(
    code: str, category: str, currency: str, lines: list[JournalEntry]
) -> GLAccount:
    """Assemble one general-ledger account section (per account + currency)."""
    total_debit = sum((ln.debit for ln in lines), _ZERO)
    total_credit = sum((ln.credit for ln in lines), _ZERO)
    return GLAccount(
        account_code=code,
        account_name=lines[0].account_name,
        category=category,
        currency=currency,
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
                currency=ln.currency,
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
            currency=txn.local_currency,
            total_debit=sum((ln.debit for ln in lines_by_txn[txn.id]), _ZERO),
            total_credit=sum((ln.credit for ln in lines_by_txn[txn.id]), _ZERO),
            lines=[
                JournalLineRead(
                    account_code=ln.account_code,
                    account_name=ln.account_name,
                    debit=ln.debit,
                    credit=ln.credit,
                    ledger=ln.ledger,
                    currency=ln.currency,
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
    """General ledger: every posted account/currency with postings and balance."""
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
        .order_by(
            JournalEntry.currency,
            JournalEntry.account_code,
            JournalEntry.created_at,
        )
    )
    if entity_id is not None:
        stmt = stmt.where(Transaction.entity_id == entity_id)

    # Key each section by (account_code, currency): the same account posted to in
    # two currencies is two sections, never one mixed-currency balance.
    grouped: dict[tuple[str, str], list[JournalEntry]] = defaultdict(list)
    category_by_key: dict[tuple[str, str], str] = {}
    for line, category in db.execute(stmt):
        key = (line.account_code, line.currency)
        grouped[key].append(line)
        category_by_key[key] = _category_str(category)

    accounts = [
        _gl_account(code, category_by_key[(code, currency)], currency, lines)
        for (code, currency), lines in sorted(grouped.items())
    ]
    return GeneralLedgerResponse(accounts=accounts)


@router.get("/tb", response_model=TrialBalanceResponse)
def get_trial_balance(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> TrialBalanceResponse:
    """Trial balance per currency; `balanced` iff every currency balances."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    rows = [
        TrialBalanceRow(
            account_code=a.account_code,
            account_name=a.account_name,
            category=a.category,
            currency=a.currency,
            debit=a.debit,
            credit=a.credit,
        )
        for a in aggregates
    ]
    currencies = [
        CurrencyTotals(
            currency=ccy,
            total_debit=(td := sum((a.debit for a in aggs), _ZERO)),
            total_credit=(tc := sum((a.credit for a in aggs), _ZERO)),
            balanced=td == tc,
        )
        for ccy, aggs in sorted(_by_currency(aggregates).items())
    ]
    return TrialBalanceResponse(
        rows=rows,
        currencies=currencies,
        balanced=all(c.balanced for c in currencies),
    )


@router.get("/pl", response_model=ProfitAndLossResponse)
def get_profit_and_loss(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> ProfitAndLossResponse:
    """Profit & loss per currency: income (credit-normal) less expenses."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    income = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "income"
    ]
    expenses = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.debit - a.credit)
        for a in aggregates
        if a.category == "expense"
    ]
    currencies = [
        ProfitAndLossTotals(
            currency=ccy,
            total_income=(ti := sum(
                (a.credit - a.debit for a in aggs if a.category == "income"), _ZERO
            )),
            total_expenses=(te := sum(
                (a.debit - a.credit for a in aggs if a.category == "expense"), _ZERO
            )),
            net_profit=ti - te,
        )
        for ccy, aggs in sorted(_by_currency(aggregates).items())
    ]
    return ProfitAndLossResponse(
        income=income, expenses=expenses, currencies=currencies
    )


@router.get("/bs", response_model=BalanceSheetResponse)
def get_balance_sheet(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> BalanceSheetResponse:
    """Balance sheet per currency: assets = liabilities + equity (incl. RE)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    assets = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.debit - a.credit)
        for a in aggregates
        if a.category == "asset"
    ]
    liabilities = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "liability"
    ]
    equity = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.credit - a.debit)
        for a in aggregates
        if a.category == "equity"
    ]

    currencies: list[BalanceSheetTotals] = []
    for ccy, aggs in sorted(_by_currency(aggregates).items()):
        # Undistributed P&L rolls into equity as retained earnings — the line
        # that makes assets == liabilities + equity hold, per currency.
        retained = _net_profit(aggs)
        equity.append(
            BookLine(
                account_code=_RETAINED_EARNINGS_CODE,
                account_name=_RETAINED_EARNINGS_NAME,
                currency=ccy,
                amount=retained,
            )
        )
        total_assets = sum(
            (a.debit - a.credit for a in aggs if a.category == "asset"), _ZERO
        )
        total_liabilities = sum(
            (a.credit - a.debit for a in aggs if a.category == "liability"), _ZERO
        )
        total_equity = (
            sum((a.credit - a.debit for a in aggs if a.category == "equity"), _ZERO)
            + retained
        )
        currencies.append(
            BalanceSheetTotals(
                currency=ccy,
                total_assets=total_assets,
                total_liabilities=total_liabilities,
                total_equity=total_equity,
                balanced=total_assets == total_liabilities + total_equity,
            )
        )

    return BalanceSheetResponse(
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        currencies=currencies,
        balanced=all(c.balanced for c in currencies),
    )


@router.get("/cf", response_model=CashFlowResponse)
def get_cash_flow(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
) -> CashFlowResponse:
    """Cash flow per currency: net movement on cash/wallet accounts (MVP: operating)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id)

    operating = [
        BookLine(account_code=a.account_code, account_name=a.account_name,
                 currency=a.currency, amount=a.debit - a.credit)
        for a in aggregates
        if a.account_code == WALLET_CODE
    ]
    currencies = [
        CashFlowTotals(
            currency=ccy,
            net_cash_flow=sum(
                (a.debit - a.credit for a in aggs if a.account_code == WALLET_CODE),
                _ZERO,
            ),
        )
        for ccy, aggs in sorted(_by_currency(aggregates).items())
    ]
    return CashFlowResponse(operating=operating, currencies=currencies)
