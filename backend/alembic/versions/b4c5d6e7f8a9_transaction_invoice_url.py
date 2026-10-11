"""persist the capture contract's invoice_url

Nullable, no backfill: historical values were discarded and are unrecoverable.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the evidence-link column; length mirrors the schema's URL_MAX bound."""
    op.add_column(
        "transactions", sa.Column("invoice_url", sa.String(2048), nullable=True)
    )


def downgrade() -> None:
    """Drop the evidence-link column."""
    op.drop_column("transactions", "invoice_url")
