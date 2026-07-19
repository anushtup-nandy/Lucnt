"""Pydantic response schemas for the Books router (backend/PLAN.md §4, Module 2/4).

All figures are derived read-only from `journal_entries` joined to
`transactions` (company scope) and `accounts` (category). Money is `Decimal`.
Sign convention: an account's ``balance`` is ``debit - credit`` (positive = net
debit); statements re-sign per the account's normal side.

**Currency (PRODUCTION_PLAN T1-1).** Every figure is denominated in a single
functional currency and carries its ``currency``. A statement is inherently
per-currency: a trial balance / balance sheet only balances *within* one
currency, so the balance identities are reported per currency (``currencies``)
and the top-level ``balanced`` holds only if *every* currency balances. A
consolidated multi-currency total (FX-translated to a group reporting currency)
is deferred (T1-6); the books never sum mixed currencies into one bogus total.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class JournalLineRead(BaseModel):
    """One posting within a journal group / general-ledger account."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal
    ledger: str
    currency: str


class JournalGroup(BaseModel):
    """All postings for a single transaction (the journal explorer's unit).

    One transaction posts to one entity, so a group is single-currency.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: uuid.UUID
    vendor: str
    date: datetime
    currency: str
    total_debit: Decimal
    total_credit: Decimal
    lines: list[JournalLineRead]


class JournalsResponse(BaseModel):
    """`GET /books/journals` — journals grouped by transaction, paginated."""

    model_config = ConfigDict(frozen=True)

    items: list[JournalGroup]
    total: int


class GLAccount(BaseModel):
    """One account's postings and running totals in the general ledger.

    An account is sectioned per currency — the same account code posted to by
    entities in different currencies yields one section per currency.
    """

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    category: str
    currency: str
    total_debit: Decimal
    total_credit: Decimal
    balance: Decimal
    entries: list[JournalLineRead]


class GeneralLedgerResponse(BaseModel):
    """`GET /books/gl` — general ledger, one section per posted account/currency."""

    model_config = ConfigDict(frozen=True)

    accounts: list[GLAccount]


class TrialBalanceRow(BaseModel):
    """One account's debit/credit totals in the trial balance."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    category: str
    currency: str
    debit: Decimal
    credit: Decimal


class CurrencyTotals(BaseModel):
    """Per-currency debit/credit totals; `balanced` is true when they are equal."""

    model_config = ConfigDict(frozen=True)

    currency: str
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


class TrialBalanceResponse(BaseModel):
    """`GET /books/tb` — trial balance. `balanced` iff every currency balances."""

    model_config = ConfigDict(frozen=True)

    rows: list[TrialBalanceRow]
    currencies: list[CurrencyTotals]
    balanced: bool


class BookLine(BaseModel):
    """A single account line on the P&L / balance sheet / cash flow."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    currency: str
    amount: Decimal


class ProfitAndLossTotals(BaseModel):
    """Per-currency P&L totals."""

    model_config = ConfigDict(frozen=True)

    currency: str
    total_income: Decimal
    total_expenses: Decimal
    net_profit: Decimal


class ProfitAndLossResponse(BaseModel):
    """`GET /books/pl` — income less expenses, per currency."""

    model_config = ConfigDict(frozen=True)

    income: list[BookLine]
    expenses: list[BookLine]
    currencies: list[ProfitAndLossTotals]


class BalanceSheetTotals(BaseModel):
    """Per-currency balance-sheet totals; `balanced` iff assets = liab + equity."""

    model_config = ConfigDict(frozen=True)

    currency: str
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    balanced: bool


class BalanceSheetResponse(BaseModel):
    """`GET /books/bs` — assets = liabilities + equity, per currency."""

    model_config = ConfigDict(frozen=True)

    assets: list[BookLine]
    liabilities: list[BookLine]
    equity: list[BookLine]
    currencies: list[BalanceSheetTotals]
    balanced: bool


class CashFlowTotals(BaseModel):
    """Per-currency net cash movement."""

    model_config = ConfigDict(frozen=True)

    currency: str
    net_cash_flow: Decimal


class CashFlowResponse(BaseModel):
    """`GET /books/cf` — movement on cash/wallet accounts, per currency."""

    model_config = ConfigDict(frozen=True)

    operating: list[BookLine]
    currencies: list[CashFlowTotals]
