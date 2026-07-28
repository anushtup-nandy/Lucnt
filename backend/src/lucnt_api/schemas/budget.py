"""Pydantic schemas for the budgets router (Phase 3, M6).

`spent`/`remaining`/`pct_used`/`status` are derived at read time from live agent
spend, never stored — so a budget view can never drift from the ledger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Upper bound on a settable monthly budget (T0-15): a sane business ceiling so a
# fat-fingered or malicious PATCH can't set an effectively-infinite cap that lets
# every spend through. Generous (100M per currency unit); raise if a real entity
# needs more.
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
    # F-10: what the authorize gate will actually grant. `remaining` ignores
    # outstanding holds; the gate does not — `reserved` is the live-hold total
    # measured with the gate's own predicate, and `available` (= remaining −
    # reserved, may go negative like `remaining`) is the number an agent should
    # self-regulate on.
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
    """Response body for `GET /budgets` — per-agent + per-workflow rollups.

    `total` counts every agent matching the request's filters, not the length of
    the returned `agents` page (T3-3). `workflows` is unpaginated and rolls up
    *all* member agents, so a rollup stays correct regardless of the page.
    """

    model_config = ConfigDict(frozen=True)

    agents: list[AgentBudget]
    workflows: list[WorkflowBudget]
    total: int = 0


class BudgetUpdate(BaseModel):
    """Request body for `PATCH /budgets/{agent_id}`."""

    model_config = ConfigDict(frozen=True)

    monthly_budget: Decimal = Field(ge=0, le=MAX_MONTHLY_BUDGET)
