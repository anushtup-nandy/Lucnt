"""add expense category to authorizations and transactions

Revision ID: e2b3c4d5f6a7
Revises: d1a2b3c4e5f6
Create Date: 2026-07-19 15:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e2b3c4d5f6a7'
down_revision: str | None = 'd1a2b3c4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the caller-stated expense category (T1-19), nullable on both tables."""
    op.add_column(
        "authorizations", sa.Column("category", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "transactions", sa.Column("category", sa.String(length=50), nullable=True)
    )


def downgrade() -> None:
    """Drop the expense category columns."""
    op.drop_column("transactions", "category")
    op.drop_column("authorizations", "category")
