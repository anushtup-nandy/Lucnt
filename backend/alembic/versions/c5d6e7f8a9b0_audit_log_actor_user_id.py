"""record the human actor on governance mutations (P7.0)

No backfill (historical actors were all API keys). No FK: a deleted user's audit
history must survive them.

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
