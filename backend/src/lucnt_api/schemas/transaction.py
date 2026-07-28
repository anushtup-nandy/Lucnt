"""Pydantic schemas for the transactions router.

`SpendRequest`/`Verdict`/`Transaction` mirror `sdk/src/lucnt/models.py`
field-for-field: this is the frozen wire contract (sdk/PLAN.md §1). Do not
add, rename, or drop a field here without updating the SDK models in lockstep.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lucnt_api.constants import (
    AGENT_SLUG_MAX,
    CATEGORY_MAX,
    ENTITY_CODE_MAX,
    INTENT_MAX,
    MODEL_NAME_MAX,
    PROJECT_MAX,
    REASON_MAX,
    TAX_CATEGORY_MAX,
    URL_MAX,
    VENDOR_MAX,
)

# ISO-4217 alpha codes are three uppercase letters. Membership in the *supported*
# set is enforced downstream by the fail-closed FX table (a valid-format but
# unrated currency is rejected at authorize/capture); the schema only guarantees
# the shape and normalises case.
_ISO_4217_RE = re.compile(r"[A-Z]{3}")


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize`. Mirrors `sdk.SpendRequest`."""

    model_config = ConfigDict(frozen=True)

    # Every `max_length` below mirrors the column the value lands in (T3-8).
    # Without them an oversized string passes validation and fails in Postgres as
    # a 500 `DataError`; with them the caller gets a 422 naming the field.
    agent: str = Field(min_length=1, max_length=AGENT_SLUG_MAX)
    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    vendor: str = Field(min_length=1, max_length=VENDOR_MAX)
    # Money must be strictly positive: a zero/negative authorize would *reduce*
    # projected spend and always approve, and a negative capture would decrement
    # spent_ytd (a budget-refund / theft primitive). See PRODUCTION_PLAN T0-4.
    amount: Decimal = Field(gt=0)
    # Mandatory (T1-5): the caller always states the currency it paid in — the one
    # fact only it knows. Never silently defaulted, so a foreign spend can never be
    # mislabelled as domestic. Validated to an ISO-4217 shape and upper-cased.
    currency: str
    # `reason` is a TEXT column, so this cap is payload policy, not a column
    # mirror — it is echoed into `agent.spend.created`.
    reason: str = Field(min_length=1, max_length=REASON_MAX)
    intent: str | None = Field(default=None, max_length=INTENT_MAX)
    project: str | None = Field(default=None, max_length=PROJECT_MAX)
    # Optional expense category (T1-19): a known category
    # (software|ai_compute|travel|marketing|general) books to that account and
    # drives blocked-ITC (T1-14); an unknown value falls back to vendor inference.
    category: str | None = Field(default=None, max_length=CATEGORY_MAX)
    # Tax slab + inclusive/exclusive (T1-10). `tax_category` picks the GST rate slab
    # (e.g. "gst_5"|"gst_12"|"gst_28"; omitted ⇒ "standard"); `tax_inclusive` says
    # whether a domestic amount embeds GST (default) or has it added on top.
    tax_category: str | None = Field(default=None, max_length=TAX_CATEGORY_MAX)
    tax_inclusive: bool = True
    # Whether the purchase is a service or goods (TE-4). A cross-border *goods*
    # spend routes to a customs path, never the services reverse charge; defaults to
    # ``service`` (agent spend is ~all services), so existing callers are unaffected.
    supply_type: Literal["service", "goods"] = "service"
    model: str | None = Field(default=None, max_length=MODEL_NAME_MAX)
    tokens: int | None = Field(default=None, ge=0)

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str) -> str:
        """Upper-case and validate the ISO-4217 shape (three letters)."""
        code = value.strip().upper()
        if not _ISO_4217_RE.fullmatch(code):
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        return code

    @field_validator("tax_category")
    @classmethod
    def _normalise_tax_category(cls, value: str | None) -> str | None:
        """Lower-case/trim the slab key; membership is enforced by the rate table."""
        return value.strip().lower() if value else None


