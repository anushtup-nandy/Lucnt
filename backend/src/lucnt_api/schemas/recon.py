"""Reconciliation schemas. `evidence` is heterogeneous per exception type."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lucnt_api.constants import NOTES_MAX


class ReconExceptionResponse(BaseModel):
    """One reconciliation exception in the recovery inbox."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    transaction_id: uuid.UUID
    exception_type: str
    status: str
    evidence: dict[str, Any]
    amount_at_stake: Decimal
    currency: str | None
    fix_notes: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReconListResponse(BaseModel):
    """`GET /recon` — inbox page plus a filter-independent summary.

    `total_at_stake` covers all active exceptions, keyed per currency.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ReconExceptionResponse]
    total: int
    total_at_stake: dict[str, Decimal]
    open_count: int


class ReconResolveRequest(BaseModel):
    """Request body for `POST /recon/{id}/resolve` — move to a terminal state."""

    model_config = ConfigDict(frozen=True)

    action: Literal["resolve", "dismiss"]
    # TEXT column: this cap is the only size bound.
    fix_notes: str | None = Field(default=None, max_length=NOTES_MAX)


class FailedDeliveryRequest(BaseModel):
    """`POST /recon/failed-delivery`: a spend whose downstream task failed.

    `amount_at_stake` defaults to the transaction's paid amount.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)
    # Router also caps it at the transaction's amount.
    amount_at_stake: Decimal | None = Field(default=None, ge=0)


class ScanResponse(BaseModel):
    """Response for `POST /recon/scan` — a reconciliation pass summary."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    created: int
    by_type: dict[str, int]
