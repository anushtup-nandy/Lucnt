"""recon exceptions: currency + dedup_key (recovery correctness)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-23 22:30:00.000000

`currency` lets "$ at stake" aggregate per currency; `dedup_key` stops a dismissed
recurring exception being re-flagged on a new transaction.
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
    op.execute(
        """
        UPDATE reconciliation_exceptions re
        SET currency = t.paid_currency
        FROM transactions t
        WHERE re.transaction_id = t.id
        """
    )
    # Must mirror the app's key canonicalisation exactly, or old rows won't dedupe.
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
