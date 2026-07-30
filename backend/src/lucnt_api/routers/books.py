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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from lucnt_api.constants import ACCOUNT_CODE_MAX
from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_admin,
    require_nav,
)
from lucnt_api.models import (
    Account,
    AccountCategory,
    Company,
    Entity,
    JournalEntry,
    Transaction,
)
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.books import (
    BalanceSheetResponse,
    BalanceSheetTotals,
    BookLine,
    CashFlowResponse,
    CashFlowTotals,
    CurrencyTotals,
    FundingRequest,
    GeneralLedgerResponse,
    GLAccount,
    JournalGroup,
    JournalLineRead,
    JournalsResponse,
    ManualJournalRequest,
    PostedJournalResponse,
    ProfitAndLossResponse,
    ProfitAndLossTotals,
    TaxSettlementHead,
    TaxSettlementRequest,
    TaxSettlementResponse,
    TrialBalanceResponse,
    TrialBalanceRow,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.chart_of_accounts import (
    CONTRIBUTED_CAPITAL_CODE,
    GST_PAYABLE_BY_HEAD,
    GST_SETTLEMENT_PAYABLE_CODE,
    ITC_BY_HEAD,
    RETAINED_EARNINGS_CODE,
    WALLET_CODE,
    ensure_default_accounts,
)

# Router-level nav gate (P7.0): a human principal may only reach a domain
# present in `perms.nav` — the same tuple the sidebar renders from, so a
# hidden page and a reachable endpoint cannot drift apart. No-op for API keys.
router = APIRouter(
    prefix="/books",
    tags=["books"],
    dependencies=[Depends(require_nav("books"))],
)

_ZERO = Decimal("0")
_RETAINED_EARNINGS_NAME = "Retained Earnings"
# Category for a posted line whose account code resolves to no CoA account
# (T1-21) — surfaced loudly rather than silently dropped by an inner join.
_UNMAPPED = "unmapped"

_FROM_DESC = "Include transactions on/after this date (YYYY-MM-DD)"
_TO_DESC = "Include transactions on/before this date (YYYY-MM-DD, inclusive)"


def _as_dt(day: date) -> datetime:
    """Midnight (UTC) at the start of `day`, to compare against transaction_date."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _posting_date_conditions(
    date_from: date | None, date_to: date | None
) -> list[ColumnElement[bool]]:
    """Restrict a journal line's `posting_date` to a period (T1-17/T1-21).

    The statements periodise off the line's own ``posting_date``, so they are true
    period statements independent of any transaction join and cover manual/funding/
    settlement journals (which have no transaction). Both bounds are inclusive.
    """
    conditions: list[ColumnElement[bool]] = []
    if date_from is not None:
        conditions.append(JournalEntry.posting_date >= date_from)
    if date_to is not None:
        conditions.append(JournalEntry.posting_date <= date_to)
    return conditions


def _txn_date_conditions(
    date_from: date | None, date_to: date | None
) -> list[ColumnElement[bool]]:
    """Period conditions on `transaction_date` for the txn-grouped journal explorer."""
    conditions: list[ColumnElement[bool]] = []
    if date_from is not None:
        conditions.append(Transaction.transaction_date >= _as_dt(date_from))
    if date_to is not None:
        conditions.append(
            Transaction.transaction_date < _as_dt(date_to) + timedelta(days=1)
        )
    return conditions


@dataclass(frozen=True)
class _Aggregate:
    """Per-account, per-currency debit/credit totals across posted journal lines."""

    account_code: str
    account_name: str
    category: str
    currency: str
    debit: Decimal
    credit: Decimal
    # Number of journal lines behind these totals. Only the general ledger reads
    # it (T3-2); the trial balance and P&L ignore it.
    line_count: int = 0


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


def _require_entity(db: Session, company: Company, code: str) -> Entity:
    """Resolve an entity code to its row, company-scoped (404 if unknown)."""
    entity = db.execute(
        select(Entity).where(Entity.company_id == company.id, Entity.code == code)
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown entity: {code}")
    return entity


def _aggregates(
    db: Session,
    company: Company,
    entity_id: uuid.UUID | None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[_Aggregate]:
    """Sum debit/credit per (currency, account) over the journal, period-scoped."""
    stmt = (
        select(
            JournalEntry.account_code,
            Account.account_name,
            Account.category,
            JournalEntry.currency,
            func.coalesce(func.sum(JournalEntry.debit), _ZERO).label("total_debit"),
            func.coalesce(func.sum(JournalEntry.credit), _ZERO).label("total_credit"),
            func.count().label("line_count"),
        )
        .select_from(JournalEntry)
        .outerjoin(
            Account,
            (Account.company_id == JournalEntry.company_id)
            & (Account.account_code == JournalEntry.account_code),
        )
        .where(JournalEntry.company_id == company.id)
        .group_by(
            JournalEntry.account_code,
            Account.account_name,
            Account.category,
            JournalEntry.currency,
        )
        .order_by(JournalEntry.currency, JournalEntry.account_code)
    )
    if entity_id is not None:
        stmt = stmt.where(JournalEntry.entity_id == entity_id)
    for condition in _posting_date_conditions(date_from, date_to):
        stmt = stmt.where(condition)

    return [
        _Aggregate(
            account_code=row.account_code,
            account_name=row.account_name or f"UNMAPPED {row.account_code}",
            category=(
                _category_str(row.category) if row.category is not None else _UNMAPPED
            ),
            currency=row.currency,
            debit=row.total_debit,
            credit=row.total_credit,
            line_count=row.line_count,
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


def _gl_entries(
    db: Session,
    company: Company,
    entity_id: uuid.UUID | None,
    account_code: str,
    date_from: date | None,
    date_to: date | None,
    page: Page,
) -> dict[tuple[str, str], list[JournalLineRead]]:
    """One page of a single account's journal lines, keyed by (code, currency).

    Scoped to one account and bounded by `page` so the general ledger never
    materialises the whole journal (T3-2). Ordered by currency then posting time
    so paging is stable; a page may therefore straddle two currency sections of
    the same account.

    Args:
        db: Session to query.
        company: Tenant scope.
        entity_id: Optional entity filter, already resolved from its code.
        account_code: The single account whose lines to read.
        date_from: Inclusive period start, or None.
        date_to: Inclusive period end, or None.
        page: The `(limit, offset)` window.

    Returns:
        `{(account_code, currency): [line, ...]}` for the requested page.
    """
    stmt = (
        select(JournalEntry)
        .where(
            JournalEntry.company_id == company.id,
            JournalEntry.account_code == account_code,
        )
        .order_by(JournalEntry.currency, JournalEntry.created_at)
        .offset(page.offset)
        .limit(page.limit)
    )
    if entity_id is not None:
        stmt = stmt.where(JournalEntry.entity_id == entity_id)
    for condition in _posting_date_conditions(date_from, date_to):
        stmt = stmt.where(condition)

    grouped: dict[tuple[str, str], list[JournalLineRead]] = defaultdict(list)
    for line in db.execute(stmt).scalars():
        grouped[(line.account_code, line.currency)].append(
            JournalLineRead(
                account_code=line.account_code,
                account_name=line.account_name,
                debit=line.debit,
                credit=line.credit,
                ledger=line.ledger,
                currency=line.currency,
            )
        )
    return grouped


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
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
    page: Page = Depends(paginate()),
) -> JournalsResponse:
    """Journal explorer: postings grouped by transaction, newest first."""
    entity_id = _resolve_entity_id(db, company, entity)

    posted = select(JournalEntry.transaction_id)
    base = select(Transaction).where(
        Transaction.company_id == company.id, Transaction.id.in_(posted)
    )
    if entity_id is not None:
        base = base.where(Transaction.entity_id == entity_id)
    for condition in _txn_date_conditions(date_from, date_to):
        base = base.where(condition)

    total = count_total(db, base)
    transactions = (
        db.execute(
            base.order_by(Transaction.created_at.desc())
            .limit(page.limit)
            .offset(page.offset)
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
            # transaction_id is nullable (manual journals), but this query filters
            # on the fetched txn ids, so every row here has one — narrow for mypy.
            if line.transaction_id is not None:
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
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
    account: str | None = Query(
        default=None,
        max_length=ACCOUNT_CODE_MAX,
        description="Return this account's individual journal lines",
    ),
    page: Page = Depends(paginate()),
) -> GeneralLedgerResponse:
    """General ledger: every posted account/currency with its totals and balance.

    Two shapes, because the line-level view is the only unbounded one (T3-2):

    * **Listing** (no `account`) — totals per account/currency, computed by a
      SQL `GROUP BY`. Costs one row per account, not one per journal line, so it
      no longer loads a 500k-line ledger into memory to add up six numbers.
      `entries` is empty here and `entry_count` reports how many lines exist.
    * **Detail** (`?account=CODE`) — the same totals plus that account's lines,
      bounded by `limit`/`offset`. Journal lines run 2-5x transaction count, so
      this is the one place the row-level cost has to be paid, and it is paid a
      page at a time.
    """
    entity_id = _resolve_entity_id(db, company, entity)

    # Totals come from the same grouped aggregate the trial balance and P&L use,
    # so the three statements can never disagree about an account's balance.
    aggregates = [
        a
        for a in _aggregates(db, company, entity_id, date_from, date_to)
        if account is None or a.account_code == account
    ]

    entries_by_key: dict[tuple[str, str], list[JournalLineRead]] = {}
    if account is not None:
        entries_by_key = _gl_entries(
            db, company, entity_id, account, date_from, date_to, page
        )

    accounts = [
        GLAccount(
            account_code=a.account_code,
            account_name=a.account_name,
            category=a.category,
            currency=a.currency,
            total_debit=a.debit,
            total_credit=a.credit,
            balance=a.debit - a.credit,
            entry_count=a.line_count,
            entries=entries_by_key.get((a.account_code, a.currency), []),
        )
        for a in aggregates
    ]
    return GeneralLedgerResponse(accounts=accounts)


@router.get("/tb", response_model=TrialBalanceResponse)
def get_trial_balance(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
) -> TrialBalanceResponse:
    """Trial balance per currency; `balanced` iff every currency balances."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id, date_from, date_to)

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
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
) -> ProfitAndLossResponse:
    """Profit & loss per currency: income (credit-normal) less expenses."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id, date_from, date_to)

    income = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.credit - a.debit,
        )
        for a in aggregates
        if a.category == "income"
    ]
    expenses = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.debit - a.credit,
        )
        for a in aggregates
        if a.category == "expense"
    ]
    currencies = [
        ProfitAndLossTotals(
            currency=ccy,
            total_income=(
                ti := sum(
                    (a.credit - a.debit for a in aggs if a.category == "income"), _ZERO
                )
            ),
            total_expenses=(
                te := sum(
                    (a.debit - a.credit for a in aggs if a.category == "expense"), _ZERO
                )
            ),
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
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
) -> BalanceSheetResponse:
    """Balance sheet per currency: assets = liabilities + equity (incl. RE)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id, date_from, date_to)

    assets = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.debit - a.credit,
        )
        for a in aggregates
        if a.category == "asset"
    ]
    liabilities = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.credit - a.debit,
        )
        for a in aggregates
        if a.category == "liability"
    ]
    equity = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.credit - a.debit,
        )
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
                account_code=RETAINED_EARNINGS_CODE,
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
    date_from: date | None = Query(default=None, description=_FROM_DESC),
    date_to: date | None = Query(default=None, description=_TO_DESC),
) -> CashFlowResponse:
    """Cash flow per currency: net movement on cash/wallet accounts (MVP: operating)."""
    entity_id = _resolve_entity_id(db, company, entity)
    aggregates = _aggregates(db, company, entity_id, date_from, date_to)

    operating = [
        BookLine(
            account_code=a.account_code,
            account_name=a.account_name,
            currency=a.currency,
            amount=a.debit - a.credit,
        )
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


# --- Write endpoints: manual journals + funding (T1-18) ----------------------


def _post_journal(
    db: Session,
    company: Company,
    entity: Entity,
    posting_date: date,
    memo: str,
    currency: str,
    specs: list[tuple[str, Decimal, Decimal]],
    accounts: dict[str, Account],
) -> PostedJournalResponse:
    """Build, balance-check and stage a set of self-sufficient journal lines.

    Each spec is ``(account_code, debit, credit)``. The lines carry no
    ``transaction_id`` (T1-18) and must reference real CoA accounts and balance
    (Σ debit == Σ credit). Does not commit — the caller owns audit + commit.
    """
    entries: list[JournalEntry] = []
    read_lines: list[JournalLineRead] = []
    total_debit = _ZERO
    total_credit = _ZERO
    for code, debit, credit in specs:
        account = accounts.get(code)
        if account is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown account code: {code}",
            )
        entries.append(
            JournalEntry(
                transaction_id=None,
                company_id=company.id,
                entity_id=entity.id,
                posting_date=posting_date,
                account_name=account.account_name,
                account_code=code,
                debit=debit,
                credit=credit,
                ledger=entity.code,
                currency=currency,
            )
        )
        read_lines.append(
            JournalLineRead(
                account_code=code,
                account_name=account.account_name,
                debit=debit,
                credit=credit,
                ledger=entity.code,
                currency=currency,
            )
        )
        total_debit += debit
        total_credit += credit

    if total_debit != total_credit:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"journal does not balance: debit {total_debit} != credit {total_credit}",
        )

    db.add_all(entries)
    return PostedJournalResponse(
        entity=entity.code,
        posting_date=posting_date,
        currency=currency,
        memo=memo,
        total_debit=total_debit,
        total_credit=total_credit,
        lines=read_lines,
    )


