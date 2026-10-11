"""Books schemas. ``balance`` is debit - credit (positive = net debit).

Statements are per-currency; mixed currencies are never summed into one total.
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
    """All postings for one transaction; always single-currency."""

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
    """One account's general-ledger totals, sectioned per currency."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    category: str
    currency: str
    total_debit: Decimal
    total_credit: Decimal
    balance: Decimal
    # Always populated, so an empty `entries` is never read as "no postings".
    entry_count: int = 0
    # Only filled for a single-account request (`?account=CODE`).
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


# --- Write endpoints: manual journals + funding ----------------------------


class ManualJournalLineInput(BaseModel):
    """One requested line of a manual journal: a one-sided posting to an account."""

    model_config = ConfigDict(frozen=True)

    account_code: str = Field(min_length=1, max_length=20)
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def _exactly_one_side(self) -> ManualJournalLineInput:
        """A line is one-sided: exactly one of debit/credit is non-zero."""
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each line must have exactly one of debit/credit non-zero")
        return self


class ManualJournalRequest(BaseModel):
    """`POST /books/journal` — a balanced manual journal in functional currency.

    Admin-only and audited; lines must balance and reference real CoA accounts.
    """

    model_config = ConfigDict(frozen=True)

    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    memo: str = Field(min_length=1, max_length=500)
    posting_date: date | None = None
    lines: list[ManualJournalLineInput] = Field(
        min_length=2, max_length=JOURNAL_LINES_MAX
    )


class FundingRequest(BaseModel):
    """`POST /books/funding` — Dr Wallet / Cr Contributed Capital top-up.

    Admin-only and audited.
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


# --- Return-cycle GST settlement -------------------------------------------


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
    """GST position and settlement journal: per-head payable cleared against ITC.

    The net is booked as GST Payable to Government; excess ITC carries forward.
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
