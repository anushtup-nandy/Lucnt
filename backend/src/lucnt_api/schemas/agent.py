"""Pydantic response schemas for the agents router."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AgentDetail(BaseModel):
    """Response body for `GET /agents` and `GET /agents/{id}`."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    entity_id: uuid.UUID
    name: str
    slug: str
    glyph: str
    role: str
    wallet_ref: str | None
    models_list: list[str]
    vendors_list: list[str]
    monthly_budget: Decimal
    spent_ytd: Decimal
    spent_today: Decimal
    status: str
    is_live: bool
    confidence_pct: int


class AgentListResponse(BaseModel):
    """Response body for `GET /agents`."""

    model_config = ConfigDict(frozen=True)

    items: list[AgentDetail]
    total: int
