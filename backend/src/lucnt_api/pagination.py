"""Shared list-endpoint pagination; every list must be bounded.

Usage::

    @router.get("")
    def list_things(page: Page = Depends(paginate())) -> ThingListResponse:
        stmt = select(Thing).where(...)
        total = count_total(db, stmt)
        rows = db.execute(
            stmt.order_by(Thing.name).offset(page.offset).limit(page.limit)
        ).scalars().all()

Apply `.offset()/.limit()` at the call site to keep `Select[...]` row types.
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

# Row-shaped collections.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Small, customer-authored collections (policies, memories).
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
    """Total rows matching `stmt`, ignoring its limit/offset."""
    return int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
