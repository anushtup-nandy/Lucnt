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


# (index name, table, columns, partial-index predicate or None). Every entry is
# justified by a real query site -- see the docstring in `upgrade()`.
_INDEXES: tuple[tuple[str, str, list[str], str | None], ...] = (
    # -- transactions: the hot money path + every statement/scan -------------
    # `services/spend_window.py` -- two sequential scans per authorize()
    # (committed_spend + recent_activity_count). The single most important index
    # in the schema: it is on the blocking path of the product's core promise.
    (
        "ix_transactions_agent_id_transaction_date",
        "transactions",
        ["agent_id", "transaction_date"],
        None,
    ),
    # `routers/transactions.py` -- GET /transactions paginates
    # company-scoped rows ordered by created_at DESC. No DESC modifier needed:
    # Postgres scans a b-tree backwards at the same cost.
    (
        "ix_transactions_company_id_created_at",
        "transactions",
        ["company_id", "created_at"],
        None,
    ),
    # `routers/recon.py`, `routers/intelligence.py`,
    # `services/tax_reclass.py` -- company-wide ledger reads to be
    # narrowed to a date window.
    (
        "ix_transactions_company_id_transaction_date",
        "transactions",
        ["company_id", "transaction_date"],
        None,
    ),
    # `routers/transactions.py` -- GET /transactions?vendor=, and the
    # (agent, vendor, currency) grouping the overcharge detector needs.
    (
        "ix_transactions_company_id_vendor",
        "transactions",
        ["company_id", "vendor"],
        None,
    ),
    # `routers/books.py` -- per-entity statement filtering; also the
    # unindexed FK.
    ("ix_transactions_entity_id", "transactions", ["entity_id"], None),
    # -- authorizations: budget reservation + the approval queue -------------
    # `services/spend_window.py` -- reserved_spend uses the
    # (agent_id, status) prefix; recent_activity_count adds created_at. One
    # composite serves both, so authorize() stops seq-scanning holds twice.
    (
        "ix_authorizations_agent_id_status_created_at",
        "authorizations",
        ["agent_id", "status", "created_at"],
        None,
    ),
    # `routers/approvals.py` -- the review/human_hold queue.
    (
        "ix_authorizations_company_id_status",
        "authorizations",
        ["company_id", "status"],
        None,
    ),
    # -- policies: read on every authorize ----------------------------------
    # `routers/transactions.py` -- _fetch_active_policies, per call.
    ("ix_policies_company_id_active", "policies", ["company_id", "active"], None),
    # -- journal_entries: the ledger's line table ---------------------------
    # `routers/transactions.py` (reverse) and `routers/books.py`
    # (journal lines for a page of transactions) -- an unindexed FK today.
    (
        "ix_journal_entries_transaction_id",
        "journal_entries",
        ["transaction_id"],
        None,
    ),
    # `routers/books.py` + `_aggregates` -- every statement filters
    # company_id, optionally entity_id, optionally a posting_date range.
    (
        "ix_journal_entries_company_id_entity_id_posting_date",
        "journal_entries",
        ["company_id", "entity_id", "posting_date"],
        None,
    ),
    # -- tax/withholding children: unindexed FKs ----------------------------
    # `services/tax_reclass.py` and the capture-replay rebuild.
    ("ix_tax_records_transaction_id", "tax_records", ["transaction_id"], None),
    (
        "ix_withholding_records_transaction_id",
        "withholding_records",
        ["transaction_id"],
        None,
    ),
    # -- recon / intelligence read paths ------------------------------------
    # `routers/recon.py` -- the inbox list and the open-count
    # header. (transaction_id lookups already ride uq_recon_txn_type.)
    (
        "ix_reconciliation_exceptions_company_id_status",
        "reconciliation_exceptions",
        ["company_id", "status"],
        None,
    ),
    # `routers/insights.py` -- the un-dismissed insight feed.
    (
        "ix_insights_company_id_is_dismissed",
        "insights",
        ["company_id", "is_dismissed"],
        None,
    ),
    # `routers/memory.py` -- GET /memory. (agent_id rides uq_memories_agent_type.)
    ("ix_memories_company_id", "memories", ["company_id"], None),
    # `routers/intelligence.py` fingerprint upserts. (agent_id is unique already.)
    ("ix_fingerprints_company_id", "fingerprints", ["company_id"], None),
    # `routers/budgets.py` -- workflow rollups.
    ("ix_workflows_company_id", "workflows", ["company_id"], None),
    # -- agents -------------------------------------------------------------
    # `routers/agents.py`, `routers/budgets.py` entity join -- unindexed FK.
    ("ix_agents_entity_id", "agents", ["entity_id"], None),
    # The agent_owner persona scopes GET /agents by owner_user_id.
    # Partial: the column is null for most rows today.
    (
        "ix_agents_owner_user_id",
        "agents",
        ["owner_user_id"],
        "owner_user_id IS NOT NULL",
    ),
)


def upgrade() -> None:
    """Index every column the hot query paths filter, join, or order on.

    Before this migration the schema had exactly one non-unique index
    (``ix_audit_logs_company_id``); everything else was a primary key or a
    unique constraint. Each ``authorize()`` therefore ran two sequential scans
    of ``transactions`` (the velocity/budget windows), two of
    ``authorizations`` (the reservation), and one of ``policies`` -- on the
    blocking path, per call. At ledger scale that is a p99 measured in seconds,
    not a future scale problem.

    Built with ``CONCURRENTLY`` inside an autocommit block (house style)
    so an index backfill never takes a write lock that would stall
    ``capture()``. The trade-off is that a failed concurrent build leaves an
    ``INVALID`` index behind that must be dropped by hand before retrying --
    acceptable versus locking the money path.
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
    """Drop the hot-path indexes (also concurrently, to avoid a write lock).

    ``IF EXISTS`` because autocommit DDL is not atomic with the version table:
    a failure later in a multi-step downgrade rolls back the version row but
    not these drops, and the re-run must not trip over already-dropped indexes.
    """
    with op.get_context().autocommit_block():
        for name, table, _columns, _where in reversed(_INDEXES):
            op.drop_index(
                name, table_name=table, postgresql_concurrently=True, if_exists=True
            )
