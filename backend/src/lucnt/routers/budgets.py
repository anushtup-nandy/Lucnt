"""Budgets router — read the live spend rollups, adjust an agent's cap (M6).

Every figure is derived from `agents.spent_ytd`/`spent_today` (updated on each
capture in `routers/transactions.py`), so the view is always consistent with the
ledger. Workflow spend sums its member agents' spend.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import Agent, Company, Workflow
from lucnt.schemas.budget import (
    AgentBudget,
    BudgetsResponse,
    BudgetUpdate,
    WorkflowBudget,
)

router = APIRouter(prefix="/budgets", tags=["budgets"])

_WARNING_PCT = Decimal("80")  # utilisation at/above which a budget reads "warning"
_ONE_PLACE = Decimal("0.1")


def _rollup(spent: Decimal, budget: Decimal) -> tuple[Decimal, Decimal, str]:
    """Derive (remaining, pct_used, status) from spend against a budget."""
    remaining = budget - spent
    pct_used = (
        (spent / budget * 100).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)
        if budget > 0
        else Decimal("0")
    )
    if remaining <= 0 and (spent > 0 or budget > 0):
        status_label = "exhausted"
    elif pct_used >= _WARNING_PCT:
        status_label = "warning"
    else:
        status_label = "ok"
    return remaining, pct_used, status_label


def _agent_budget(agent: Agent) -> AgentBudget:
    """Build the budget view for a single agent."""
    remaining, pct_used, status_label = _rollup(agent.spent_ytd, agent.monthly_budget)
    return AgentBudget(
        id=agent.id,
        name=agent.name,
        slug=agent.slug,
        entity_id=agent.entity_id,
        monthly_budget=agent.monthly_budget,
        spent=agent.spent_ytd,
        spent_today=agent.spent_today,
        remaining=remaining,
        pct_used=pct_used,
        status=status_label,
    )


@router.get("", response_model=BudgetsResponse)
def get_budgets(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> BudgetsResponse:
    """Per-agent budget utilisation plus per-workflow rollups for the company."""
    agents = (
        db.execute(
            select(Agent).where(Agent.company_id == company.id).order_by(Agent.name)
        )
        .scalars()
        .all()
    )
    workflows = (
        db.execute(
            select(Workflow)
            .where(Workflow.company_id == company.id)
            .order_by(Workflow.name)
        )
        .scalars()
        .all()
    )

    workflow_rollups: list[WorkflowBudget] = []
    for workflow in workflows:
        members = workflow.agents  # lazy-loaded; fine at MVP scale
        spent = sum((m.spent_ytd for m in members), Decimal("0"))
        remaining, pct_used, status_label = _rollup(spent, workflow.budget)
        workflow_rollups.append(
            WorkflowBudget(
                id=workflow.id,
                name=workflow.name,
                budget=workflow.budget,
                spent=spent,
                remaining=remaining,
                pct_used=pct_used,
                status=status_label,
                agent_ids=[m.id for m in members],
            )
        )

    return BudgetsResponse(
        agents=[_agent_budget(a) for a in agents],
        workflows=workflow_rollups,
    )


@router.patch("/{agent_id}", response_model=AgentBudget)
def update_agent_budget(
    agent_id: uuid.UUID,
    payload: BudgetUpdate,
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> AgentBudget:
    """Adjust an agent's monthly budget cap (company-scoped)."""
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.company_id == company.id)
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown agent")

    agent.monthly_budget = payload.monthly_budget
    db.commit()
    db.refresh(agent)
    return _agent_budget(agent)
