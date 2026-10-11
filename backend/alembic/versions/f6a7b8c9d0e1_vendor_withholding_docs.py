"""vendors: TRC / Form 10F withholding-doc fields (onboarding)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-22 14:30:00.000000

The s.195 DTAA treaty rate applies only with an unexpired TRC + Form 10F on file.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: str | None = 'e5f6a7b8c9d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add TRC / Form 10F documentation fields to vendors."""
    op.add_column(
        "vendors",
        sa.Column(
            "trc_on_file", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column("vendors", sa.Column("trc_expiry", sa.Date(), nullable=True))
    op.add_column(
        "vendors",
        sa.Column(
            "form_10f_on_file",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column("vendors", "trc_on_file", server_default=None)
    op.alter_column("vendors", "form_10f_on_file", server_default=None)


def downgrade() -> None:
    """Reverse: drop the withholding-doc fields."""
    op.drop_column("vendors", "form_10f_on_file")
    op.drop_column("vendors", "trc_expiry")
    op.drop_column("vendors", "trc_on_file")
