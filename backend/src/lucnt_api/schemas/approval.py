"""Schemas for the approval queue (M6 approver persona, PRODUCTION_PLAN T0-11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ApprovalItem(BaseModel):
    """One pending (review / human_hold) authorization awaiting an approver."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    entity_id: uuid.UUID
    vendor: str
    amount: Decimal
    currency: str | None
    status: str
    reason: str | None  # caller-supplied justification
    reason_text: str | None  # policy engine's explanation
    created_at: datetime


class ApprovalListResponse(BaseModel):
    """Response body for `GET /approvals`."""

    model_config = ConfigDict(frozen=True)

    items: list[ApprovalItem]
    total: int


class ApprovalActionResponse(BaseModel):
    """Response body for approve / reject — the hold's new verdict status."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    status: str
