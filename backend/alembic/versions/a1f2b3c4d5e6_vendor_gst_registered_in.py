"""vendors.gst_registered_in — registration-in-buyer-country as the RCM key

Revision ID: a1f2b3c4d5e6
Revises: f5a6b7c8d9e0
Create Date: 2026-07-22 09:30:00.000000

The reverse-charge decision must key off *where the vendor is GST-registered*, not
its home country and never its invoice currency. The legal
test for India RCM is "is the vendor GST-registered in the buyer's country": a
foreign vendor OIDAR-registered in India charges forward IGST (domestic
treatment), while a domestic vendor not registered in India is reverse charge
under CGST s.9(4). A single ``is_registered`` boolean cannot express "registered
*where*", so it is replaced by ``gst_registered_in`` (a list of ISO-3166 alpha-2
country codes). The old boolean meant "charges forward GST in its own country", so
it back-fills to ``[country]``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1f2b3c4d5e6'
down_revision: str | None = 'f5a6b7c8d9e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add gst_registered_in, back-fill from is_registered+country, drop the bool."""
    op.add_column(
        "vendors",
        sa.Column(
            "gst_registered_in",
            postgresql.ARRAY(sa.String(length=2)),
            nullable=False,
            server_default="{}",
        ),
    )
    # A vendor previously flagged registered (charging forward GST) in its own
    # country becomes registered-in-[country]. Rows with no country cannot be
    # placed and stay empty (they will route to the recon queue).
    op.execute(
        sa.text(
            "UPDATE vendors SET gst_registered_in = ARRAY[country] "
            "WHERE is_registered = true AND country IS NOT NULL"
        )
    )
    op.drop_column("vendors", "is_registered")


def downgrade() -> None:
    """Reverse: re-create is_registered (true iff registered anywhere), drop the list."""
    op.add_column(
        "vendors",
        sa.Column(
            "is_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE vendors SET is_registered = true "
            "WHERE array_length(gst_registered_in, 1) IS NOT NULL"
        )
    )
    op.alter_column("vendors", "is_registered", server_default=None)
    op.drop_column("vendors", "gst_registered_in")
