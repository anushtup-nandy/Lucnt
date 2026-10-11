"""recon_exception_type += tax_unclassified (conservative-default flag)

Revision ID: b2c3d4e5f6a7
Revises: a1f2b3c4d5e6
Create Date: 2026-07-22 10:15:00.000000

Capture-emitted when a vendor lacks place-of-supply data, so finance can enrich it.
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
    """Add tax_unclassified; ADD VALUE is safe in a txn since it's unused here."""
    op.execute(
        "ALTER TYPE recon_exception_type ADD VALUE IF NOT EXISTS 'tax_unclassified'"
    )


def downgrade() -> None:
    """Recreate the enum without tax_unclassified (Postgres can't drop one value).

    Rows holding the value are deleted first so the re-cast succeeds.
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