@router.post(
    "/journal",
    response_model=PostedJournalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_journal(
    request: ManualJournalRequest,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> PostedJournalResponse:
    """Post a balanced manual journal — an accrual, prepaid, or correction (T1-18).

    Admin-scoped and audited. Booked in the entity's functional currency; the
    lines must reference real CoA accounts and balance.
    """
    entity = _require_entity(db, company, request.entity)
    accounts = ensure_default_accounts(db, company.id)
    posting_date = request.posting_date or datetime.now(UTC).date()
    specs = [(ln.account_code, ln.debit, ln.credit) for ln in request.lines]
    result = _post_journal(
        db,
        company,
        entity,
        posting_date,
        request.memo,
        entity.currency,
        specs,
        accounts,
    )
    record_audit(
        db,
        ctx,
        action="books.manual_journal",
        entity_type="journal",
        before=None,
        after={
            "entity": entity.code,
            "posting_date": posting_date.isoformat(),
            "memo": request.memo,
            "total_debit": str(result.total_debit),
            "currency": entity.currency,
        },
    )
    db.commit()
    return result


@router.post(
    "/funding",
    response_model=PostedJournalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_funding(
    request: FundingRequest,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> PostedJournalResponse:
    """Fund an entity's wallet: Dr Wallet / Cr Contributed Capital (T1-18).

    The funding/opening-balance entry the ledger previously lacked — without it
    every balance sheet showed negative cash & equity from day one (the wallet was
    only ever credited by spend). Admin-scoped, audited, functional currency.
    """
    entity = _require_entity(db, company, request.entity)
    accounts = ensure_default_accounts(db, company.id)
    posting_date = request.posting_date or datetime.now(UTC).date()
    specs = [
        (WALLET_CODE, request.amount, _ZERO),
        (CONTRIBUTED_CAPITAL_CODE, _ZERO, request.amount),
    ]
    result = _post_journal(
        db,
        company,
        entity,
        posting_date,
        request.memo,
        entity.currency,
        specs,
        accounts,
    )
    record_audit(
        db,
        ctx,
        action="books.funding",
        entity_type="journal",
        before=None,
        after={
            "entity": entity.code,
            "posting_date": posting_date.isoformat(),
            "amount": str(request.amount),
            "currency": entity.currency,
        },
    )
    db.commit()
    return result


# Utilise input tax credit against output GST in this head order (IGST first, then
# CGST/SGST, then the legacy unsplit head) — the legal set-off order.
_GST_HEADS: tuple[str, ...] = ("IGST", "CGST", "SGST", "GST")


@router.post(
    "/tax-settlement",
    response_model=TaxSettlementResponse,
    status_code=status.HTTP_201_CREATED,
)
def settle_taxes(
    request: TaxSettlementRequest,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> TaxSettlementResponse:
    """Book a return-cycle GST settlement, clearing Payable against ITC (T1-11).

    Nets the period's accumulated output GST (the per-head Payable accounts) against
    input tax credit (the per-head ITC accounts), books the residual as GST Payable
    to Government, and carries forward any unutilised ITC — so the balance sheet no
    longer shows an ever-growing liability + asset. Admin-scoped and audited.
    """
    entity = _require_entity(db, company, request.entity)
    accounts = ensure_default_accounts(db, company.id)
    posting_date = request.posting_date or request.date_to or datetime.now(UTC).date()

    aggregates = _aggregates(db, company, entity.id, request.date_from, request.date_to)
    by_code = {a.account_code: a for a in aggregates if a.currency == entity.currency}

    heads: list[TaxSettlementHead] = []
    output: dict[str, Decimal] = {}
    itc: dict[str, Decimal] = {}
    for head in _GST_HEADS:
        p = by_code.get(GST_PAYABLE_BY_HEAD[head])
        i = by_code.get(ITC_BY_HEAD[head])
        # Payable is credit-normal, ITC debit-normal.
        output[head] = (p.credit - p.debit) if p is not None else _ZERO
        itc[head] = (i.debit - i.credit) if i is not None else _ZERO
        if output[head] != _ZERO or itc[head] != _ZERO:
            heads.append(
                TaxSettlementHead(
                    head=head, output_tax=output[head], input_credit=itc[head]
                )
            )

    total_output = sum(output.values(), _ZERO)
    total_itc = sum(itc.values(), _ZERO)
    utilised = min(total_output, total_itc)
    net_payable = total_output - utilised
    carried_forward = total_itc - utilised

    # Dr each accumulated Payable to clear it; Cr ITC up to the utilised amount in
    # set-off order; Cr the net to GST Payable to Government. Balances by
    # construction: Σ Payable(Dr) == utilised ITC(Cr) + net payable(Cr).
    specs: list[tuple[str, Decimal, Decimal]] = []
    for head in _GST_HEADS:
        if output[head] > _ZERO:
            specs.append((GST_PAYABLE_BY_HEAD[head], output[head], _ZERO))
    remaining = utilised
    for head in _GST_HEADS:
        if remaining <= _ZERO:
            break
        use = min(itc[head], remaining)
        if use > _ZERO:
            specs.append((ITC_BY_HEAD[head], _ZERO, use))
            remaining -= use
    if net_payable > _ZERO:
        specs.append((GST_SETTLEMENT_PAYABLE_CODE, _ZERO, net_payable))

    journal = None
    if specs:
        journal = _post_journal(
            db,
            company,
            entity,
            posting_date,
            request.memo,
            entity.currency,
            specs,
            accounts,
        )
        record_audit(
            db,
            ctx,
            action="books.tax_settlement",
            entity_type="journal",
            before=None,
            after={
                "entity": entity.code,
                "posting_date": posting_date.isoformat(),
                "net_payable": str(net_payable),
                "currency": entity.currency,
            },
        )
        db.commit()

    return TaxSettlementResponse(
        entity=entity.code,
        currency=entity.currency,
        posting_date=posting_date,
        total_output_tax=total_output,
        total_input_credit=total_itc,
        net_payable=net_payable,
        credit_carried_forward=carried_forward,
        heads=heads,
        journal=journal,
    )
