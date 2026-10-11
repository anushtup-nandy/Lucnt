"""release orphaned authorization holds (void + TTL)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-28 12:00:00.000000

Existing rows stay NULL ("never expires"): back-filling expires_at would release
every historical hold at once, a silent budget increase.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_authorizations_agent_id_status_released_at_expires_at"


def upgrade() -> None:
    """Add the release/expiry columns and the index `reserved_spend` now needs."""
    op.add_column(
        "authorizations",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("release_reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # reserved_spend filters on these on every authorize(); avoids a seq scan.
    op.create_index(
        _INDEX,
        "authorizations",
        ["agent_id", "status", "released_at", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the columns; released holds reserve again (the safe direction)."""
    op.drop_index(_INDEX, table_name="authorizations")
    op.drop_column("authorizations", "expires_at")
    op.drop_column("authorizations", "release_reason")
    op.drop_column("authorizations", "released_at")
