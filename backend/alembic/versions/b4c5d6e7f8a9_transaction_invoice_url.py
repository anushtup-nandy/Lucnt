"""persist the capture contract's invoice_url

`CaptureRequest.invoice_url` has been accepted and length-validated since P1
and then silently dropped — `transactions` had no column for it, so a caller
who attached evidence could never read it back. The wire contract implied
persistence; now the ledger honours it.

Nullable with no backfill: historical rows genuinely carried no evidence link
(the value was discarded at capture time and is unrecoverable), so NULL is the
truthful representation, not a placeholder.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the evidence-link column; length mirrors the schema's URL_MAX bound."""
    op.add_column(
        "transactions", sa.Column("invoice_url", sa.String(2048), nullable=True)
    )


def downgrade() -> None:
    """Drop the evidence-link column."""
    op.drop_column("transactions", "invoice_url")