class Verdict(BaseModel):
    """Response body for `POST /transactions/authorize`. Mirrors `sdk.Verdict`.

    Phase 3 adds `code`, a machine-readable reason (`ok`, `budget_exhausted`,
    `merchant_blocked`, `velocity_exceeded`, `amount_over_threshold`,
    `outside_hours`) the SDK maps to typed exceptions under `strict=True`. It is
    an open string set (each future rule adds a code), not a `Literal`, so a new
    code never breaks an older SDK.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    status: Literal["approved", "blocked", "review", "human_hold"]
    code: str = "ok"
    reason: str | None = None


class CaptureRequest(BaseModel):
    """Request body for `POST /transactions/capture`. Mirrors `sdk.ledger.capture()`.

    Phase 3.5 adds optional settlement overrides (`amount`/`model`/`tokens`):
    the actual values known only after the spend (e.g. the true LLM cost from
    `meter()`). When omitted, the authorization's held values stand. This is the
    hold-vs-settle split — `authorize` holds an estimate, `capture` books truth
    — and the same fields serve the future card rail (auth $X, settle $Y).
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    source: Literal["sdk", "card", "invoice"] = "sdk"
    # Evidence link persisted on the transaction (F-12) and readable back via
    # GET /transactions/{id}; the M3 invoice hook is its structured consumer.
    invoice_url: str | None = Field(default=None, max_length=URL_MAX)
    # A settlement override, when present, must be strictly positive (T0-4); its
    # upper bound versus the authorized amount is enforced in the router (T0-3).
    amount: Decimal | None = Field(default=None, gt=0)
    model: str | None = Field(default=None, max_length=MODEL_NAME_MAX)
    tokens: int | None = Field(default=None, ge=0)
    # F-8: True when the booked amount is the authorize-time estimate rather
    # than settled actual usage — the SDK's T5-1 fallback sets this so the
    # ledger can distinguish an estimate from truth instead of an ERROR log.
    estimated: bool = False


class JournalLine(BaseModel):
    """One double-entry journal line returned with a captured transaction.

    Mirrors `sdk.JournalLine`. Part of the frozen contract — keep in lockstep.
    """

    model_config = ConfigDict(frozen=True)

    account_name: str
    account_code: str
    debit: Decimal
    credit: Decimal
    ledger: str
    currency: str


class TaxInfo(BaseModel):
    """The tax treatment returned with a captured transaction.

    Mirrors `sdk.TaxInfo`. Part of the frozen contract — keep in lockstep.
    """

    model_config = ConfigDict(frozen=True)

    tax_type: str
    jurisdiction: str
    rate: Decimal
    tax_amount: Decimal
    is_reclaimable: bool
    reclaim_amount: Decimal
    currency: str
    legal_reference: str
    reverse_charge: bool
    self_invoice_generated: bool


class Transaction(BaseModel):
    """Response body for `POST /transactions/capture`. Mirrors `sdk.Transaction`.

    Phase 2 adds `journal` (double-entry lines) and `tax` (jurisdiction
    treatment), both produced synchronously on capture.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    authorization_id: uuid.UUID | None
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    # F-8: echoes the request flag so the caller can see what the ledger booked.
    estimated: bool = False
    # F-12: echoes the persisted evidence link (readable back via GET too).
    invoice_url: str | None = None
    journal: list[JournalLine] = Field(default_factory=list)
    tax: TaxInfo | None = None


class TransactionDetail(BaseModel):
    """Full detail for `GET /transactions/{id}` — beyond the frozen SDK contract."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    entity_id: uuid.UUID
    authorization_id: uuid.UUID | None
    source: str
    vendor: str
    item_description: str | None
    paid_amount: Decimal
    paid_currency: str
    local_amount: Decimal
    local_currency: str
    project: str | None
    intent: str | None
    model_used: str | None
    tokens_consumed: int | None
    # F-8: the booked amount is an unsettled estimate (see `CaptureRequest`).
    estimated: bool
    # F-12: the evidence link supplied at capture, if any.
    invoice_url: str | None
    status: str
    reason_text: str | None
    failure_reason: str | None
    transaction_date: datetime


class TransactionListResponse(BaseModel):
    """Response body for `GET /transactions`."""

    model_config = ConfigDict(frozen=True)

    items: list[TransactionDetail]
    total: int


class VoidRequest(BaseModel):
    """Request body for `POST /transactions/void`. Mirrors `sdk.ledger.void()`.

    Releases an approved-but-uncaptured hold so it stops reserving the agent's
    budget (T5-4). Used when the spend the hold was taken for did not happen —
    the vendor call failed after `authorize()` returned, say.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    # Free-text explanation stored on the hold, for the recovery inbox (M7).
    reason: str | None = Field(default=None, max_length=REASON_MAX)


class VoidResult(BaseModel):
    """Result of releasing a hold via `POST /transactions/void`.

    `already_released` makes the endpoint idempotent: a retried void (the SDK
    calls it from an error path, so retries are expected) returns 200 describing
    the original release rather than conflicting with it.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    released_at: datetime
    release_reason: str | None = None
    already_released: bool = False
