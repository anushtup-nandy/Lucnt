"""Pydantic schemas for the reconciliation (recovery) router (Phase 4, M7).

Enum columns are surfaced as plain `str` (the router maps `.value`), mirroring
`schemas/policy.py`. `evidence` is the detector facts plus the drafted recovery
plan; it is heterogeneous per exception type (`Any` is justified, as in Policy).
"""

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
    """Response for `GET /recon` — the inbox page plus a stable summary header.

    `total` is the filtered/paginated count; `total_at_stake` and `open_count`
    summarize all *active* (open + recovering) exceptions for the company,
    independent of the list filters — the "$ at stake" headline. `total_at_stake`
    is a **per-currency** map (currency -> sum), never a meaningless cross-currency
    total (PRODUCTION_PLAN T2-2).
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
    # Bounded (T3-8): `fix_notes` lands in a TEXT column, so nothing downstream
    # rejects an oversized note — the cap is what stops one resolve inflating
    # storage and the audit record.
    fix_notes: str | None = Field(default=None, max_length=NOTES_MAX)


class FailedDeliveryRequest(BaseModel):
    """Request body for `POST /recon/failed-delivery` (the SDK `mark_failed_delivery`).

    Agent-reported: a spend whose downstream task failed. `amount_at_stake`
    defaults to the transaction's full paid amount when not given.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: uuid.UUID
    # Bounded free text (T2-14): an unbounded reason is an abuse/DoS vector.
    reason: str = Field(min_length=1, max_length=500)
    # Non-negative; the router additionally caps it at the transaction's amount.
    amount_at_stake: Decimal | None = Field(default=None, ge=0)


class ScanResponse(BaseModel):
    """Response for `POST /recon/scan` — a reconciliation pass summary."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    created: int
    by_type: dict[str, int]
