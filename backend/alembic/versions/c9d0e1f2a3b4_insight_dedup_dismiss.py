"""insights: dedup_key + dismissed state (Tier 2 T2-10)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-23 22:55:00.000000

Recompute used to delete every un-applied insight and re-insert fresh rows, which
churned ids (racing a human's "apply" → 404) and regenerated a reviewed insight
forever (PRODUCTION_PLAN T2-10). This adds:

- `dedup_key` — a stable natural key (type:agent:subject) so recompute upserts an
  insight *in place* (preserving id / is_applied) instead of delete+insert.
- `is_dismissed` / `dismissed_at` — a reviewed-but-not-applied insight can be
  dismissed; a dismissed insight is suppressed from regeneration.

`dedup_key` is nullable (no uniqueness constraint — the recompute dedups in-app and
self-heals) and back-filled from existing rows so the first post-migration recompute
matches them in place rather than orphaning them.
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
    # Drop the server_default: the model owns the Python-side default from here.
    op.alter_column("insights", "is_dismissed", server_default=None)
    # Back-fill dedup_key to match the generator's key (type:agent:subject, where
    # subject is the cost_spike project or empty for the per-agent types).
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
