"""Intelligence router: recompute fingerprints/memories/insights; serve `query()`."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.dependencies import (
    get_current_company,
    get_db,
    require_admin,
    require_cheap_enough,
)
from lucnt_api.models import (
    Agent,
    Company,
    Entity,
    Fingerprint,
    Insight,
    Memory,
    MemoryType,
    Transaction,
)
from lucnt_api.schemas.intelligence import (
    BudgetBlock,
    CostUnitBlock,
    FinancialMemoryResponse,
    FingerprintBlock,
    ProjectionBlock,
    RecomputeResponse,
    VendorShareBlock,
)
from lucnt_api.services.insight_generator import (
    AgentIntel,
    DraftInsight,
    DraftMemory,
    derive_memories,
    generate_insights,
)
from lucnt_api.services.intelligence import (
    # Shared with the SQL aggregate below so label and rounding cannot drift.
    _UNATTRIBUTED,
    CostUnit,
    FingerprintFeatures,
    SpendProjection,
    SpendRecord,
    _q_money,
    compute_fingerprint,
    cost_per_output,
    project_spend,
)
from lucnt_api.services.spend_window import now_in_entity_tz, period_committed_spend

router = APIRouter(prefix="/intelligence", tags=["intelligence"])

_WARNING_PCT = Decimal("80")  # mirrors budgets._rollup
_ONE_PLACE = Decimal("0.1")
_QUERY_COST_UNITS = 5  # top cost-per-output rows returned by query()
# Rows per round trip when `recompute` streams the ledger.
_LEDGER_CHUNK_SIZE = 1_000

# The column tuple `recompute` selects, in SELECT order.
type _SpendRow = tuple[
    uuid.UUID,  # agent_id
    str,  # vendor
    Decimal,  # local_amount
    str,  # local_currency
    str | None,  # project
    str | None,  # model_used
    int | None,  # tokens_consumed
    datetime,  # transaction_date
]


def _record(txn: Transaction) -> SpendRecord:
    """Project a transaction to a `SpendRecord`; twin of `_record_from_row`."""
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


def _record_from_row(row: _SpendRow) -> SpendRecord:
    """Column-tuple twin of `_record`, avoiding ORM hydration on a full scan."""
    agent_id, vendor, amount, currency, project, model_used, tokens, date = row
    return SpendRecord(
        agent_id=agent_id,
        vendor=vendor,
        amount=amount,
        currency=currency,
        project=project,
        model_used=model_used,
        tokens=tokens,
        transaction_date=date,
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


def _budget_block(db: Session, agent: Agent, entity: Entity) -> BudgetBlock:
    """Live current-month budget headroom from the ledger, not ``spent_ytd``."""
    budget = agent.monthly_budget
    spent, _today = period_committed_spend(db, agent.id, entity)
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
    """Compute the full intelligence bundle for one agent from its records.

    ``now`` must be entity-local: it sets month boundaries and velocity aging.
    """
    features = compute_fingerprint(records, home_currency=fallback_currency, now=now)
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


@router.post(
    "/recompute",
    response_model=RecomputeResponse,
    # See recon.scan: a full-ledger recompute behind the tight bucket.
    dependencies=[Depends(require_cheap_enough)],
)
def recompute(
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RecomputeResponse:
    """Recompute fingerprints, memories and insights over the company ledger.

    Idempotent; applied or dismissed insights are preserved.
    """
    now_utc = datetime.now(UTC)
    agents = {
        a.id: a
        for a in db.execute(
            select(Agent).where(Agent.company_id == company.id)
        ).scalars()
    }
    entities = {
        e.id: e
        for e in db.execute(
            select(Entity).where(Entity.company_id == company.id)
        ).scalars()
    }

    # Stream columns, not entities: ORM rows would pin the whole ledger in memory.
    records_by_agent: dict[uuid.UUID, list[SpendRecord]] = {}
    ledger = db.execute(
        select(
            Transaction.agent_id,
            Transaction.vendor,
            Transaction.local_amount,
            Transaction.local_currency,
            Transaction.project,
            Transaction.model_used,
            Transaction.tokens_consumed,
            Transaction.transaction_date,
        )
        .where(Transaction.company_id == company.id)
        .order_by(Transaction.agent_id)
        .execution_options(yield_per=_LEDGER_CHUNK_SIZE)
    ).tuples()
    for row in ledger:
        records_by_agent.setdefault(row[0], []).append(_record_from_row(row))

    fingerprints = _existing_fingerprints(db, records_by_agent.keys())

    intels: list[AgentIntel] = []
    for agent_id, records in records_by_agent.items():
        agent = agents.get(agent_id)
        if agent is None:  # defensive: FK guarantees it exists
            continue
        entity = entities.get(agent.entity_id)
        fallback = entity.currency if entity is not None else "USD"
        # Compute in entity-local time; stamp the row in UTC.
        now_local = now_in_entity_tz(entity) if entity is not None else now_utc
        intel, features, _proj, _units = _build_intel(
            agent, records, now_local, fallback
        )
        intels.append(intel)
        _upsert_fingerprint(db, company, agent, features, now_utc, fingerprints)

    memory_drafts = derive_memories(intels)
    memory_count = _upsert_memories(
        db, company, memory_drafts, _existing_memories(db, memory_drafts)
    )
    insight_drafts = generate_insights(intels)
    _sync_insights(db, company, insight_drafts)

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


def _existing_fingerprints(
    db: Session, agent_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Fingerprint]:
    """Existing fingerprints keyed by `agent_id` (unique), in one query."""
    if not agent_ids:
        return {}
    rows = db.execute(
        select(Fingerprint).where(Fingerprint.agent_id.in_(agent_ids))
    ).scalars()
    return {row.agent_id: row for row in rows}


def _upsert_fingerprint(
    db: Session,
    company: Company,
    agent: Agent,
    features: FingerprintFeatures,
    now: datetime,
    existing_by_agent: dict[uuid.UUID, Fingerprint],
) -> None:
    """Insert or refresh an agent's fingerprint row.

    The SAVEPOINT falls back to an update if a concurrent recompute inserted first.
    """
    existing = existing_by_agent.get(agent.id)
    if existing is None:
        fingerprint = Fingerprint(
            company_id=company.id,
            agent_id=agent.id,
            feature_vector=features.as_feature_vector(),
            anomaly_score=features.anomaly_score,
            last_updated=now,
        )
        try:
            with db.begin_nested():
                db.add(fingerprint)
                db.flush()
            return
        except IntegrityError:
            existing = db.execute(
                select(Fingerprint).where(Fingerprint.agent_id == agent.id)
            ).scalar_one()
    existing.feature_vector = features.as_feature_vector()
    existing.anomaly_score = features.anomaly_score
    existing.last_updated = now


def _existing_memories(
    db: Session, drafts: Sequence[DraftMemory]
) -> dict[tuple[uuid.UUID, MemoryType], Memory]:
    """Existing memories keyed by `(agent_id, memory_type)` (unique), in one query."""
    agent_ids = {draft.agent_id for draft in drafts}
    if not agent_ids:
        return {}
    rows = db.execute(select(Memory).where(Memory.agent_id.in_(agent_ids))).scalars()
    return {(row.agent_id, row.memory_type): row for row in rows}


def _upsert_memories(
    db: Session,
    company: Company,
    drafts: Sequence[DraftMemory],
    existing_by_key: dict[tuple[uuid.UUID, MemoryType], Memory],
) -> int:
    """Insert or refresh each (agent, type) memory; returns the count written.

    The per-row SAVEPOINT also covers two drafts sharing a key.
    """
    written = 0
    for draft in drafts:
        existing = existing_by_key.get((draft.agent_id, draft.memory_type))
        if existing is None:
            memory = Memory(
                company_id=company.id,
                agent_id=draft.agent_id,
                memory_type=draft.memory_type,
                summary=draft.summary,
                detail_text=draft.detail_text,
                metrics_json=draft.metrics_json,
            )
            try:
                with db.begin_nested():
                    db.add(memory)
                    db.flush()
                written += 1
                continue
            except IntegrityError:
                existing = db.execute(
                    select(Memory).where(
                        Memory.agent_id == draft.agent_id,
                        Memory.memory_type == draft.memory_type,
                    )
                ).scalar_one()
        existing.summary = draft.summary
        existing.detail_text = draft.detail_text
        existing.metrics_json = draft.metrics_json
        written += 1
    return written


def _sync_insights(db: Session, company: Company, drafts: list[DraftInsight]) -> None:
    """Upsert insights on `dedup_key`, preserving ids; delete stale active ones.

    A key a human already dismissed or applied is never regenerated.
    """
    existing = list(
        db.execute(select(Insight).where(Insight.company_id == company.id)).scalars()
    )
    by_key: dict[str, list[Insight]] = {}
    for row in existing:
        if row.dedup_key is not None:
            by_key.setdefault(row.dedup_key, []).append(row)

    draft_keys: set[str] = set()
    for draft in drafts:
        draft_keys.add(draft.dedup_key)
        rows = by_key.get(draft.dedup_key, [])
        if any(r.is_dismissed or r.is_applied for r in rows):
            continue
        active = next(
            (r for r in rows if not r.is_dismissed and not r.is_applied), None
        )
        if active is not None:
            active.insight_type = draft.insight_type
            active.agent_id = draft.agent_id
            active.title = draft.title
            active.body = draft.body
            active.value_text = draft.value_text
            active.action_label = draft.action_label
            active.action_payload = draft.action_payload
        else:
            db.add(
                Insight(
                    company_id=company.id,
                    agent_id=draft.agent_id,
                    dedup_key=draft.dedup_key,
                    insight_type=draft.insight_type,
                    title=draft.title,
                    body=draft.body,
                    value_text=draft.value_text,
                    action_label=draft.action_label,
                    action_payload=draft.action_payload,
                )
            )

    for row in existing:
        if (
            not row.is_applied
            and not row.is_dismissed
            and (row.dedup_key is None or row.dedup_key not in draft_keys)
        ):
            db.delete(row)


@router.get("/query", response_model=FinancialMemoryResponse)
def query(
    agent: str = Query(description="Agent slug to read financial memory for"),
    q: str | None = Query(
        default=None, description="Optional free-text question, keyword-routed"
    ),
    company: Company = Depends(get_current_company),
    db: Session = Depends(get_db),
) -> FinancialMemoryResponse:
    """An agent's financial memory: budget, projection, cost-per-output, fingerprint.

    Fingerprint stats are as fresh as the last `/recompute`; `budget` is always live.
    """
    agent_row = db.execute(
        select(Agent).where(Agent.company_id == company.id, Agent.slug == agent)
    ).scalar_one_or_none()
    if agent_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {agent}")

    entity = db.get(Entity, agent_row.entity_id)
    if entity is None:  # FK guarantees it; guard for the type checker
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent entity not found")
    fallback_currency = entity.currency
    now = now_in_entity_tz(entity)

    stored = db.execute(
        select(Fingerprint.feature_vector).where(Fingerprint.agent_id == agent_row.id)
    ).scalar_one_or_none()

    if stored is not None:
        fingerprint = _fingerprint_block_from_vector(stored)
        home_currency = fingerprint.home_currency if fingerprint else None
        currency = home_currency or fallback_currency
        cost_units = _cost_units_for_agent(db, agent_row.id)
        projection = project_spend(
            month_to_date=_month_to_date_for_agent(
                db, agent_row.id, home_currency, now
            ),
            now=now,
            monthly_budget=agent_row.monthly_budget,
        )
    else:
        records = [
            _record(txn)
            for txn in db.execute(
                select(Transaction).where(Transaction.agent_id == agent_row.id)
            ).scalars()
        ]
        intel, features, projection, cost_units = _build_intel(
            agent_row, records, now, fallback_currency
        )
        fingerprint = _fingerprint_block(features)
        currency = intel.currency

    budget = _budget_block(db, agent_row, entity)
    top_units = [_cost_unit_block(u) for u in cost_units[:_QUERY_COST_UNITS]]
    headline = _headline(q, currency, budget, projection, cost_units)

    return FinancialMemoryResponse(
        agent=agent_row.slug,
        currency=currency,
        headline=headline,
        budget=budget,
        projection=_projection_block(projection),
        cost_per_output=top_units,
        fingerprint=fingerprint,
    )


def _cost_units_for_agent(db: Session, agent_id: uuid.UUID) -> list[CostUnit]:
    """SQL twin of `cost_per_output`; must match it term for term (test-pinned)."""
    unit_expr = case(
        (
            or_(Transaction.project.is_(None), Transaction.project == ""),
            _UNATTRIBUTED,
        ),
        else_=Transaction.project,
    )
    rows = db.execute(
        select(
            unit_expr.label("unit"),
            Transaction.local_currency,
            func.coalesce(func.sum(Transaction.local_amount), 0),
            func.count(),
            # One missing token count disqualifies the group, as in the pure fn.
            func.count().filter(Transaction.tokens_consumed.is_(None)),
            func.coalesce(func.sum(Transaction.tokens_consumed), 0),
        )
        .where(Transaction.agent_id == agent_id)
        .group_by(unit_expr, Transaction.local_currency)
    ).all()

    units: list[CostUnit] = []
    for unit, currency, total_spent, count, missing_tokens, summed_tokens in rows:
        total = Decimal(total_spent)
        tokens = int(summed_tokens) if missing_tokens == 0 else None
        units.append(
            CostUnit(
                unit=unit,
                dimension="project",
                currency=currency,
                total_spent=_q_money(total),
                count=count,
                avg_cost=_q_money(total / Decimal(count)),
                total_tokens=tokens,
                cost_per_1k_tokens=(
                    _q_money(total / Decimal(tokens) * 1000) if tokens else None
                ),
            )
        )
    units.sort(key=lambda u: u.avg_cost, reverse=True)
    return units


def _month_to_date_for_agent(
    db: Session, agent_id: uuid.UUID, home_currency: str | None, now: datetime
) -> Decimal:
    """SQL twin of `_month_to_date`.

    Not `committed_spend`: home-currency only and no reversal sign-flip.
    """
    if home_currency is None:
        return Decimal("0")
    month_start = datetime(now.year, now.month, 1, tzinfo=now.tzinfo)
    total = db.execute(
        select(func.coalesce(func.sum(Transaction.local_amount), 0)).where(
            Transaction.agent_id == agent_id,
            Transaction.local_currency == home_currency,
            Transaction.transaction_date >= month_start,
        )
    ).scalar_one()
    return Decimal(total)


def _fingerprint_block_from_vector(
    vector: Mapping[str, Any],
) -> FingerprintBlock | None:
    """Rebuild the fingerprint view from a persisted JSONB `feature_vector`."""
    total_records = int(vector.get("total_records") or 0)
    if total_records == 0:
        return None
    raw_stats = vector.get("amount_stats")
    stats: Mapping[str, Any] | None = (
        raw_stats if isinstance(raw_stats, Mapping) else None
    )
    return FingerprintBlock(
        home_currency=vector.get("home_currency"),
        total_records=total_records,
        sample_size=int(stats["sample_size"]) if stats else 0,
        mean_amount=_opt_decimal(stats["mean"]) if stats else None,
        stddev_amount=_opt_decimal(stats["stddev"]) if stats else None,
        median_amount=_opt_decimal(stats["median"]) if stats else None,
        daily_velocity=Decimal(str(vector.get("daily_velocity", "0"))),
        anomaly_score=Decimal(str(vector.get("anomaly_score", "0"))),
        top_vendors=[
            VendorShareBlock(
                vendor=str(v["vendor"]),
                count=int(v["count"]),
                share=Decimal(str(v["share"])),
            )
            for v in vector.get("top_vendors", [])
        ],
    )


def _opt_decimal(value: object) -> Decimal | None:
    """Parse a stored `str(Decimal)` back to `Decimal`, tolerating a null."""
    return None if value is None else Decimal(str(value))


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
    """One-line answer to `q`, keyword-routed to budget, cost or on-track."""
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
