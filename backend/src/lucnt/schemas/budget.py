"""Pydantic schemas for the budgets router (Phase 3, M6).

`spent`/`remaining`/`pct_used`/`status` are derived at read time from live agent
spend, never stored — so a budget view can never drift from the ledger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class AgentBudget(BaseModel):
    """One agent's budget utilisation."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    slug: str
    entity_id: uuid.UUID
    monthly_budget: Decimal
    spent: Decimal
    spent_today: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str  # ok | warning | exhausted


class WorkflowBudget(BaseModel):
    """A workflow's shared budget, spend rolled up across its member agents."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    budget: Decimal
    spent: Decimal
    remaining: Decimal
    pct_used: Decimal
    status: str
    agent_ids: list[uuid.UUID]


class BudgetsResponse(BaseModel):
    """Response body for `GET /budgets` — per-agent + per-workflow rollups."""

    model_config = ConfigDict(frozen=True)

    agents: list[AgentBudget]
    workflows: list[WorkflowBudget]


class BudgetUpdate(BaseModel):
    """Request body for `PATCH /budgets/{agent_id}`."""

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal = Field(ge=0)
