"""Intelligence router (Phase 5, M8) — recompute the CFO tables, serve live memory.

Two endpoints, both ledger-reading (they never touch the frozen `authorize→capture`
spine, like the Phase 4 recon scan):

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
    # Imported from the pure engine so the SQL cost-unit aggregate below uses
    # the engine's own fallback label and money rounding rather than a copy
    # that could drift from it.
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

_WARNING_PCT = Decimal("80")  # mirrors budgets._rollup — utilisation warning line
_ONE_PLACE = Decimal("0.1")
_QUERY_COST_UNITS = 5  # top cost-per-output rows returned by query()
# Rows fetched per round trip when `recompute` streams the whole company ledger.
# Mirrors `recon._LEDGER_CHUNK_SIZE`: big enough to amortise the round
# trip, small enough that no single fetch reintroduces the memory spike.
_LEDGER_CHUNK_SIZE = 1_000

# The column tuple `recompute` selects, in SELECT order. Declared next to
# `_record_from_row` so the projection and its unpack cannot silently drift.
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
    """Project a committed transaction to the intelligence value object.

    Uses the entity-local figures so cross-agent comparisons stay currency-consistent.
    Keep in sync with `_record_from_row`, its column-tuple twin.
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


def _record_from_row(row: _SpendRow) -> SpendRecord:
    """Project a streamed ledger row to the intelligence value object.

    The column-tuple twin of `_record`. `recompute` scans the entire
    company ledger, so it selects the eight columns a `SpendRecord` needs rather
    than hydrating — and retaining — a `Transaction` entity per row.

    Args:
        row: `(agent_id, vendor, local_amount, local_currency, project,
            model_used, tokens_consumed, transaction_date)`.

    Returns:
        The `SpendRecord` equivalent of that row.
    """
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
    """Live budget headroom for the current month (mirrors the budgets rollup).

    Spend is the entity-local, current-month committed figure derived from the
    ledger, not the never-reset ``agents.spent_ytd`` counter — so an agent
    on-budget this month is not told it is "exhausted" by an inception-to-date
    total compared against a monthly budget.
    """
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

    ``now`` is entity-local and ``fallback_currency`` is the entity's functional
    currency; the fingerprint pins its home currency to it and ages its velocity
    to ``now``, and the projection uses local month boundaries.
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
    """Recompute fingerprints/memories/insights over the whole company ledger.

    Upserts one fingerprint per agent and one memory per (agent, type); clears
    the company's **un-applied** insights and regenerates them (applied insights
    persist as a record of the action a human took). Idempotent — a second call
    with no new spend yields the same state.
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

    # Stream only the eight columns a `SpendRecord` needs, grouped in a
    # single pass. Hydrating `Transaction` entities pinned the whole ledger in
    # the identity map *alongside* the records derived from it, for a response
    # of five scalars. Ordering by `agent_id` keeps each agent's rows
    # contiguous, so the grouping walks the stream without random dict churn.
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

    # One SELECT for every agent's fingerprint instead of one per agent.
    fingerprints = _existing_fingerprints(db, records_by_agent.keys())

    intels: list[AgentIntel] = []
    for agent_id, records in records_by_agent.items():
        agent = agents.get(agent_id)
        if agent is None:  # defensive: FK guarantees it exists
            continue
        entity = entities.get(agent.entity_id)
        fallback = entity.currency if entity is not None else "USD"
        # Compute in the entity's local time (month boundaries, velocity aging);
        # stamp the fingerprint row with a real UTC wall-clock time.
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
    """Load the fingerprint rows for `agent_ids` in one query.

    Replaces the per-agent SELECT that `_upsert_fingerprint` used to issue from
    inside the recompute loop. `uq_fingerprints_agent` guarantees at most one
    row per agent, so `agent_id` is a safe key.

    Args:
        db: Active session.
        agent_ids: Agents whose fingerprints are about to be upserted.

    Returns:
        Existing fingerprints keyed by `agent_id`; agents with none are absent.
    """
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
    """Insert or refresh the single fingerprint row for an agent (race-safe).

    A concurrent recompute may insert the row between our prefetch and INSERT;
    the SAVEPOINT lets us fall back to an update instead of 500-ing on the
    `uq_fingerprints_agent` violation.

    Args:
        db: Active session.
        company: Owning company (stamped on an inserted row).
        agent: Agent the fingerprint describes.
        features: Freshly computed feature vector and anomaly score.
        now: UTC wall-clock time to stamp as `last_updated`.
        existing_by_agent: Prefetched fingerprints from `_existing_fingerprints`
            — the loop-invariant replacement for a per-agent SELECT.
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
    """Load the memory rows `drafts` will upsert, in one query.

    Replaces the per-draft SELECT inside `_upsert_memories`. `uq_memories_agent_type`
    guarantees at most one row per `(agent_id, memory_type)`, so that pair is a
    safe key.

    Args:
        db: Active session.
        drafts: Memories about to be upserted.

    Returns:
        Existing memories keyed by `(agent_id, memory_type)`.
    """
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

    Race-safe against a concurrent recompute via a per-row SAVEPOINT that falls
    back to an update on the `uq_memories_agent_type` violation. That
    fallback also covers the one case the prefetched map cannot see: two drafts
    sharing a key, where the second insert collides with the first.

    Args:
        db: Active session.
        company: Owning company (stamped on an inserted row).
        drafts: Memories to upsert.
        existing_by_key: Prefetched rows from `_existing_memories`.

    Returns:
        The number of memories written (inserted or refreshed).
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
    """Upsert insights on their stable `dedup_key`, preserving ids.

    Instead of delete-all + reinsert (which churned ids and raced a human's
    "apply"), match each draft to an existing row by `dedup_key`:
    - a dismissed/applied row for that key → leave it (the human handled it; do not
      regenerate an active copy — no more nagging forever);
    - an active row → refresh its content in place (stable id);
    - no row → insert.
    Finally, delete active rows whose condition no longer regenerates (stale).
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
    """An agent's living financial memory (M8), served without a ledger scan.

    Two paths, because this is the per-spend SDK call and a full scan per call is
    O(n) per call and O(n^2) over an agent's lifetime:

    * **Fast path** — an agent that has been through `/recompute` has a stored
      `Fingerprint`, so the statistics are read rather than recomputed, and the
      projection and cost-per-output come from SQL aggregates. No record is
      loaded into Python.
    * **Fallback** — no stored fingerprint (an agent that has never been
      recomputed) computes live exactly as before. This is what keeps the
      documented "needs no prior recompute" contract true.

    **Staleness is real and deliberate.** On the fast path the fingerprint
    statistics — mean, stddev, median, top vendors, anomaly score — are as fresh
    as the last `/recompute`. `budget` is always live (a SQL sum over the ledger),
    so spend headroom, the figure an agent gates on, is never stale.
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
    """`cost_per_output` as one `GROUP BY`, instead of scanning every record.

    Deliberately mirrors `services.intelligence.cost_per_output` term for term —
    same `(project, currency)` grouping, same `"(unattributed)"` fallback for a
    null *or empty* project, same "tokens only when every member has them" rule,
    and the same `_q_money` rounding applied in Python so the SQL sum is never
    quantized differently. `test_sql_cost_units_match_the_pure_function` pins the
    two against each other; that test, not this comment, is what stops them
    drifting.

    Args:
        db: Session to query.
        agent_id: The agent whose spend to group.

    Returns:
        Cost units, dearest average first — the same list `cost_per_output`
        would return for the same rows.
    """
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
            # A single missing token count disqualifies the whole group, exactly
            # as `all(m.tokens is not None ...)` does in the pure function.
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
    """The SQL twin of `_month_to_date`.

    Matches it exactly, including the two things that make it *not*
    interchangeable with `spend_window.committed_spend`: it filters to the home
    currency, and it does **not** sign-flip reversals. Collapsing the two would
    silently change the projection's `month_to_date`.
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
    """Rebuild the fingerprint view from a persisted `feature_vector`.

    `as_feature_vector` stores every money/ratio field as `str(Decimal)`, so this
    round-trips exactly — no float ever touches the figures. `Any` is unavoidable
    here: the source is a JSONB column, so the values are genuinely untyped until
    this function narrows them.
    """
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
