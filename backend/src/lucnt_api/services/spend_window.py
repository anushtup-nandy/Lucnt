"""Money-path spend windows and budget reservation.

Shared by every gate that admits a spend so the budget is measured identically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.orm import Session

from lucnt_api.models import Agent, Authorization, Entity, Transaction, VerdictStatus

__all__ = [
    "now_in_entity_tz",
    "period_starts",
    "committed_spend",
    "reserved_spend",
    "company_reserved_spend",
    "recent_activity_count",
    "period_committed_spend",
    "company_period_committed_spend",
]


def now_in_entity_tz(entity: Entity) -> datetime:
    """Current time in the entity's local timezone (UTC if the tz string is bad)."""
    tz: tzinfo
    try:
        tz = ZoneInfo(entity.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return datetime.now(tz)


def period_starts(now: datetime) -> tuple[datetime, datetime]:
    """`(month_start, day_start)` in `now`'s entity-local timezone, not UTC."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return month_start, day_start


def _captured_hold_exists() -> ColumnElement[bool]:
    """Correlated `EXISTS`: this authorization already has a committed capture."""
    return (
        select(Transaction.id)
        .where(Transaction.authorization_id == Authorization.id)
        .exists()
    )


def committed_spend(db: Session, agent_id: uuid.UUID, since: datetime) -> Decimal:
    """Ledger-derived spend by this agent since `since`, in functional currency.

    Reversals subtract, so a refund frees budget.
    """
    signed_amount = case(
        (Transaction.reverses_transaction_id.is_(None), Transaction.local_amount),
        else_=-Transaction.local_amount,
    )
    total = db.execute(
        select(func.coalesce(func.sum(signed_amount), 0)).where(
            Transaction.agent_id == agent_id,
            Transaction.transaction_date >= since,
        )
    ).scalar_one()
    return Decimal(total)


def _reservation_filters(now: datetime) -> list[ColumnElement[bool]]:
    """Live-reservation predicate shared by the gate and the budget read.

    One definition so what an agent reads is what the gate enforces. NULL expiry
    never expires (fail-closed).
    """
    return [
        Authorization.status == VerdictStatus.APPROVED,
        Authorization.released_at.is_(None),
        or_(Authorization.expires_at.is_(None), Authorization.expires_at > now),
        ~_captured_hold_exists(),
    ]


def reserved_spend(db: Session, agent_id: uuid.UUID) -> Decimal:
    """Budget reserved by this agent's approved-but-uncaptured holds.

    Not period-filtered: a captured old hold stamps the current period.
    """
    now = datetime.now(UTC)
    total = db.execute(
        select(func.coalesce(func.sum(Authorization.local_amount), 0)).where(
            Authorization.agent_id == agent_id,
            *_reservation_filters(now),
        )
    ).scalar_one()
    # Guard is for the type checker; coalesce already ensures non-null.
    return Decimal(total if total is not None else 0)


def company_reserved_spend(
    db: Session, company_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Batched ``reserved_spend`` for every agent in a company.

    Returns:
        `{agent_id: reserved}`; agents with no live holds are absent, not zero.
    """
    now = datetime.now(UTC)
    rows = db.execute(
        select(
            Authorization.agent_id,
            func.coalesce(func.sum(Authorization.local_amount), 0),
        )
        .where(Authorization.company_id == company_id, *_reservation_filters(now))
        .group_by(Authorization.agent_id)
    ).all()
    return {agent_id: Decimal(total) for agent_id, total in rows}


def period_committed_spend(
    db: Session, agent_id: uuid.UUID, entity: Entity
) -> tuple[Decimal, Decimal]:
    """`(this_month, today)` committed spend for an agent, in entity-local periods.

    Use this, not ``agents.spent_ytd``, against a monthly budget.
    """
    now = now_in_entity_tz(entity)
    month_start, day_start = period_starts(now)
    return (
        committed_spend(db, agent_id, month_start),
        committed_spend(db, agent_id, day_start),
    )


def company_period_committed_spend(
    db: Session, company_id: uuid.UUID, entities: Iterable[Entity]
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Batched ``period_committed_spend``: one query per entity, not per agent.

    Args:
        db: Session to query.
        company_id: Tenant whose agents to total.
        entities: The company's entities, supplying the local period boundaries.

    Returns:
        `{agent_id: (this_month, today)}`; agents with no spend are absent.
    """
    signed_amount = case(
        (Transaction.reverses_transaction_id.is_(None), Transaction.local_amount),
        else_=-Transaction.local_amount,
    )
    spend: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
    for entity in entities:
        month_start, day_start = period_starts(now_in_entity_tz(entity))
        rows = db.execute(
            select(
                Transaction.agent_id,
                func.coalesce(func.sum(signed_amount), 0),
                func.coalesce(
                    func.sum(signed_amount).filter(
                        Transaction.transaction_date >= day_start
                    ),
                    0,
                ),
            )
            .join(Agent, Agent.id == Transaction.agent_id)
            .where(
                Agent.company_id == company_id,
                Agent.entity_id == entity.id,
                Transaction.transaction_date >= month_start,
            )
            .group_by(Transaction.agent_id)
        ).all()
        for agent_id, month_total, today_total in rows:
            spend[agent_id] = (Decimal(month_total), Decimal(today_total))
    return spend


def recent_activity_count(db: Session, agent_id: uuid.UUID, since: datetime) -> int:
    """Committed spends + uncaptured approved holds since `since`.

    Holds count so an authorize-without-capture loop still trips velocity.
    Voided holds are excluded; expired ones deliberately are not.
    """
    committed = db.execute(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.agent_id == agent_id,
            Transaction.transaction_date >= since,
        )
    ).scalar_one()
    held = db.execute(
        select(func.count())
        .select_from(Authorization)
        .where(
            Authorization.agent_id == agent_id,
            Authorization.status == VerdictStatus.APPROVED,
            Authorization.created_at >= since,
            Authorization.released_at.is_(None),
            ~_captured_hold_exists(),
        )
    ).scalar_one()
    return int(committed) + int(held)
