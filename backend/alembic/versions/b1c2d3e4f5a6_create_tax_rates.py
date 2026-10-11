"""create tax_rates

Revision ID: b1c2d3e4f5a6
Revises: a4b5c6d7e8f9
Create Date: 2026-07-19 16:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from uuid6 import uuid7


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: str | None = 'a4b5c6d7e8f9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Seeded here (not seed_dev.py) so every environment has the reference data.
# US-Sales-Tax 0 exists so the resolver never fails closed on a US spend.
# (regime, category, rate, effective_from)
_SEED_RATES: tuple[tuple[str, str, str, date], ...] = (
    ("IN-GST", "standard", "0.18", date(2017, 7, 1)),
    ("SG-GST", "standard", "0.05", date(2004, 1, 1)),
    ("SG-GST", "standard", "0.07", date(2007, 7, 1)),
    ("SG-GST", "standard", "0.08", date(2023, 1, 1)),
    ("SG-GST", "standard", "0.09", date(2024, 1, 1)),
    ("US-Sales-Tax", "standard", "0.00", date(2000, 1, 1)),
)


def upgrade() -> None:
    """Create tax_rates (regime/category, effective-dated) and seed statutory rates."""
    op.create_table(
        "tax_rates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("rate", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "regime",
            "category",
            "effective_from",
            name="uq_tax_rates_regime_category_effective_from",
        ),
        sa.CheckConstraint(
            "rate >= 0 AND rate <= 1", name="ck_tax_rates_rate_fraction"
        ),
    )

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
                "regime": regime,
                "category": category,
                "rate": Decimal(rate),
                "effective_from": effective_from,
                "source": "seed",
            }
            for regime, category, rate, effective_from in _SEED_RATES
        ],
    )


def downgrade() -> None:
    """Drop the tax_rates table."""
    op.drop_table("tax_rates")
