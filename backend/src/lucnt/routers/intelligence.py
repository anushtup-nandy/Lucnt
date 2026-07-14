"""Intelligence router (Phase 5, M8) — recompute the CFO tables, serve live memory.

Two endpoints, both ledger-reading (they never touch the frozen `authorize→capture`
spine — `CLAUDE.local.md`, like the Phase 4 recon scan):

- `POST /intelligence/recompute` runs the pure intelligence services
  (`services/intelligence.py` + `services/insight_generator.py`) over the whole
  company ledger and **upserts** `fingerprints`/`memories` and regenerates
  `insights`. It powers the Insights and Memory dashboard pages.
- `GET /intelligence/query` computes an agent's living financial memory **live**
  from its ledger (budget headroom, run-rate projection, cost-per-output,
  fingerprint) so an agent can self-regulate *before* it spends. It is the SDK
  `query()` backing and needs no prior recompute.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lucnt.dependencies import get_current_company, get_db
from lucnt.models import (
    Agent,
    Company,
    Entity,
    Fingerprint,
    Insight,
    Memory,
    Transaction,
)
from lucnt.schemas.intelligence import (
    BudgetBlock,
    CostUnitBlock,
    FinancialMemoryResponse,
    FingerprintBlock,
    ProjectionBlock,
    RecomputeResponse,
    VendorShareBlock,
)
from lucnt.services.insight_generator import (
    AgentIntel,
    DraftInsight,
    DraftMemory,
    derive_memories,
    generate_insights,
)
from lucnt.services.intelligence import (
    CostUnit,
    FingerprintFeatures,
    SpendProjection,
    SpendRecord,
    compute_fingerprint,
    cost_per_output,
    project_spend,
)

router = APIRouter(prefix="/intelligence", tags=["intelligence"])

_WARNING_PCT = Decimal("80")  # mirrors budgets._rollup — utilisation warning line
_ONE_PLACE = Decimal("0.1")
_QUERY_COST_UNITS = 5  # top cost-per-output rows returned by query()


def _record(txn: Transaction) -> SpendRecord:
    """Project a committed transaction to the intelligence value object.

    Uses the entity-local figures so cross-agent comparisons stay currency-consistent.
    """
    return SpendRecord(
        agent_id=txn.agent_id,
        vendor=txn.vendor,
        amount=txn.local_amount,
        currency=txn.local_currency,
        project=txn.project,
        model_used=txn.model_used,
        tokens=txn.tokens_consumed,
        transaction_date=txn.transaction_date,
    )


def _month_to_date(
    records: list[SpendRecord], now: datetime, home_currency: str | None
) -> Decimal:
    """Sum this calendar month's home-currency spend (never mix currencies)."""
    if home_currency is None:
        return Decimal("0")
    month_start = datetime(now.year, now.month, 1, tzinfo=now.tzinfo)
    return sum(
        (
            r.amount
            for r in records
            if r.currency == home_currency and r.transaction_date >= month_start
        ),
        Decimal("0"),
    )


def _budget_block(agent: Agent) -> BudgetBlock:
    """Live budget headroom — mirrors budgets._rollup (spent = spent_ytd)."""
    budget = agent.monthly_budget
    spent = agent.spent_ytd
    remaining = budget - spent
    pct_used = (
        (spent / budget * 100).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)
        if budget > 0
        else Decimal("0")
    )
    if remaining <= 0 and (spent > 0 or budget > 0):
        label = "exhausted"
    elif pct_used >= _WARNING_PCT:
        label = "warning"
    else:
        label = "ok"
    return BudgetBlock(
        monthly_budget=budget,
        spent=spent,
        remaining=remaining,
        pct_used=pct_used,
        status=label,
    )


def _build_intel(
    agent: Agent,
    records: list[SpendRecord],
    now: datetime,
    fallback_currency: str,
) -> tuple[AgentIntel, FingerprintFeatures, SpendProjection, list[CostUnit]]:
    """Compute the full intelligence bundle for one agent from its records."""
    features = compute_fingerprint(records)
    currency = features.home_currency or fallback_currency
    mtd = _month_to_date(records, now, features.home_currency)
    projection = project_spend(
        month_to_date=mtd, now=now, monthly_budget=agent.monthly_budget
    )
    cost_units = cost_per_output(records)
    intel = AgentIntel(
        agent_id=agent.id,
        agent_name=agent.name,
        agent_slug=agent.slug,
        currency=currency,
        projection=projection,
        features=features,
        cost_units=cost_units,
    )
    return intel, features, projection, cost_units


