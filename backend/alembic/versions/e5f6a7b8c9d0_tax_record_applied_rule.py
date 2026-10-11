"""tax_records.applied_rule_id + applied_rule_source (audit trail)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-22 13:30:00.000000

An auditor must be able to answer "why was this taxed this way" without
reverse-engineering the rate tables months later. Each
`tax_records` row now links to the exact `tax_rates` row that produced it
(`applied_rule_id`) and snapshots its provenance (`applied_rule_source`).
Nullable: the conservative paths that apply no rate (UNCLASSIFIED, CUSTOMS,
US-unconfigured) leave it null. The `effective_to` close-out column landed with
US sales/use tax; the resolver already honours it (a closed rate is not applied
after its close date).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the applied-rule link + source snapshot to tax_records."""
    op.add_column(
        "tax_records", sa.Column("applied_rule_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "tax_records",
        sa.Column("applied_rule_source", sa.String(length=50), nullable=True),
    )
    op.create_foreign_key(
        "fk_tax_records_applied_rule_id_tax_rates",
        "tax_records",
        "tax_rates",
        ["applied_rule_id"],
        ["id"],
    )


def downgrade() -> None:
    """Reverse: drop the FK and both columns."""
    op.drop_constraint(
        "fk_tax_records_applied_rule_id_tax_rates", "tax_records", type_="foreignkey"
    )
    op.drop_column("tax_records", "applied_rule_source")
    op.drop_column("tax_records", "applied_rule_id")
