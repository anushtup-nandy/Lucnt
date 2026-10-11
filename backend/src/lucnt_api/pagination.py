"""One definition of the list-endpoint pagination contract.

Before this module, `limit`/`offset` were declared inline in eight routers with
the cap literal copy-pasted at each site (and two silently divergent values,
200 and 500), while `GET /budgets` and `GET /approvals` had no bound at all. A
per-endpoint bound is a security property, not a nicety — an unpaginated list is
an unbounded response and an unbounded serialization cost for any valid key — so
it belongs in one place that a new endpoint gets by default.

Usage::

    @router.get("")
    def list_things(page: Page = Depends(paginate())) -> ThingListResponse:
        stmt = select(Thing).where(...)
        total = count_total(db, stmt)
        rows = db.execute(
            stmt.order_by(Thing.name).offset(page.offset).limit(page.limit)
        ).scalars().all()

`Page` deliberately does *not* wrap `.offset()/.limit()` in a helper method:
applying them at the call site keeps SQLAlchemy's `Select[...]` row types intact
for mypy, which a generic passthrough would erode to `Any`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

__all__ = [
    "Page",
    "paginate",
    "count_total",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "WIDE_PAGE_SIZE",
    "WIDE_MAX_PAGE_SIZE",
]

# The default contract for row-shaped collections (transactions, agents, vendors,
# recon exceptions, insights, journals, budgets, approvals).
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# A wider bound for small configuration-shaped collections — policies and
# learned memories are bounded by how many a customer authors, not by ledger
# volume, so a client normally wants them all in one call.
WIDE_PAGE_SIZE = 100
WIDE_MAX_PAGE_SIZE = 500


@dataclass(frozen=True)
class Page:
    """A validated `(limit, offset)` window for a list query."""

    limit: int
    offset: int


def paginate(
    default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE
) -> Callable[..., Page]:
    """Build a FastAPI dependency yielding a bounded `Page`.

    A factory rather than a plain dependency so an endpoint can widen its own
    bound (``paginate(WIDE_PAGE_SIZE, WIDE_MAX_PAGE_SIZE)``) without restating
    the `ge`/`le` validation or the `offset` parameter.

    Args:
        default: Page size when the caller sends no `limit`.
        maximum: Hard ceiling; a larger `limit` is a 422, never a silent clamp.

    Returns:
        A dependency callable for `Depends(...)`.
    """

    def dependency(
        limit: int = Query(default=default, ge=1, le=maximum),
        offset: int = Query(default=0, ge=0),
    ) -> Page:
        return Page(limit=limit, offset=offset)

    return dependency


def count_total(db: Session, stmt: Select[Any]) -> int:
    """Total rows matching `stmt`, ignoring its limit/offset.

    Wraps the statement as a subquery so the caller's filters and joins are
    counted exactly as written — the idiom that was duplicated verbatim at nine
    call sites.
    """
    return int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
