"""add transaction reversal columns

Revision ID: f3a4b5c6d7e8
Revises: e2b3c4d5f6a7
Create Date: 2026-07-19 16:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: str | None = 'e2b3c4d5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support refunds/reversals as a linked contra transaction.

    A reversal is a real transaction (positive amount, contra journal) pointing at
    the original via ``reverses_transaction_id``; the original is flagged
    ``is_reversed``. No negative amounts (the money CHECKs stay intact) and no
    enum change — status stays COMPLETED for both rows.
    """
    op.add_column(
        "transactions",
        sa.Column("reverses_transaction_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "is_reversed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        "fk_transactions_reverses_transaction_id",
        "transactions",
        "transactions",
        ["reverses_transaction_id"],
        ["id"],
    )
    # At most one reversal per original transaction (idempotent reverse).
    op.create_index(
        "uq_transactions_reverses_transaction_id",
        "transactions",
        ["reverses_transaction_id"],
        unique=True,
        postgresql_where=sa.text("reverses_transaction_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the reversal columns."""
    op.drop_index(
        "uq_transactions_reverses_transaction_id", table_name="transactions"
    )
    op.drop_constraint(
        "fk_transactions_reverses_transaction_id", "transactions", type_="foreignkey"
    )
    op.drop_column("transactions", "is_reversed")
    op.drop_column("transactions", "reverses_transaction_id")
