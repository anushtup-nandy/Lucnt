"""place of supply: entity country/gst_state + tax_records.tax_head

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-19 17:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: str | None = 'd3e4f5a6b7c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add recipient place-of-supply + the GST head tax rows book (T1-3/T1-9)."""
    op.add_column("entities", sa.Column("country", sa.String(length=2), nullable=True))
    op.add_column(
        "entities", sa.Column("gst_state", sa.String(length=10), nullable=True)
    )
    # Back-fill the recipient country from the regime it already implies; gst_state
    # is set per-entity later (needed only to split India intra- vs inter-state).
    op.execute(
        sa.text(
            """
            UPDATE entities SET country = CASE tax_regime
                WHEN 'IN-GST' THEN 'IN'
                WHEN 'SG-GST' THEN 'SG'
                WHEN 'US-Sales-Tax' THEN 'US'
                ELSE NULL
            END
            """
        )
    )

    op.add_column(
        "tax_records", sa.Column("tax_head", sa.String(length=10), nullable=True)
    )
    # Existing rows are the unsplit path: GST for GST/GST_RCM, NONE for US sales tax.
    op.execute(
        sa.text(
            """
            UPDATE tax_records
            SET tax_head = CASE WHEN tax_type = 'SALES_TAX' THEN 'NONE' ELSE 'GST' END
            """
        )
    )
    op.alter_column("tax_records", "tax_head", nullable=False)


def downgrade() -> None:
    """Reverse: drop the head column and the entity place-of-supply columns."""
    op.drop_column("tax_records", "tax_head")
    op.drop_column("entities", "gst_state")
    op.drop_column("entities", "country")
