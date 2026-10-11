"""recon_exception_type += tax_unclassified (TE-1b conservative-default flag)

Revision ID: b2c3d4e5f6a7
Revises: a1f2b3c4d5e6
Create Date: 2026-07-22 10:15:00.000000

When a spend hits a vendor with no place-of-supply data, the tax engine can no
longer silently guess import-vs-domestic from currency (PRODUCTION_PLAN TE-1). It
books a conservative no-phantom treatment and the money path opens a recon
exception so a finance admin enriches the vendor. That needs a new
`recon_exception_type` value — this one is *capture-emitted*, unlike the ledger
detectors' types.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1f2b3c4d5e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the tax_unclassified value to the recon_exception_type enum.

    Postgres 12+ allows ADD VALUE inside a transaction as long as the new value is
    not *used* in the same transaction (it is not — only rows written later use it).
    """
    op.execute(
        "ALTER TYPE recon_exception_type ADD VALUE IF NOT EXISTS 'tax_unclassified'"
    )


def downgrade() -> None:
    """Rebuild the enum without tax_unclassified.

    Postgres cannot DROP a single enum value, so the type is recreated and the
    column re-cast. Rows holding the value are deleted first: they are
    capture-emitted flags with no meaning in the pre-feature schema, and a full
    ``downgrade base && upgrade head`` must stay runnable (same convention as the
    enum-type drop fixes).
    """
    op.execute(
        "DELETE FROM reconciliation_exceptions "
        "WHERE exception_type = 'tax_unclassified'"
    )
    op.execute(
        "ALTER TYPE recon_exception_type RENAME TO recon_exception_type_old"
    )
    op.execute(
        "CREATE TYPE recon_exception_type AS ENUM("
        "'duplicate', 'overcharge', 'unused_subscription', 'failed_delivery')"
    )
    op.execute(
        "ALTER TABLE reconciliation_exceptions "
        "ALTER COLUMN exception_type TYPE recon_exception_type "
        "USING exception_type::text::recon_exception_type"
    )
    op.execute("DROP TYPE recon_exception_type_old")
