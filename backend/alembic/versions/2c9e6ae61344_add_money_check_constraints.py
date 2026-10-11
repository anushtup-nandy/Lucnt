"""add money check constraints

Revision ID: 2c9e6ae61344
Revises: 35aecd0f010b
Create Date: 2026-07-18 13:43:11.886227

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2c9e6ae61344'
down_revision: str | None = '35aecd0f010b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add DB-level money CHECKs; they hold even for writers that bypass Pydantic."""
    op.create_check_constraint(
        "ck_authorizations_amount_positive", "authorizations", "amount > 0"
    )
    op.create_check_constraint(
        "ck_transactions_paid_amount_positive", "transactions", "paid_amount > 0"
    )
    op.create_check_constraint(
        "ck_transactions_local_amount_positive", "transactions", "local_amount > 0"
    )
    op.create_check_constraint(
        "ck_transactions_exchange_rate_positive", "transactions", "exchange_rate > 0"
    )
    op.create_check_constraint(
        "ck_journal_entries_one_sided",
        "journal_entries",
        "debit >= 0 AND credit >= 0 AND (debit = 0) <> (credit = 0)",
    )


def downgrade() -> None:
    """Drop the money integrity CHECKs."""
    op.drop_constraint(
        "ck_journal_entries_one_sided", "journal_entries", type_="check"
    )
    op.drop_constraint(
        "ck_transactions_exchange_rate_positive", "transactions", type_="check"
    )
    op.drop_constraint(
        "ck_transactions_local_amount_positive", "transactions", type_="check"
    )
    op.drop_constraint(
        "ck_transactions_paid_amount_positive", "transactions", type_="check"
    )
    op.drop_constraint(
        "ck_authorizations_amount_positive", "authorizations", type_="check"
    )
