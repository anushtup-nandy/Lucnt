"""create withholding_records (India s.195 withholding, TE-3)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-22 15:15:00.000000

The compliance record for India s.195 withholding on a captured India → non-resident
payment — the second regime beside GST (PRODUCTION_PLAN TE-3). Form ids are nullable
(15CA/15CB generation deferred); the authorize-time block lives on the
authorization's verdict, not here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: str | None = 'f6a7b8c9d0e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the withholding_records table."""
    op.create_table(
        "withholding_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("treaty_rate_applied", sa.Boolean(), nullable=False),
        sa.Column("form_15ca_id", sa.String(length=50), nullable=True),
        sa.Column("form_15cb_id", sa.String(length=50), nullable=True),
        sa.Column("legal_reference", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop the withholding_records table."""
    op.drop_table("withholding_records")
