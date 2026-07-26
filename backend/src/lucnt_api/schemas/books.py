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
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lucnt_api.constants import ENTITY_CODE_MAX, JOURNAL_LINES_MAX


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
    # How many journal lines this section totals. Always populated, so a client
    # can tell that postings exist even when `entries` is empty (T3-2).
    entry_count: int = 0
    # Populated only for a single-account request (`?account=CODE`). The default
    # listing returns totals per account and leaves this empty rather than
    # serialising every line in the ledger — `entry_count` is the honest signal
    # that lines exist, so an empty list is never mistaken for "no postings".
    entries: list[JournalLineRead] = Field(default_factory=list)


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


# --- Write endpoints: manual journals + funding (T1-18) ----------------------


class ManualJournalLineInput(BaseModel):
    """One requested line of a manual journal: a one-sided posting to an account."""

    model_config = ConfigDict(frozen=True)

    account_code: str = Field(min_length=1, max_length=20)
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def _exactly_one_side(self) -> ManualJournalLineInput:
        """A line is one-sided: exactly one of debit/credit is non-zero (T0-7)."""
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each line must have exactly one of debit/credit non-zero")
        return self


class ManualJournalRequest(BaseModel):
    """`POST /books/journal` — a balanced manual journal (accrual, adjustment).

    Admin-scoped and audited (T1-18). Booked in the entity's functional currency;
    the lines must balance (Σ debit == Σ credit) and reference real CoA accounts.
    """

    model_config = ConfigDict(frozen=True)

    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    memo: str = Field(min_length=1, max_length=500)
    posting_date: date | None = None
    lines: list[ManualJournalLineInput] = Field(
        min_length=2, max_length=JOURNAL_LINES_MAX
    )


class FundingRequest(BaseModel):
    """`POST /books/funding` — a wallet funding / opening-balance top-up (T1-18).

    Books Dr Wallet / Cr Contributed Capital in the entity's functional currency,
    so a funded entity's balance sheet no longer shows negative cash & equity from
    day one. Admin-scoped and audited.
    """

    model_config = ConfigDict(frozen=True)

    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    amount: Decimal = Field(gt=0)
    memo: str = Field(default="Wallet funding", min_length=1, max_length=500)
    posting_date: date | None = None


class PostedJournalResponse(BaseModel):
    """The journal booked by a manual-journal / funding post."""

    model_config = ConfigDict(frozen=True)

    entity: str
    posting_date: date
    currency: str
    memo: str
    total_debit: Decimal
    total_credit: Decimal
    lines: list[JournalLineRead]


# --- Return-cycle GST settlement (T1-11) -------------------------------------


class TaxSettlementRequest(BaseModel):
    """`POST /books/tax-settlement` — settle a return period's GST (admin)."""

    model_config = ConfigDict(frozen=True)

    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    date_from: date | None = None
    date_to: date | None = None
    memo: str = Field(default="GST return settlement", min_length=1, max_length=500)
    posting_date: date | None = None


class TaxSettlementHead(BaseModel):
    """One GST head's output tax and input credit for the return period."""

    model_config = ConfigDict(frozen=True)

    head: str
    output_tax: Decimal
    input_credit: Decimal


class TaxSettlementResponse(BaseModel):
    """The computed GST position + the settlement journal booked (if any).

    Clears the accumulated per-head GST Payable against Input Tax Credit so the
    balance sheet no longer shows an ever-growing liability + asset (T1-11); the
    net is booked as GST Payable to Government, and any excess ITC carries forward.
    """

    model_config = ConfigDict(frozen=True)

    entity: str
    currency: str
    posting_date: date
    total_output_tax: Decimal
    total_input_credit: Decimal
    net_payable: Decimal
    credit_carried_forward: Decimal
    heads: list[TaxSettlementHead]
    journal: PostedJournalResponse | None
