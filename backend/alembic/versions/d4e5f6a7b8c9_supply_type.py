"""supply_type (service|goods) on authorizations + transactions (TE-4)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-22 12:30:00.000000

Import of goods (customs duty + IGST at the border) is a different mechanism from
import of services (reverse charge). Nothing guarded a goods transaction from
silently routing through the services RCM path (PRODUCTION_PLAN TE-4). A
``supply_type`` on the spine (default ``"service"`` — agent spend is ~all
services) lets the engine send a cross-border *goods* spend to a distinct customs
path instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add supply_type to authorizations + transactions (default 'service')."""
    for table in ("authorizations", "transactions"):
        op.add_column(
            table,
            sa.Column(
                "supply_type",
                sa.String(length=10),
                nullable=False,
                server_default="service",
            ),
        )
        # The default only back-filled existing rows; new rows carry an explicit value.
        op.alter_column(table, "supply_type", server_default=None)


def downgrade() -> None:
    """Reverse: drop supply_type from both tables."""
    for table in ("transactions", "authorizations"):
        op.drop_column(table, "supply_type")
