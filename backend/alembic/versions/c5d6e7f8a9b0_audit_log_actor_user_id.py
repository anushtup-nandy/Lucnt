"""record the human actor on governance mutations (P7.0)

`audit_logs` could only name an API key. With the dashboard, budgets, policies,
approvals and reconciliation resolutions are mutated by signed-in humans, and a
governance product that writes those rows with a NULL actor is not recording
them at all.

`frontend/PLAN.md` §10 scoped P7.0 as "no new tables, no migration". This is the
deliberate exception, and the smallest one available: one nullable column, no
backfill, no FK. No backfill because historical rows genuinely had no human
actor — every one of them was an API key, and `actor_api_key_id` already says so.
No FK for the same reason `actor_api_key_id` has none: a deleted user's history
must survive them, since an audit trail a delete can erase is no trail.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the human-actor column alongside the existing API-key actor."""
    op.add_column(
        "audit_logs", sa.Column("actor_user_id", sa.Uuid(), nullable=True)
    )


def downgrade() -> None:
    """Drop the human-actor column."""
    op.drop_column("audit_logs", "actor_user_id")
