"""recon exceptions: currency + dedup_key (Tier 2 recovery correctness)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-23 22:30:00.000000

Two columns on `reconciliation_exceptions` for Tier 2 recovery correctness:

- `currency` — the currency of `amount_at_stake` (the transaction's paid currency),
  so the inbox "$ at stake" header aggregates *per currency* instead of summing
  ₹ + $ + ¥ into a meaningless total (PRODUCTION_PLAN T2-2).
- `dedup_key` — a stable recurring-identity key (type:agent:vendor:amount:currency)
  for the ledger-detector types, so a *dismissed* recurring exception (e.g. a
  subscription) is not re-flagged next month on a new transaction row (T2-7). NULL
  for the per-transaction types (failed_delivery, tax_unclassified).

Both are nullable; existing rows are back-filled from the joined transaction. The
back-fill string mirrors the app's canonicalisation (lower/trim vendor,
Numeric(18,4) text) so pre-existing recurring exceptions dedupe correctly too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add currency + dedup_key and back-fill from the joined transaction."""
    op.add_column(
        "reconciliation_exceptions",
        sa.Column("currency", sa.String(length=3), nullable=True),
    )
    op.add_column(
        "reconciliation_exceptions",
        sa.Column("dedup_key", sa.Text(), nullable=True),
    )
    # Back-fill currency for every existing exception from its transaction.
    op.execute(
        """
        UPDATE reconciliation_exceptions re
        SET currency = t.paid_currency
        FROM transactions t
        WHERE re.transaction_id = t.id
        """
    )
    # Back-fill dedup_key for the recurring-identity (ledger-detector) types only.
    # Mirrors the app's canonicalisation: exception_type value, agent uuid text,
    # lower(trim(vendor)), Numeric(18,4) amount text, paid currency.
    op.execute(
        """
        UPDATE reconciliation_exceptions re
        SET dedup_key = re.exception_type::text || ':' || t.agent_id::text
            || ':' || lower(btrim(t.vendor)) || ':' || t.paid_amount::text
            || ':' || t.paid_currency
        FROM transactions t
        WHERE re.transaction_id = t.id
          AND re.exception_type::text
              IN ('duplicate', 'overcharge', 'unused_subscription')
        """
    )


def downgrade() -> None:
    """Drop the two columns (no enum types added)."""
    op.drop_column("reconciliation_exceptions", "dedup_key")
    op.drop_column("reconciliation_exceptions", "currency")
