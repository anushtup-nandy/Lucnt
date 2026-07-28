"""Budgets router — read the live spend rollups, adjust an agent's cap (M6).

Spend is derived from the ledger for the **current month** in the entity's local
timezone and functional currency (`services/spend_window.py`), so the rollups reset
at the period boundary and match the invariant `authorize()` enforces — never the
never-reset `agents.spent_ytd` counter, which mis-reads inception-to-date spend
against a *monthly* budget (PRODUCTION_PLAN T2-3). Workflow spend sums its member
agents' current-month spend.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_current_company,
    get_db,
    require_admin,
)
from lucnt_api.models import Agent, Company, Entity, Workflow
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.budget import (
    AgentBudget,
    BudgetsResponse,
    BudgetUpdate,
    WorkflowBudget,
)
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.spend_window import (
    company_period_committed_spend,
    company_reserved_spend,
    period_committed_spend,
    reserved_spend,
)

router = APIRouter(prefix="/budgets", tags=["budgets"])

_WARNING_PCT = Decimal("80")  # utilisation at/above which a budget reads "warning"
_ONE_PLACE = Decimal("0.1")
SLUG_MAX_LENGTH = 100  # mirrors models.Agent.slug — String(100)

# An agent absent from the batched spend map simply has no spend this month.
_NO_SPEND = (Decimal("0"), Decimal("0"))


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


def _agent_budget(
    agent: Agent, spent: Decimal, spent_today: Decimal, reserved: Decimal
) -> AgentBudget:
    """Build the budget view for a single agent from its current-month spend.

    `reserved`/`available` mirror the authorize gate's arithmetic (F-10): the
    gate subtracts live holds from headroom, so the read reports them too —
    otherwise an agent self-regulating on this read sees room the gate refuses.
    """
    remaining, pct_used, status_label = _rollup(spent, agent.monthly_budget)
    return AgentBudget(
        id=agent.id,
        name=agent.name,
        slug=agent.slug,
        entity_id=agent.entity_id,
        monthly_budget=agent.monthly_budget,
        spent=spent,
        spent_today=spent_today,
        remaining=remaining,
        reserved=reserved,
        available=remaining - reserved,
        pct_used=pct_used,
        status=status_label,
    )


@router.get("", response_model=BudgetsResponse)
def get_budgets(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
    page: Page = Depends(paginate()),
    agent: str | None = Query(
        default=None, max_length=SLUG_MAX_LENGTH, description="Filter to one slug"
    ),
) -> BudgetsResponse:
    """Per-agent budget utilisation plus per-workflow rollups for the company.

    The `agents` array is paginated and `total` counts all matching agents; the
    `workflows` rollups are configuration-shaped (bounded by how many workflows
    a customer authors, not by ledger volume) and are always returned whole, so
    a rollup never silently omits a member that fell off the page.

    `agent` narrows to a single slug — the SDK's `check_budget()` path, which
    would otherwise download every agent's budget to read one row.
    """
    stmt = select(Agent).where(Agent.company_id == company.id)
    if agent is not None:
        stmt = stmt.where(Agent.slug == agent)
    total = count_total(db, stmt)
    agents = (
        db.execute(stmt.order_by(Agent.name).offset(page.offset).limit(page.limit))
        .scalars()
        .all()
    )
    workflows = (
        db.execute(
            select(Workflow)
            .where(Workflow.company_id == company.id)
            # Eager-load members: the rollup below reads `workflow.agents` for
            # every workflow, which lazy-loading turns into a query per row (T3-3).
            .options(selectinload(Workflow.agents))
            .order_by(Workflow.name)
        )
        .scalars()
        .all()
    )
    entities = (
        db.execute(select(Entity).where(Entity.company_id == company.id))
        .scalars()
        .all()
    )

    # Current-month committed spend for the whole company in one aggregate per
    # entity, entity-local period + functional currency (T2-3). Batched because
    # the per-agent form cost two queries each (T3-3), and because the workflow
    # rollups below need members that the current page may not contain.
    month_spend = company_period_committed_spend(db, company.id, entities)
    # Live holds per agent, measured with the gate's own predicate (F-10).
    reserved = company_reserved_spend(db, company.id)

    workflow_rollups: list[WorkflowBudget] = []
    for workflow in workflows:
        members = workflow.agents
        spent = sum(
            (month_spend.get(m.id, _NO_SPEND)[0] for m in members), Decimal("0")
        )
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
        agents=[
            _agent_budget(
                a, *month_spend.get(a.id, _NO_SPEND), reserved.get(a.id, Decimal("0"))
            )
            for a in agents
        ],
        workflows=workflow_rollups,
        total=total,
    )


@router.patch("/{agent_id}", response_model=AgentBudget)
def update_agent_budget(
    agent_id: uuid.UUID,
    payload: BudgetUpdate,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> AgentBudget:
    """Adjust an agent's monthly budget cap (company-scoped)."""
    agent = db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.company_id == company.id)
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown agent")

    old_budget = agent.monthly_budget
    agent.monthly_budget = payload.monthly_budget
    record_audit(
        db,
        ctx.api_key,
        action="budget.update",
        entity_type="agent",
        entity_id=agent.id,
        before={"monthly_budget": str(old_budget)},
        after={"monthly_budget": str(payload.monthly_budget)},
    )
    db.commit()
    db.refresh(agent)
    entity = db.get(Entity, agent.entity_id)
    if entity is None:  # FK guarantees it; guard for the type checker
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent entity not found")
    spent, spent_today = period_committed_spend(db, agent.id, entity)
    return _agent_budget(agent, spent, spent_today, reserved_spend(db, agent.id))
