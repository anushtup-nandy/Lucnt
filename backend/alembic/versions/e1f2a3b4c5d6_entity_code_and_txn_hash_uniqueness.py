"""unique entity code and transaction hash

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-25 11:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: str | None = 'd0e1f2a3b4c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make entity code and card-rail transaction_hash unique per company.

    A duplicate entity code makes scalar_one_or_none() 500 on every authorize; a
    redelivered card webhook would double-book. Unique indexes, not constraints,
    so they build CONCURRENTLY (no ACCESS EXCLUSIVE lock) and can be partial.
    """
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_entities_company_code",
            "entities",
            ["company_id", "code"],
            unique=True,
            postgresql_concurrently=True,
        )
        op.create_index(
            "uq_transactions_company_transaction_hash",
            "transactions",
            ["company_id", "transaction_hash"],
            unique=True,
            postgresql_concurrently=True,
            postgresql_where=sa.text("transaction_hash IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop both uniqueness guards (concurrently, to avoid a write lock)."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_transactions_company_transaction_hash",
            table_name="transactions",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "uq_entities_company_code",
            table_name="entities",
            postgresql_concurrently=True,
        )
