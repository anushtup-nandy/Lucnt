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

from pydantic import BaseModel, ConfigDict


class ReconExceptionResponse(BaseModel):
    """One reconciliation exception in the recovery inbox."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    transaction_id: uuid.UUID
    exception_type: str
    status: str
    evidence: dict[str, Any]
    amount_at_stake: Decimal
    fix_notes: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReconListResponse(BaseModel):
    """Response for `GET /recon` — the inbox page plus a stable summary header.

    `total` is the filtered/paginated count; `total_at_stake` and `open_count`
    summarize all *active* (open + recovering) exceptions for the company,
    independent of the list filters — the "$ at stake" headline.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ReconExceptionResponse]
    total: int
    total_at_stake: Decimal
    open_count: int


class ReconResolveRequest(BaseModel):
    """Request body for `POST /recon/{id}/resolve` — move to a terminal state."""

    model_config = ConfigDict(frozen=True)

    action: Literal["resolve", "dismiss"]
    fix_notes: str | None = None


class FailedDeliveryRequest(BaseModel):
    """Request body for `POST /recon/failed-delivery` (the SDK `mark_failed_delivery`).

    Agent-reported: a spend whose downstream task failed. `amount_at_stake`
    defaults to the transaction's full paid amount when not given.
    """

    model_config = ConfigDict(frozen=True)

    transaction_id: uuid.UUID
    reason: str
    amount_at_stake: Decimal | None = None


class ScanResponse(BaseModel):
    """Response for `POST /recon/scan` — a reconciliation pass summary."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    created: int
    by_type: dict[str, int]
