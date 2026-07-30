"""Money-path spend windows + budget reservation (shared money-path helpers).

Given a session and an agent, compute the numbers the budget invariant needs:

* ``committed_spend`` — spend derived from the ledger, period-resetting (T0-5);
* ``reserved_spend`` — budget held by approved-but-uncaptured authorizations (T0-1);
* ``recent_activity_count`` — committed spends + outstanding holds, for velocity (T0-8).

Kept in one place so every gate that can *admit* a spend — ``authorize()`` and the
approval queue (T0-11) — measures the budget identically. Free of routing/HTTP.
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
    """`(month_start, day_start)` in `now`'s (entity-local) timezone.

    Local boundaries, not UTC, so an IST month flips at local midnight rather
    than 05:30 (cf. PRODUCTION_PLAN T2-9).
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return month_start, day_start


def _captured_hold_exists() -> ColumnElement[bool]:
    """Correlated `EXISTS`: this authorization already has a committed capture.

    `~` it to select approved holds not yet captured into a transaction.
    """
    return (
        select(Transaction.id)
        .where(Transaction.authorization_id == Authorization.id)
        .exists()
    )


def committed_spend(db: Session, agent_id: uuid.UUID, since: datetime) -> Decimal:
    """Committed spend (from the ledger) by this agent since `since` (T0-5).

    Derived from `transactions.transaction_date`, so 'this month'/'today' windows
    reset automatically at the period boundary — no stored running counter to
    drift or need a scheduled reset. Sums `local_amount` (the entity's functional
    currency, FX-converted at capture) so the budget invariant never adds mixed
    currencies (T1-2). A reversal transaction (T1-16) subtracts rather than adds,
    so a refund frees budget.
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
    """The gate's exact definition of a live reservation, in one place (F-10).

    Both the authorize gate (via ``reserved_spend``) and the budget read (via
    ``company_reserved_spend``) measure reservations with these filters, so the
    number an agent reads is the number the gate enforces — a fork here is the
    check-then-blocked inconsistency F-10 describes.

    * `status == APPROVED` — only an approved hold reserves.
    * `released_at IS NULL` — an explicit `void()` stops reservation (T5-4).
    * unexpired, where a NULL expiry never expires — rows written before the
      TTL column existed keep the old fail-closed behaviour rather than being
      released wholesale by the migration.
    * not yet captured into a transaction.
    """
    return [
        Authorization.status == VerdictStatus.APPROVED,
        Authorization.released_at.is_(None),
        or_(Authorization.expires_at.is_(None), Authorization.expires_at > now),
        ~_captured_hold_exists(),
    ]


def reserved_spend(db: Session, agent_id: uuid.UUID) -> Decimal:
    """Budget reserved by this agent's approved-but-uncaptured holds (T0-1).

    Not period-filtered: an old hold, if ever captured, stamps the *current*
    period, so counting it now is correct and stops a stale hold from being
    captured to bust a later period's budget.

    The definition of "still reserving" lives in ``_reservation_filters`` and
    is shared with the batched budget read (F-10).

    Sums `local_amount` (the hold's functional-currency snapshot from authorize)
    so reservation is measured in the same currency as the budget (T1-2).
    """
    now = datetime.now(UTC)
    total = db.execute(
        select(func.coalesce(func.sum(Authorization.local_amount), 0)).where(
            Authorization.agent_id == agent_id,
            *_reservation_filters(now),
        )
    ).scalar_one()
    # coalesce guarantees non-null at SQL level; the guard is for the type checker
    # (local_amount is a nullable column).
    return Decimal(total if total is not None else 0)


def company_reserved_spend(
    db: Session, company_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Live reservations for every agent in a company, in one aggregate (F-10).

    The batched read-path counterpart to ``reserved_spend`` — `GET /budgets`
    reports what the authorize gate will actually grant, so it must measure
    reservations with the gate's own predicate, and batched so the read does
    not cost a query per agent (T3-3, same reasoning as
    ``company_period_committed_spend``).

    Returns:
        `{agent_id: reserved}` in each agent's functional currency. An agent
        with no live holds is **absent**, not zero — callers must default it.
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
    """`(this_month, today)` committed spend for an agent, in the entity's periods.

    Both are derived from the ledger (T0-5), in the entity's functional currency
    (``local_amount``, T1-2), with month/day boundaries in the entity's local
    timezone (T2-9). This is the *display/read* counterpart to the invariant
    ``authorize()`` already enforces — the budget rollups (``GET /budgets``) and
    ``query()``'s budget headroom must show this period-resetting figure, not the
    never-reset ``agents.spent_ytd`` counter that mis-reads inception-to-date spend
    against a *monthly* budget (PRODUCTION_PLAN T2-3).
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
    """`(this_month, today)` committed spend for every agent in a company.

    The batched counterpart to ``period_committed_spend``, which costs two
    queries *per agent* — a query storm at any real agent count (T3-3). Since
    month/day boundaries are entity-local (T2-9), agents are grouped by entity
    and one aggregate runs per entity: O(entities) queries, not O(agents), and
    for the common single-entity tenant exactly one.

    Filters on `agent_id`/`transaction_date` alone, exactly as
    ``committed_spend`` does — the `agents` join only selects *which* agents,
    so a transaction stamped with a different `entity_id` than its agent's
    still lands in the same bucket both functions would put it in.

    Args:
        db: Session to query.
        company_id: Tenant whose agents to total.
        entities: The company's entities, supplying the local period boundaries.

    Returns:
        `{agent_id: (this_month, today)}`. An agent with no spend this month is
        **absent**, not zero — callers must default it.
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
                # The outer WHERE already bounds to month_start, so only the
                # narrower today-window needs its own FILTER.
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
    """Committed spends + uncaptured approved holds since `since` (T0-8).

    Counting holds makes velocity catch an agent authorizing in a tight loop
    without ever capturing — the exact runaway M5 promises to stop, which a
    committed-only count silently misses.

    An explicitly `void()`ed hold is excluded (T5-4): the caller has stated that
    spend did not happen, so charging it against velocity would throttle an agent
    for activity it correctly cleaned up. Expiry is deliberately *not* excluded
    here — the TTL is far longer than any velocity window, and an agent hammering
    authorize without capturing is precisely the runaway this gate exists to
    catch, so letting age release it would reopen the hole.
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
