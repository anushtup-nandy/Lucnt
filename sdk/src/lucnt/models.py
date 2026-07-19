"""Typed data structures shared across the lucnt SDK."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LucntConfig(BaseModel):
    """Resolved SDK configuration: API key, base URL, and transport timeout."""

    model_config = ConfigDict(frozen=True)

    # SecretStr so the key never lands in a repr/log/traceback (T0-19). Read the
    # plaintext with `api_key.get_secret_value()` only where it is actually sent.
    api_key: SecretStr
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
    # Mandatory (T1-5): the caller always states the currency it paid in — the one
    # fact only it knows. Never silently defaulted, so a foreign spend is never
    # mislabelled as domestic. The backend validates the ISO-4217 shape.
    currency: str
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
    currency: str  # the functional (entity) currency this line is booked in


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
    currency: str  # the functional currency tax_amount/reclaim_amount are in
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


class BudgetSummary(BaseModel):
    """Live budget headroom inside a `FinancialMemory` (Phase 5).

    Mirrors the query response's budget block. Distinct from `BudgetStatus`
    (which `check_budget()` returns and carries the agent slug).
    """

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str  # ok | warning | exhausted


class SpendProjection(BaseModel):
    """A month-end spend projection from the current run-rate (Phase 5)."""

    model_config = ConfigDict(frozen=True)

    month_to_date: Decimal
    projected_month_end: Decimal
    monthly_budget: Decimal
    on_track: bool
    projected_overrun: Decimal
    day_of_month: int
    days_in_month: int


class CostPerOutput(BaseModel):
    """Cost per unit of work (a project) for an agent, in one currency (Phase 5)."""

    model_config = ConfigDict(frozen=True)

    unit: str
    dimension: str
    currency: str
    total_spent: Decimal
    count: int
    avg_cost: Decimal
    total_tokens: int | None = None
    cost_per_1k_tokens: Decimal | None = None


class VendorShare(BaseModel):
    """One vendor's share of an agent's transaction count (Phase 5)."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    count: int
    share: Decimal


class SpendFingerprint(BaseModel):
    """A compact view of an agent's learned spend fingerprint (Phase 5, M8).

    Amount statistics are null until the agent has enough same-currency history
    to establish a baseline; `anomaly_score` is the largest recent deviation from
    that baseline (0 when not yet computable).
    """

    model_config = ConfigDict(frozen=True)

    home_currency: str | None
    total_records: int
    sample_size: int
    mean_amount: Decimal | None = None
    stddev_amount: Decimal | None = None
    median_amount: Decimal | None = None
    daily_velocity: Decimal
    anomaly_score: Decimal
    top_vendors: list[VendorShare] = Field(default_factory=list)


class FinancialMemory(BaseModel):
    """An agent's living financial memory, from `query()` (Phase 5, M8).

    Computed live from the ledger so an agent can self-regulate *before* it
    spends: budget headroom, a month-end run-rate projection, cost-per-output,
    and its spend fingerprint. `headline` is a plain-language answer to the
    optional `question` (keyword-routed server-side; no LLM).
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    currency: str
    headline: str
    budget: BudgetSummary
    projection: SpendProjection
    cost_per_output: list[CostPerOutput] = Field(default_factory=list)
    fingerprint: SpendFingerprint | None = None

    @property
    def on_track(self) -> bool:
        """True when the run-rate projects at/under budget for the month."""
        return self.projection.on_track


class ReconException(BaseModel):
    """A reconciliation exception, returned by `mark_failed_delivery()` (Phase 4).

    Mirrors the fields of the backend `ReconExceptionResponse` the SDK cares
    about (server-only timestamps are ignored). `evidence` carries the drafted
    recovery plan the recovery layer will act on.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    transaction_id: str
    exception_type: str
    status: str
    amount_at_stake: Decimal
    evidence: dict[str, Any] = Field(default_factory=dict)
    fix_notes: str | None = None
