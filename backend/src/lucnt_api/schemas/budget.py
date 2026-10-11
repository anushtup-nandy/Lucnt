"""Budgets schemas. Utilisation is derived at read time, never stored."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Ceiling so a PATCH can't set an effectively-infinite cap.
MAX_MONTHLY_BUDGET = Decimal("100000000")


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
    # `remaining` ignores live holds; `available` = remaining - reserved is what
    # the authorize gate grants (may go negative).
    reserved: Decimal
    available: Decimal
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
    """`GET /budgets` — per-agent page plus unpaginated workflow rollups.

    `total` counts all matching agents, not the page length.
    """

    model_config = ConfigDict(frozen=True)

    agents: list[AgentBudget]
    workflows: list[WorkflowBudget]
    total: int = 0


class BudgetUpdate(BaseModel):
    """Request body for `PATCH /budgets/{agent_id}`."""

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal = Field(ge=0, le=MAX_MONTHLY_BUDGET)
