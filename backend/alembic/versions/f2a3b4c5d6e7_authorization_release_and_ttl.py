"""release orphaned authorization holds (void + TTL)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-28 12:00:00.000000

T5-4. An approved-but-uncaptured hold reserves an agent's budget indefinitely
(`services/spend_window.py::reserved_spend`), so a vendor error after
`authorize()` — or a crash between authorize and capture — silently and
permanently shrinks what that agent may spend. Two independent releases:

* `released_at` / `release_reason` — an explicit `POST /transactions/void`.
* `expires_at` — a TTL stamped at authorize, evaluated at read time so no sweep
  job is needed.

**Existing rows are left with NULL in all three columns, deliberately.**
Back-filling `expires_at` from `created_at` would release every historical hold
the instant this migration ran, which is a silent budget *increase* across every
agent in production — the opposite of the fail-closed behaviour those rows were
written under. NULL means "never expires", so history keeps its old semantics and
only holds created after the deploy carry a TTL.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_authorizations_agent_id_status_released_at_expires_at"


def upgrade() -> None:
    """Add the release/expiry columns and the index `reserved_spend` now needs."""
    op.add_column(
        "authorizations",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("release_reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "authorizations",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # `reserved_spend` filters (agent_id, status, released_at, expires_at) on the
    # blocking path of every authorize(); without this it degrades to a seq scan
    # on the two new predicates.
    op.create_index(
        _INDEX,
        "authorizations",
        ["agent_id", "status", "released_at", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the release/expiry columns and their index.

    Reverting loses which holds were voided, so every previously-released hold
    starts reserving budget again — tighter, not looser, which is the safe
    direction for a rollback.
    """
    op.drop_index(_INDEX, table_name="authorizations")
    op.drop_column("authorizations", "expires_at")
    op.drop_column("authorizations", "release_reason")
    op.drop_column("authorizations", "released_at")
