"""Transactions schemas.

`SpendRequest`/`Verdict`/`Transaction` are the frozen wire contract: change
them only in lockstep with `sdk/src/lucnt/models.py`.
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

# Shape only; supported-currency membership is enforced by the fail-closed FX table.
_ISO_4217_RE = re.compile(r"[A-Z]{3}")


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize`. Mirrors `sdk.SpendRequest`."""

    model_config = ConfigDict(frozen=True)

    # `max_length`s mirror columns: a 422 instead of a Postgres 500.
    agent: str = Field(min_length=1, max_length=AGENT_SLUG_MAX)
    entity: str = Field(min_length=1, max_length=ENTITY_CODE_MAX)
    vendor: str = Field(min_length=1, max_length=VENDOR_MAX)
    # Strictly positive: a negative amount would bypass budgets / refund spend.
    amount: Decimal = Field(gt=0)
    # Never defaulted, so a foreign spend can't be mislabelled as domestic.
    currency: str
    # TEXT column; cap is payload policy (echoed into `agent.spend.created`).
    reason: str = Field(min_length=1, max_length=REASON_MAX)
    intent: str | None = Field(default=None, max_length=INTENT_MAX)
    project: str | None = Field(default=None, max_length=PROJECT_MAX)
    # Known category drives account + blocked-ITC; unknown falls back to vendor.
    category: str | None = Field(default=None, max_length=CATEGORY_MAX)
    # GST slab (e.g. "gst_5"; omitted => "standard"); inclusive = amount embeds GST.
    tax_category: str | None = Field(default=None, max_length=TAX_CATEGORY_MAX)
    tax_inclusive: bool = True
    # Cross-border goods go via customs, never the services reverse charge.
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

    `code` is an open string (not a `Literal`) so a new code never breaks an
    older SDK.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    status: Literal["approved", "blocked", "review", "human_hold"]
    code: str = "ok"
    reason: str | None = None


class CaptureRequest(BaseModel):
    """Request body for `POST /transactions/capture`. Mirrors `sdk.ledger.capture()`.

    `amount`/`model`/`tokens` override the held estimate with settled actuals.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    source: Literal["sdk", "card", "invoice"] = "sdk"
    invoice_url: str | None = Field(default=None, max_length=URL_MAX)
    # Upper bound vs the authorized amount is enforced in the router.
    amount: Decimal | None = Field(default=None, gt=0)
    model: str | None = Field(default=None, max_length=MODEL_NAME_MAX)
    tokens: int | None = Field(default=None, ge=0)
    # Booked amount is the authorize-time estimate, not settled usage.
    estimated: bool = False


class JournalLine(BaseModel):
    """A captured transaction's journal line. Mirrors `sdk.JournalLine`."""

    model_config = ConfigDict(frozen=True)

    account_name: str
    account_code: str
    debit: Decimal
    credit: Decimal
    ledger: str
    currency: str


class TaxInfo(BaseModel):
    """A captured transaction's tax treatment. Mirrors `sdk.TaxInfo`."""

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
    """Response body for `POST /transactions/capture`. Mirrors `sdk.Transaction`."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    authorization_id: uuid.UUID | None
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    estimated: bool = False
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
    estimated: bool
    invoice_url: str | None
    status: str
    # `status` is always `completed`; this is what marks a reversal.
    is_reversed: bool
    reason_text: str | None
    failure_reason: str | None
    transaction_date: datetime


class TransactionFull(TransactionDetail):
    """`GET /transactions/{id}` — detail plus tax and journal (N+1 on the list)."""

    tax: TaxInfo | None = None
    journal: list[JournalLine] = []


class TransactionListResponse(BaseModel):
    """Response body for `GET /transactions`."""

    model_config = ConfigDict(frozen=True)

    items: list[TransactionDetail]
    total: int


class VoidRequest(BaseModel):
    """Request body for `POST /transactions/void`. Mirrors `sdk.ledger.void()`.

    Releases an approved-but-uncaptured hold when the spend did not happen.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=REASON_MAX)


class VoidResult(BaseModel):
    """Result of `POST /transactions/void`.

    Idempotent: a retried void returns 200 with `already_released` set.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    released_at: datetime
    release_reason: str | None = None
    already_released: bool = False


class AuthorizationSummary(BaseModel):
    """One policy verdict from `GET /authorizations`; agent name/glyph inline."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    agent_glyph: str
    entity_id: uuid.UUID
    vendor: str
    amount: Decimal
    currency: str | None
    reason: str
    intent: str | None
    project: str | None
    status: Literal["approved", "blocked", "review", "human_hold"]
    # Open string, like `Verdict.code`.
    verdict_code: str | None
    reason_text: str | None
    created_at: datetime


class AuthorizationListResponse(BaseModel):
    """Response body for `GET /authorizations`."""

    model_config = ConfigDict(frozen=True)

    items: list[AuthorizationSummary]
    total: int
