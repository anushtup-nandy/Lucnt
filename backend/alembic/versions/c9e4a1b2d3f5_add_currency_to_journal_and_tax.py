"""add currency to journal_entries and tax_records

Revision ID: c9e4a1b2d3f5
Revises: b8d3f1a2c4e5
Create Date: 2026-07-19 13:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c9e4a1b2d3f5'
down_revision: str | None = 'b8d3f1a2c4e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stamp the functional (entity) currency on journal + tax rows.

    Added nullable, backfilled from each row's transaction's entity currency,
    then set NOT NULL — the safe pattern for a NOT NULL column on a populated
    table. New rows are stamped by the capture path in the same change.
    """
    op.add_column(
        "journal_entries", sa.Column("currency", sa.String(length=3), nullable=True)
    )
    op.add_column(
        "tax_records", sa.Column("currency", sa.String(length=3), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE journal_entries AS je SET currency = e.currency "
            "FROM transactions AS t JOIN entities AS e ON e.id = t.entity_id "
            "WHERE je.transaction_id = t.id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tax_records AS tr SET currency = e.currency "
            "FROM transactions AS t JOIN entities AS e ON e.id = t.entity_id "
            "WHERE tr.transaction_id = t.id"
        )
    )
    op.alter_column("journal_entries", "currency", nullable=False)
    op.alter_column("tax_records", "currency", nullable=False)


def downgrade() -> None:
    """Drop the currency columns."""
    op.drop_column("tax_records", "currency")
    op.drop_column("journal_entries", "currency")
