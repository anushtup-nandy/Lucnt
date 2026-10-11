"""index every hot query column

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-25 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: str | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (index name, table, columns, partial-index predicate or None).
_INDEXES: tuple[tuple[str, str, list[str], str | None], ...] = (
    # spend_window: on the blocking path of every authorize().
    (
        "ix_transactions_agent_id_transaction_date",
        "transactions",
        ["agent_id", "transaction_date"],
        None,
    ),
    # GET /transactions orders DESC; b-trees scan backwards at no extra cost.
    (
        "ix_transactions_company_id_created_at",
        "transactions",
        ["company_id", "created_at"],
        None,
    ),
    (
        "ix_transactions_company_id_transaction_date",
        "transactions",
        ["company_id", "transaction_date"],
        None,
    ),
    (
        "ix_transactions_company_id_vendor",
        "transactions",
        ["company_id", "vendor"],
        None,
    ),
    ("ix_transactions_entity_id", "transactions", ["entity_id"], None),
    # One composite serves both reserved_spend (prefix) and recent_activity_count.
    (
        "ix_authorizations_agent_id_status_created_at",
        "authorizations",
        ["agent_id", "status", "created_at"],
        None,
    ),
    (
        "ix_authorizations_company_id_status",
        "authorizations",
        ["company_id", "status"],
        None,
    ),
    ("ix_policies_company_id_active", "policies", ["company_id", "active"], None),
    (
        "ix_journal_entries_transaction_id",
        "journal_entries",
        ["transaction_id"],
        None,
    ),
    (
        "ix_journal_entries_company_id_entity_id_posting_date",
        "journal_entries",
        ["company_id", "entity_id", "posting_date"],
        None,
    ),
    ("ix_tax_records_transaction_id", "tax_records", ["transaction_id"], None),
    (
        "ix_withholding_records_transaction_id",
        "withholding_records",
        ["transaction_id"],
        None,
    ),
    (
        "ix_reconciliation_exceptions_company_id_status",
        "reconciliation_exceptions",
        ["company_id", "status"],
        None,
    ),
    (
        "ix_insights_company_id_is_dismissed",
        "insights",
        ["company_id", "is_dismissed"],
        None,
    ),
    ("ix_memories_company_id", "memories", ["company_id"], None),
    ("ix_fingerprints_company_id", "fingerprints", ["company_id"], None),
    ("ix_workflows_company_id", "workflows", ["company_id"], None),
    ("ix_agents_entity_id", "agents", ["entity_id"], None),
    # Partial: owner_user_id is null for most rows.
    (
        "ix_agents_owner_user_id",
        "agents",
        ["owner_user_id"],
        "owner_user_id IS NOT NULL",
    ),
)


def upgrade() -> None:
    """Index the hot query paths, CONCURRENTLY so capture() is never write-locked.

    A failed concurrent build leaves an INVALID index; drop it by hand before retry.
    """
    with op.get_context().autocommit_block():
        for name, table, columns, where in _INDEXES:
            op.create_index(
                name,
                table,
                columns,
                unique=False,
                postgresql_concurrently=True,
                postgresql_where=sa.text(where) if where else None,
            )


def downgrade() -> None:
    """Drop the indexes concurrently.

    IF EXISTS: autocommit DDL isn't atomic with the version row, so re-runs must cope.
    """
    with op.get_context().autocommit_block():
        for name, table, _columns, _where in reversed(_INDEXES):
            op.drop_index(
                name, table_name=table, postgresql_concurrently=True, if_exists=True
            )
