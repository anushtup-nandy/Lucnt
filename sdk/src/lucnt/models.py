"""Typed data structures shared across the lucnt SDK."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LucntConfig(BaseModel):
    """Resolved SDK configuration: API key, base URL, and transport timeout."""

    model_config = ConfigDict(frozen=True)

    api_key: str
    base_url: str
    timeout: float


class TaskContext(BaseModel):
    """Ambient identity/intent set once via `task()`, inherited by calls inside it."""

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    intent: str | None = None
    project: str | None = None


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize` — the frozen P1 wire contract.

    `amount` is `Decimal`, never `float`: this is a ledger, and float
    rounding error compounds across authorize -> capture -> journal ->
    reconciliation.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    reason: str
    intent: str | None = None
    project: str | None = None
    model: str | None = None
    tokens: int | None = None


class Verdict(BaseModel):
    """Policy verdict from `POST /transactions/authorize`, before any spend happens.

    `code` is a machine-readable reason (`ok`, `budget_exhausted`,
    `merchant_blocked`, `velocity_exceeded`, `amount_over_threshold`,
    `outside_hours`). It drives `strict=True` error mapping in `ledger.py`:
    `budget_exhausted` -> `BudgetExhausted`, any other blocking code ->
    `PolicyBlocked`. An open string set, so a new backend code never breaks this.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    status: Literal["approved", "blocked", "review", "human_hold"]
    code: str = "ok"
    reason: str | None = None

    @property
    def approved(self) -> bool:
        """True only when `status == "approved"`; every other status blocks it."""
        return self.status == "approved"


class BudgetStatus(BaseModel):
    """An agent's live budget utilisation, from `check_budget()` (Phase 3).

    Mirrors the backend `AgentBudget`. `status` is `ok` | `warning` |
    `exhausted`; every figure is derived server-side from live spend.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    monthly_budget: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str

    @property
    def exhausted(self) -> bool:
        """True when the agent has no budget headroom left."""
        return self.status == "exhausted"


class JournalLine(BaseModel):
    """One double-entry journal line booked for a captured spend (Phase 2).

    Mirrors the backend `JournalLine`; part of the frozen wire contract.
    """

    model_config = ConfigDict(frozen=True)

    account_name: str
    account_code: str
    debit: Decimal
    credit: Decimal
    ledger: str


class TaxInfo(BaseModel):
    """The jurisdiction tax treatment computed for a captured spend (Phase 2).

    Mirrors the backend `TaxInfo`; part of the frozen wire contract. For a
    reverse-charge spend `reverse_charge` and `self_invoice_generated` are True.
    """

    model_config = ConfigDict(frozen=True)

    tax_type: str
    jurisdiction: str
    rate: Decimal
    tax_amount: Decimal
    is_reclaimable: bool
    reclaim_amount: Decimal
    legal_reference: str
    reverse_charge: bool
    self_invoice_generated: bool


class Transaction(BaseModel):
    """A committed spend, returned by `POST /transactions/capture`.

    Phase 2 adds the `journal` (double-entry lines) and `tax` (treatment) the
    backend books synchronously on capture.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    authorization_id: str
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    journal: list[JournalLine] = Field(default_factory=list)
    tax: TaxInfo | None = None

    @property
    def approved(self) -> bool:
        """Always True: a Transaction only exists once capture has succeeded."""
        return True
