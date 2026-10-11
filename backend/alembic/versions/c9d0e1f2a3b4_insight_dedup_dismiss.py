"""insights: dedup_key + dismissed state

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-23 22:55:00.000000

`dedup_key` lets recompute upsert in place (stable ids); no unique constraint, the
app dedups. Dismissed insights are suppressed from regeneration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add dedup_key + dismissed state and back-fill dedup_key."""
    op.add_column("insights", sa.Column("dedup_key", sa.Text(), nullable=True))
    op.add_column(
        "insights",
        sa.Column(
            "is_dismissed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "insights",
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("insights", "is_dismissed", server_default=None)
    # Must match the generator's key so existing rows are upserted, not orphaned.
    op.execute(
        """
        UPDATE insights
        SET dedup_key = insight_type::text || ':' || coalesce(agent_id::text, '')
            || ':' || coalesce(action_payload->>'project', '')
        """
    )


def downgrade() -> None:
    """Drop the three columns (no enum types added)."""
    op.drop_column("insights", "dismissed_at")
    op.drop_column("insights", "is_dismissed")
    op.drop_column("insights", "dedup_key")
