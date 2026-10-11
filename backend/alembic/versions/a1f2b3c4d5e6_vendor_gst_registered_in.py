"""vendors.gst_registered_in — registration-in-buyer-country as the RCM key

Revision ID: a1f2b3c4d5e6
Revises: f5a6b7c8d9e0
Create Date: 2026-07-22 09:30:00.000000

India RCM keys off "GST-registered in the buyer's country", which a boolean can't
express; replaced by an ISO-3166 alpha-2 list, back-filled to ``[country]``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1f2b3c4d5e6'
down_revision: str | None = 'f5a6b7c8d9e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add gst_registered_in, back-fill from is_registered+country, drop the bool."""
    op.add_column(
        "vendors",
        sa.Column(
            "gst_registered_in",
            postgresql.ARRAY(sa.String(length=2)),
            nullable=False,
            server_default="{}",
        ),
    )
    # Rows with no country stay empty (they route to the recon queue).
    op.execute(
        sa.text(
            "UPDATE vendors SET gst_registered_in = ARRAY[country] "
            "WHERE is_registered = true AND country IS NOT NULL"
        )
    )
    op.drop_column("vendors", "is_registered")


def downgrade() -> None:
    """Reverse: re-create is_registered (true iff registered anywhere), drop the list."""
    op.add_column(
        "vendors",
        sa.Column(
            "is_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE vendors SET is_registered = true "
            "WHERE array_length(gst_registered_in, 1) IS NOT NULL"
        )
    )
    op.alter_column("vendors", "is_registered", server_default=None)
    op.drop_column("vendors", "gst_registered_in")
