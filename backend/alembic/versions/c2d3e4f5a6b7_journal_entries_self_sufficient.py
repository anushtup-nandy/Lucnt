"""journal_entries self-sufficient (company/entity/posting_date, nullable txn)

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-19 16:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add company_id/entity_id/posting_date; backfill; make transaction_id nullable.

    Journal lines become self-sufficient so the books read path scopes
    and periodises off the line itself, and manual/funding/settlement journals can
    post with no transaction. Existing rows are backfilled from their transaction.
    """
    op.add_column(
        "journal_entries", sa.Column("company_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "journal_entries", sa.Column("entity_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "journal_entries", sa.Column("posting_date", sa.Date(), nullable=True)
    )

    # Backfill from the transaction every existing line already references.
    op.execute(
        sa.text(
            """
            UPDATE journal_entries je
            SET company_id = t.company_id,
                entity_id = t.entity_id,
                posting_date = t.transaction_date::date
            FROM transactions t
            WHERE je.transaction_id = t.id
            """
        )
    )

    op.alter_column("journal_entries", "company_id", nullable=False)
    op.alter_column("journal_entries", "entity_id", nullable=False)
    op.alter_column("journal_entries", "posting_date", nullable=False)
    # Manual/funding/settlement journals post without a transaction.
    op.alter_column("journal_entries", "transaction_id", nullable=True)

    op.create_foreign_key(
        "fk_journal_entries_company_id_companies",
        "journal_entries",
        "companies",
        ["company_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_journal_entries_entity_id_entities",
        "journal_entries",
        "entities",
        ["entity_id"],
        ["id"],
    )


def downgrade() -> None:
    """Reverse: drop the new columns and re-require transaction_id.

    Manual/funding/settlement lines (no transaction) are deleted first so the
    NOT NULL restore is clean on populated data.
    """
    op.execute(
        sa.text("DELETE FROM journal_entries WHERE transaction_id IS NULL")
    )
    op.alter_column("journal_entries", "transaction_id", nullable=False)
    op.drop_constraint(
        "fk_journal_entries_entity_id_entities",
        "journal_entries",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_journal_entries_company_id_companies",
        "journal_entries",
        type_="foreignkey",
    )
    op.drop_column("journal_entries", "posting_date")
    op.drop_column("journal_entries", "entity_id")
    op.drop_column("journal_entries", "company_id")
