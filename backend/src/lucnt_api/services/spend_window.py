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
from datetime import datetime, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from lucnt_api.models import Authorization, Entity, Transaction, VerdictStatus


def now_in_entity_tz(entity: Entity) -> datetime:
    """Current time in the entity's local timezone (UTC if the tz string is bad)."""
    tz: tzinfo
    try:
        tz = ZoneInfo(entity.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
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
    drift or need a scheduled reset. NB: sums `paid_amount` as-is; FX
    normalisation to a functional currency is Tier 1 (T1-2).
    """
    total = db.execute(
        select(func.coalesce(func.sum(Transaction.paid_amount), 0)).where(
            Transaction.agent_id == agent_id,
            Transaction.transaction_date >= since,
        )
    ).scalar_one()
    return Decimal(total)


def reserved_spend(db: Session, agent_id: uuid.UUID) -> Decimal:
    """Budget reserved by this agent's approved-but-uncaptured holds (T0-1).

    Not period-filtered: an old hold, if ever captured, stamps the *current*
    period, so counting it now is correct and stops a stale hold from being
    captured to bust a later period's budget. Holds that never capture keep
    reserving (fail-closed) until a TTL / `void()` releases them (Tier 5, T5-4).
    """
    total = db.execute(
        select(func.coalesce(func.sum(Authorization.amount), 0)).where(
            Authorization.agent_id == agent_id,
            Authorization.status == VerdictStatus.APPROVED,
            ~_captured_hold_exists(),
        )
    ).scalar_one()
    return Decimal(total)


def recent_activity_count(db: Session, agent_id: uuid.UUID, since: datetime) -> int:
    """Committed spends + uncaptured approved holds since `since` (T0-8).

    Counting holds makes velocity catch an agent authorizing in a tight loop
    without ever capturing — the exact runaway M5 promises to stop, which a
    committed-only count silently misses.
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
            ~_captured_hold_exists(),
        )
    ).scalar_one()
    return int(committed) + int(held)
