"""Pydantic response schemas for the Books router (backend/PLAN.md §4, Module 2/4).

All figures are derived read-only from `journal_entries` joined to
`transactions` (company scope) and `accounts` (category). Money is `Decimal`.
Sign convention: an account's ``balance`` is ``debit - credit`` (positive = net
debit); statements re-sign per the account's normal side.
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


class JournalGroup(BaseModel):
    """All postings for a single transaction (the journal explorer's unit)."""

    model_config = ConfigDict(frozen=True)

    transaction_id: uuid.UUID
    vendor: str
    date: datetime
    total_debit: Decimal
    total_credit: Decimal
    lines: list[JournalLineRead]


class JournalsResponse(BaseModel):
    """`GET /books/journals` — journals grouped by transaction, paginated."""

    model_config = ConfigDict(frozen=True)

    items: list[JournalGroup]
    total: int


class GLAccount(BaseModel):
    """One account's postings and running totals in the general ledger."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    category: str
    total_debit: Decimal
    total_credit: Decimal
    balance: Decimal
    entries: list[JournalLineRead]


class GeneralLedgerResponse(BaseModel):
    """`GET /books/gl` — general ledger, one section per posted account."""

    model_config = ConfigDict(frozen=True)

    accounts: list[GLAccount]


class TrialBalanceRow(BaseModel):
    """One account's debit/credit totals in the trial balance."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    category: str
    debit: Decimal
    credit: Decimal


class TrialBalanceResponse(BaseModel):
    """`GET /books/tb` — trial balance; `balanced` must be True for sound books."""

    model_config = ConfigDict(frozen=True)

    rows: list[TrialBalanceRow]
    total_debit: Decimal
    total_credit: Decimal
    balanced: bool


class BookLine(BaseModel):
    """A single account line on the P&L / balance sheet / cash flow."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    amount: Decimal


class ProfitAndLossResponse(BaseModel):
    """`GET /books/pl` — income less expenses over all posted transactions."""

    model_config = ConfigDict(frozen=True)

    income: list[BookLine]
    expenses: list[BookLine]
    total_income: Decimal
    total_expenses: Decimal
    net_profit: Decimal


class BalanceSheetResponse(BaseModel):
    """`GET /books/bs` — assets = liabilities + equity (incl. retained earnings)."""

    model_config = ConfigDict(frozen=True)

    assets: list[BookLine]
    liabilities: list[BookLine]
    equity: list[BookLine]
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    balanced: bool


class CashFlowResponse(BaseModel):
    """`GET /books/cf` — movement on cash/wallet accounts (MVP: all operating)."""

    model_config = ConfigDict(frozen=True)

    operating: list[BookLine]
    net_cash_flow: Decimal
