"""Typed data structures shared across the lucnt SDK."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# No `float`: `Decimal(1.83) != Decimal("1.83")`. `bool` is rejected at the
# boundary despite subclassing `int`.
Money = Decimal | int | str


class LucntConfig(BaseModel):
    """Resolved SDK configuration: credentials, endpoint, timeouts, retry policy.

    Safety controls (e.g. the TLS host allowlist) are deliberately not fields.
    """

    model_config = ConfigDict(frozen=True)

    # SecretStr keeps the key out of reprs, logs, and tracebacks.
    api_key: SecretStr
    base_url: str
    timeout: float
    connect_timeout: float | None = None
    read_timeout: float | None = None
    # Counts retries, not attempts: 2 means up to 3 requests.
    max_retries: int = Field(default=2, ge=0)
    backoff_base: float = Field(default=0.25, gt=0)
    backoff_max: float = Field(default=8.0, gt=0)
    respect_retry_after: bool = True
    pricing_feed: bool = True
    pricing_feed_ttl: float = Field(default=3600.0, ge=0)


class TaskContext(BaseModel):
    """Ambient identity/intent set once via `task()`, inherited by calls inside it."""

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    intent: str | None = None
    project: str | None = None


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize` (the wire contract)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    vendor: str
    amount: Decimal
    # Never defaulted, so a foreign spend is never mislabelled as domestic.
    currency: str
    reason: str
    intent: str | None = None
    project: str | None = None
    # software|ai_compute|travel|marketing|general; else vendor-inferred.
    category: str | None = None
    # GST rate slab (e.g. "gst_5"); omitted -> standard rate.
    tax_category: str | None = None
    tax_inclusive: bool = True
    # Cross-border goods route to customs, not the services reverse charge.
    supply_type: Literal["service", "goods"] = "service"
    model: str | None = None
    tokens: int | None = None


class Verdict(BaseModel):
    """Policy verdict from `authorize()`, before any spend happens.

    `code` is an open string set (`ok`, `budget_exhausted`, `merchant_blocked`,
    `velocity_exceeded`, `amount_over_threshold`, `outside_hours`, ...).
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


class VoidResult(BaseModel):
    """Result of releasing a hold via `void()`.

    `already_released` is True on a repeat void, which is a safe no-op.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    released_at: datetime
    release_reason: str | None = None
    already_released: bool = False


class BudgetStatus(BaseModel):
    """An agent's live budget utilisation, from `check_budget()`.

    Self-regulate on `available` (`remaining - reserved` holds), not
    `remaining`: it is what the authorize gate will actually grant.
    `status` is `ok` | `warning` | `exhausted`.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    monthly_budget: Decimal
    spent: Decimal
    remaining: Decimal
    reserved: Decimal
    available: Decimal
    pct_used: Decimal
    status: str

    @property
    def exhausted(self) -> bool:
        """True when the agent has no budget headroom left."""
        return self.status == "exhausted"


class JournalLine(BaseModel):
    """One double-entry journal line booked for a captured spend."""

    model_config = ConfigDict(frozen=True)

    account_name: str
    account_code: str
    debit: Decimal
    credit: Decimal
    ledger: str
    currency: str  # the functional (entity) currency this line is booked in


class TaxInfo(BaseModel):
    """The jurisdiction tax treatment computed for a captured spend."""

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
    """A committed spend with its journal lines and tax treatment, from `capture()`."""

    model_config = ConfigDict(frozen=True)

    id: str
    authorization_id: str
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    # True when `amount` is the authorize-time estimate, not settled usage.
    estimated: bool = False
    invoice_url: str | None = None
    journal: list[JournalLine] = Field(default_factory=list)
    tax: TaxInfo | None = None

    @property
    def approved(self) -> bool:
        """Always True: a Transaction only exists once capture has succeeded."""
        return True


class BudgetSummary(BaseModel):
    """Live budget headroom inside a `FinancialMemory`."""

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str  # ok | warning | exhausted


class SpendProjection(BaseModel):
    """A month-end spend projection from the current run-rate."""

    model_config = ConfigDict(frozen=True)

    month_to_date: Decimal
    projected_month_end: Decimal
    monthly_budget: Decimal
    on_track: bool
    projected_overrun: Decimal
    day_of_month: int
    days_in_month: int


class CostPerOutput(BaseModel):
    """Cost per unit of work (a project) for an agent, in one currency."""

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
    """One vendor's share of an agent's transaction count."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    count: int
    share: Decimal


class SpendFingerprint(BaseModel):
    """A compact view of an agent's learned spend fingerprint.

    Amount stats are None until there is enough same-currency history.
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
    """An agent's living financial memory, from `query()`.

    `headline` answers the optional `question` (keyword-routed; no LLM).
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
    """A reconciliation exception, returned by `mark_failed_delivery()`."""

    model_config = ConfigDict(frozen=True)

    id: str
    transaction_id: str
    exception_type: str
    status: str
    amount_at_stake: Decimal
    evidence: dict[str, Any] = Field(default_factory=dict)
    fix_notes: str | None = None
