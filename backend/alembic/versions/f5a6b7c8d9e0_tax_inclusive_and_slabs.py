"""tax inclusive/exclusive flag + rate category on authz/txn; seed GST slabs

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-19 18:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from uuid6 import uuid7


# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: str | None = 'e4f5a6b7c8d9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# India GST rate slabs (T1-10) so a caller can state a 0/5/12/28% supply instead of
# always paying the 18% "standard" rate the compile-time constant assumed.
_IN_SLABS: tuple[tuple[str, str], ...] = (
    ("gst_0", "0.00"),
    ("gst_5", "0.05"),
    ("gst_12", "0.12"),
    ("gst_18", "0.18"),
    ("gst_28", "0.28"),
)
_SLAB_EFFECTIVE = date(2017, 7, 1)


def upgrade() -> None:
    """Add tax_category + tax_inclusive to authorizations/transactions; seed slabs."""
    for table in ("authorizations", "transactions"):
        op.add_column(
            table, sa.Column("tax_category", sa.String(length=20), nullable=True)
        )
        op.add_column(
            table,
            sa.Column(
                "tax_inclusive",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )
        # The default was only needed to back-fill existing rows; new rows carry an
        # explicit value from the app.
        op.alter_column(table, "tax_inclusive", server_default=None)

    tax_rates = sa.table(
        "tax_rates",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("regime", sa.String()),
        sa.column("category", sa.String()),
        sa.column("rate", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("source", sa.String()),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        tax_rates,
        [
            {
                "id": uuid7(),
                "created_at": now,
                "regime": "IN-GST",
                "category": category,
                "rate": Decimal(rate),
                "effective_from": _SLAB_EFFECTIVE,
                "source": "seed",
            }
            for category, rate in _IN_SLABS
        ],
    )


def downgrade() -> None:
    """Reverse: drop the columns and the seeded slab rows."""
    op.execute(
        sa.text(
            "DELETE FROM tax_rates WHERE regime = 'IN-GST' AND category LIKE 'gst_%'"
        )
    )
    for table in ("transactions", "authorizations"):
        op.drop_column(table, "tax_inclusive")
        op.drop_column(table, "tax_category")
