"""Pydantic schemas for the transactions router.

`SpendRequest`/`Verdict`/`Transaction` mirror `sdk/src/lucnt/models.py`
field-for-field: this is the frozen wire contract (sdk/PLAN.md §1). Do not
add, rename, or drop a field here without updating the SDK models in lockstep.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize`. Mirrors `sdk.SpendRequest`."""

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
    """Response body for `POST /transactions/authorize`. Mirrors `sdk.Verdict`."""

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    status: Literal["approved", "blocked", "review", "human_hold"]
    reason: str | None = None


class CaptureRequest(BaseModel):
    """Request body for `POST /transactions/capture`. Mirrors `sdk.ledger.capture()`."""

    model_config = ConfigDict(frozen=True)

    authorization_id: uuid.UUID
    source: Literal["sdk", "card", "invoice"] = "sdk"
    invoice_url: str | None = None


class Transaction(BaseModel):
    """Response body for `POST /transactions/capture`. Mirrors `sdk.Transaction`."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    authorization_id: uuid.UUID | None
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None


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
    status: str
    reason_text: str | None
    failure_reason: str | None
    transaction_date: datetime


class TransactionListResponse(BaseModel):
    """Response body for `GET /transactions`."""

    model_config = ConfigDict(frozen=True)

    items: list[TransactionDetail]
    total: int
