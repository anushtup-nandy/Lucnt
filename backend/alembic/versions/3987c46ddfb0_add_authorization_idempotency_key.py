"""add authorization idempotency key

Revision ID: 3987c46ddfb0
Revises: 2c9e6ae61344
Create Date: 2026-07-18 15:20:45.034067

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3987c46ddfb0'
down_revision: str | None = '2c9e6ae61344'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add authorization idempotency.

    A client-supplied ``idempotency_key`` lets a retried ``authorize`` (e.g. a
    timed-out ``spend()``) return the original hold instead of minting a fresh
    one — which, followed by a capture, would double-book. ``verdict_code``
    persists the machine-readable code so an idempotent replay returns the exact
    original verdict (it cannot be re-derived — policies/spend move on).
    """
    op.add_column(
        "authorizations",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("verdict_code", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "uq_authorizations_company_idempotency_key",
        "authorizations",
        ["company_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop authorization idempotency."""
    op.drop_index(
        "uq_authorizations_company_idempotency_key", table_name="authorizations"
    )
    op.drop_column("authorizations", "verdict_code")
    op.drop_column("authorizations", "idempotency_key")
