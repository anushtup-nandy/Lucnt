"""mark estimate-booked captures on the ledger

Flags captures booked at the authorized amount because actual usage was
unreadable. Existing rows stay ``false``: relabelling history would falsify it.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the estimated flag; NOT NULL needs the server default on a populated table."""
    op.add_column(
        "transactions",
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop the estimated flag."""
    op.drop_column("transactions", "estimated")