@router.post("/recompute", response_model=RecomputeResponse)
def recompute(
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> RecomputeResponse:
    """Recompute fingerprints/memories/insights over the whole company ledger.

    Upserts one fingerprint per agent and one memory per (agent, type); clears
    the company's **un-applied** insights and regenerates them (applied insights
    persist as a record of the action a human took). Idempotent — a second call
    with no new spend yields the same state.
    """
    now = datetime.now(timezone.utc)
    agents = {
        a.id: a
        for a in db.execute(
            select(Agent).where(Agent.company_id == company.id)
        ).scalars()
    }
    entity_currency = {
        e.id: e.currency
        for e in db.execute(
            select(Entity).where(Entity.company_id == company.id)
        ).scalars()
    }

    records_by_agent: dict[uuid.UUID, list[SpendRecord]] = {}
    txns = db.execute(
        select(Transaction).where(Transaction.company_id == company.id)
    ).scalars()
    for txn in txns:
        records_by_agent.setdefault(txn.agent_id, []).append(_record(txn))

    intels: list[AgentIntel] = []
    for agent_id, records in records_by_agent.items():
        agent = agents.get(agent_id)
        if agent is None:  # defensive: FK guarantees it exists
            continue
        fallback = entity_currency.get(agent.entity_id, "USD")
        intel, features, _proj, _units = _build_intel(agent, records, now, fallback)
        intels.append(intel)
        _upsert_fingerprint(db, company, agent, features, now)

    memory_count = _upsert_memories(db, company, derive_memories(intels))
    insight_drafts = generate_insights(intels)
    _regenerate_insights(db, company, insight_drafts)

    db.commit()

    by_type: dict[str, int] = {}
    for draft in insight_drafts:
        key = draft.insight_type.value
        by_type[key] = by_type.get(key, 0) + 1
    return RecomputeResponse(
        agents_analyzed=len(intels),
        fingerprints=len(intels),
        memories=memory_count,
        insights=len(insight_drafts),
        by_insight_type=by_type,
    )


def _upsert_fingerprint(
    db: Session,
    company: Company,
    agent: Agent,
    features: FingerprintFeatures,
    now: datetime,
) -> None:
    """Insert or refresh the single fingerprint row for an agent."""
    existing = db.execute(
        select(Fingerprint).where(Fingerprint.agent_id == agent.id)
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            Fingerprint(
                company_id=company.id,
                agent_id=agent.id,
                feature_vector=features.as_feature_vector(),
                anomaly_score=features.anomaly_score,
                last_updated=now,
            )
        )
    else:
        existing.feature_vector = features.as_feature_vector()
        existing.anomaly_score = features.anomaly_score
        existing.last_updated = now


def _upsert_memories(
    db: Session, company: Company, drafts: list[DraftMemory]
) -> int:
    """Insert or refresh each (agent, type) memory; returns the count written."""
    for draft in drafts:
        existing = db.execute(
            select(Memory).where(
                Memory.agent_id == draft.agent_id,
                Memory.memory_type == draft.memory_type,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                Memory(
                    company_id=company.id,
                    agent_id=draft.agent_id,
                    memory_type=draft.memory_type,
                    summary=draft.summary,
                    detail_text=draft.detail_text,
                    metrics_json=draft.metrics_json,
                )
            )
        else:
            existing.summary = draft.summary
            existing.detail_text = draft.detail_text
            existing.metrics_json = draft.metrics_json
    return len(drafts)


def _regenerate_insights(
    db: Session, company: Company, drafts: list[DraftInsight]
) -> None:
    """Clear the company's un-applied insights, then insert the fresh drafts."""
    db.execute(
        delete(Insight).where(
            Insight.company_id == company.id,
            Insight.is_applied.is_(False),
        )
    )
    for draft in drafts:
        db.add(
            Insight(
                company_id=company.id,
                agent_id=draft.agent_id,
                insight_type=draft.insight_type,
                title=draft.title,
                body=draft.body,
                value_text=draft.value_text,
                action_label=draft.action_label,
                action_payload=draft.action_payload,
            )
        )


@router.get("/query", response_model=FinancialMemoryResponse)
def query(
    agent: str = Query(description="Agent slug to read financial memory for"),
    q: str | None = Query(
        default=None, description="Optional free-text question, keyword-routed"
    ),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> FinancialMemoryResponse:
    """An agent's living financial memory, computed live from its ledger (M8)."""
    agent_row = db.execute(
        select(Agent).where(Agent.company_id == company.id, Agent.slug == agent)
    ).scalar_one_or_none()
    if agent_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {agent}")

    now = datetime.now(timezone.utc)
    records = [
        _record(txn)
        for txn in db.execute(
            select(Transaction).where(Transaction.agent_id == agent_row.id)
        ).scalars()
    ]
    entity = db.get(Entity, agent_row.entity_id)
    fallback_currency = entity.currency if entity is not None else "USD"

    intel, features, projection, cost_units = _build_intel(
        agent_row, records, now, fallback_currency
    )
    budget = _budget_block(agent_row)
    top_units = [_cost_unit_block(u) for u in cost_units[:_QUERY_COST_UNITS]]
    fingerprint = _fingerprint_block(features)
    headline = _headline(q, intel.currency, budget, projection, cost_units)

    return FinancialMemoryResponse(
        agent=agent_row.slug,
        currency=intel.currency,
        headline=headline,
        budget=budget,
        projection=_projection_block(projection),
        cost_per_output=top_units,
        fingerprint=fingerprint,
    )


def _cost_unit_block(unit: CostUnit) -> CostUnitBlock:
    return CostUnitBlock(
        unit=unit.unit,
        dimension=unit.dimension,
        currency=unit.currency,
        total_spent=unit.total_spent,
        count=unit.count,
        avg_cost=unit.avg_cost,
        total_tokens=unit.total_tokens,
        cost_per_1k_tokens=unit.cost_per_1k_tokens,
    )


def _projection_block(projection: SpendProjection) -> ProjectionBlock:
    return ProjectionBlock(
        month_to_date=projection.month_to_date,
        projected_month_end=projection.projected_month_end,
        monthly_budget=projection.monthly_budget,
        on_track=projection.on_track,
        projected_overrun=projection.projected_overrun,
        day_of_month=projection.day_of_month,
        days_in_month=projection.days_in_month,
    )


def _fingerprint_block(features: FingerprintFeatures) -> FingerprintBlock | None:
    """A compact fingerprint view; None until the agent has any spend."""
    if features.total_records == 0:
        return None
    stats = features.amount_stats
    return FingerprintBlock(
        home_currency=features.home_currency,
        total_records=features.total_records,
        sample_size=stats.sample_size if stats is not None else 0,
        mean_amount=stats.mean if stats is not None else None,
        stddev_amount=stats.stddev if stats is not None else None,
        median_amount=stats.median if stats is not None else None,
        daily_velocity=features.daily_velocity,
        anomaly_score=features.anomaly_score,
        top_vendors=[
            VendorShareBlock(vendor=v.vendor, count=v.count, share=v.share)
            for v in features.top_vendors
        ],
    )


def _headline(
    q: str | None,
    currency: str,
    budget: BudgetBlock,
    projection: SpendProjection,
    cost_units: list[CostUnit],
) -> str:
    """One-line answer to `q`, keyword-routed to the most relevant dimension.

    No LLM: a plain keyword match picks budget / cost / on-track (the default).
    """
    ql = (q or "").lower()
    if any(w in ql for w in ("cost", "output", "per unit", "expensive")):
        attributed = [u for u in cost_units if u.unit != "(unattributed)"]
        if attributed:
            u = attributed[0]
            return f"Priciest output: '{u.unit}' at {u.avg_cost} {u.currency}/unit."
        return "No project-attributed spend yet to cost out."
    if any(w in ql for w in ("budget", "remaining", "left", "headroom")):
        return (
            f"{budget.remaining} {currency} of {budget.monthly_budget} left this "
            f"cycle ({budget.pct_used}% used)."
        )
    # Default: the on-track / month-end projection (the CFO's headline question).
    if projection.on_track:
        return (
            f"On track: projecting {projection.projected_month_end} {currency} "
            f"against a {projection.monthly_budget} {currency} budget."
        )
    return (
        f"At risk: projecting {projection.projected_month_end} {currency}, "
        f"~{projection.projected_overrun} {currency} over budget at the current "
        f"run-rate."
    )
