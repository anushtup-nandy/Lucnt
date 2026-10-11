"""add fx snapshot to authorizations

Revision ID: d1a2b3c4e5f6
Revises: c9e4a1b2d3f5
Create Date: 2026-07-19 13:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd1a2b3c4e5f6'
down_revision: str | None = 'c9e4a1b2d3f5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Snapshot the hold's functional-currency value on authorizations.

    Existing holds backfill at rate 1.0 so uncaptured holds keep reserving budget.
    """
    op.add_column(
        "authorizations",
        sa.Column("exchange_rate", sa.Numeric(precision=18, scale=8), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("local_amount", sa.Numeric(precision=18, scale=4), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("local_currency", sa.String(length=3), nullable=True),
    )
    op.create_check_constraint(
        "ck_authorizations_local_amount_positive",
        "authorizations",
        "local_amount IS NULL OR local_amount > 0",
    )
    op.create_check_constraint(
        "ck_authorizations_exchange_rate_positive",
        "authorizations",
        "exchange_rate IS NULL OR exchange_rate > 0",
    )
    op.execute(
        sa.text(
            "UPDATE authorizations AS a "
            "SET local_amount = a.amount, exchange_rate = 1.0, "
            "local_currency = COALESCE(a.currency, e.currency) "
            "FROM entities AS e "
            "WHERE a.entity_id = e.id AND a.local_amount IS NULL"
        )
    )


def downgrade() -> None:
    """Drop the FX snapshot columns."""
    op.drop_constraint(
        "ck_authorizations_exchange_rate_positive", "authorizations", type_="check"
    )
    op.drop_constraint(
        "ck_authorizations_local_amount_positive", "authorizations", type_="check"
    )
    op.drop_column("authorizations", "local_currency")
    op.drop_column("authorizations", "local_amount")
    op.drop_column("authorizations", "exchange_rate")
