"""tax_rates: jurisdiction/rule_type/effective_to + entities.us_state; seed US states

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-22 11:30:00.000000

Seeds all 51 jurisdictions (B2B branch for split states). **Rates are unverified
research; confirm with a CPA before relying on them.**
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from uuid6 import uuid7


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# US SaaS taxability, B2B branch (TX nets its 80% rule to ~5%). City home-rule tax
# is out of scope. VT is disputed: routed to the exception queue, never applied.
_US_TAXABLE: dict[str, str] = {
    "AZ": "0.056", "DC": "0.06", "HI": "0.04", "IN": "0.07", "KY": "0.06",
    "LA": "0.05", "MA": "0.0625", "MS": "0.07", "NE": "0.055", "NM": "0.05",
    "NY": "0.04", "OH": "0.0575", "PA": "0.06", "RI": "0.07", "SC": "0.06",
    "SD": "0.045", "TN": "0.07", "TX": "0.05", "UT": "0.061", "WA": "0.065",
    "WV": "0.06", "CT": "0.01", "MD": "0.03",
}
_US_EXEMPT: tuple[str, ...] = (
    "AL", "AR", "CA", "FL", "GA", "ID", "KS", "ME", "MI", "MN", "MO", "NV",
    "NJ", "NC", "ND", "OK", "VA", "WI", "WY", "CO", "IL", "IA",
)
_US_NO_TAX: tuple[str, ...] = ("AK", "DE", "MT", "NH", "OR")
_US_DISPUTED: dict[str, str] = {"VT": "0.06"}

_US_CATEGORY = "standard"
_US_EFFECTIVE_FROM = date(2020, 1, 1)


def _us_rows() -> list[dict[str, object]]:
    """Build the (jurisdiction, rule_type, rate) seed rows for every US state."""
    now = datetime.now(timezone.utc)
    rows: list[dict[str, object]] = []

    def add(state: str, rule_type: str, rate: str) -> None:
        rows.append(
            {
                "id": uuid7(),
                "created_at": now,
                "regime": "US-Sales-Tax",
                "category": _US_CATEGORY,
                "jurisdiction": f"US-{state}",
                "rule_type": rule_type,
                "rate": Decimal(rate),
                "effective_from": _US_EFFECTIVE_FROM,
                "effective_to": None,
                "source": "seed:appendix-a",
            }
        )

    for state, rate in _US_TAXABLE.items():
        add(state, "standard", rate)
    for state in _US_EXEMPT:
        add(state, "exempt", "0")
    for state in _US_NO_TAX:
        add(state, "no_tax", "0")
    for state, rate in _US_DISPUTED.items():
        add(state, "disputed", rate)
    return rows


def upgrade() -> None:
    """Add jurisdiction/rule_type/effective_to + us_state; seed the US state table."""
    op.add_column(
        "tax_rates", sa.Column("jurisdiction", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "tax_rates",
        sa.Column(
            "rule_type",
            sa.String(length=20),
            nullable=False,
            server_default="standard",
        ),
    )
    op.add_column(
        "tax_rates", sa.Column("effective_to", sa.Date(), nullable=True)
    )
    # The default only back-filled; the app sets rule_type explicitly.
    op.alter_column("tax_rates", "rule_type", server_default=None)

    # US rows differ only by jurisdiction, so it joins the unique key.
    op.drop_constraint(
        "uq_tax_rates_regime_category_effective_from", "tax_rates", type_="unique"
    )
    op.create_unique_constraint(
        "uq_tax_rates_regime_category_jurisdiction_effective_from",
        "tax_rates",
        ["regime", "category", "jurisdiction", "effective_from"],
    )

    op.add_column(
        "entities", sa.Column("us_state", sa.String(length=2), nullable=True)
    )

    tax_rates = sa.table(
        "tax_rates",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("regime", sa.String()),
        sa.column("category", sa.String()),
        sa.column("jurisdiction", sa.String()),
        sa.column("rule_type", sa.String()),
        sa.column("rate", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
        sa.column("source", sa.String()),
    )
    op.bulk_insert(tax_rates, _us_rows())


def downgrade() -> None:
    """Reverse: drop the seeded US per-state rows, restore the old unique key + cols."""
    op.execute(
        sa.text(
            "DELETE FROM tax_rates WHERE regime = 'US-Sales-Tax' "
            "AND jurisdiction IS NOT NULL"
        )
    )
    op.drop_column("entities", "us_state")
    op.drop_constraint(
        "uq_tax_rates_regime_category_jurisdiction_effective_from",
        "tax_rates",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_tax_rates_regime_category_effective_from",
        "tax_rates",
        ["regime", "category", "effective_from"],
    )
    op.drop_column("tax_rates", "effective_to")
    op.drop_column("tax_rates", "rule_type")
    op.drop_column("tax_rates", "jurisdiction")
