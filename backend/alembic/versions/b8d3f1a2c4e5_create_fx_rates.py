"""create fx_rates

Revision ID: b8d3f1a2c4e5
Revises: a7cae8873eeb
Create Date: 2026-07-19 12:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from uuid6 import uuid7


# revision identifiers, used by Alembic.
revision: str = 'b8d3f1a2c4e5'
down_revision: str | None = 'a7cae8873eeb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Baseline usd_per_unit rates; the resolver fails closed for unseeded currencies.
# Seeded here (not seed_dev.py) so every environment has the reference data.
_BASELINE_RATES: tuple[tuple[str, str], ...] = (
    ("USD", "1.0"),  # anchor
    ("INR", "0.0117"),  # ~85.5 INR / USD
    ("SGD", "0.74"),  # ~1.35 SGD / USD
    ("EUR", "1.08"),
    ("GBP", "1.27"),
)


def upgrade() -> None:
    """Create fx_rates (USD-anchored, effective-dated) and seed baseline rates."""
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("usd_per_unit", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "currency", "effective_from", name="uq_fx_rates_currency_effective_from"
        ),
        sa.CheckConstraint(
            "usd_per_unit > 0", name="ck_fx_rates_usd_per_unit_positive"
        ),
    )

    fx_rates = sa.table(
        "fx_rates",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("currency", sa.String()),
        sa.column("usd_per_unit", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("source", sa.String()),
    )
    now = datetime.now(timezone.utc)
    effective = date(2026, 1, 1)
    op.bulk_insert(
        fx_rates,
        [
            {
                "id": uuid7(),
                "created_at": now,
                "currency": code,
                "usd_per_unit": Decimal(rate),
                "effective_from": effective,
                "source": "seed",
            }
            for code, rate in _BASELINE_RATES
        ],
    )


def downgrade() -> None:
    """Drop the fx_rates table."""
    op.drop_table("fx_rates")
