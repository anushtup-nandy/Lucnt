"""add api_key scope

Revision ID: 7b9700cb50ed
Revises: 3987c46ddfb0
Create Date: 2026-07-18 15:41:01.159800

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7b9700cb50ed'
down_revision: str | None = '3987c46ddfb0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add api_keys.scope (runtime|admin) for separation of duties (T0-12).

    Existing keys default to `admin` (server_default) so they keep full power;
    agent runtime keys are provisioned `runtime` explicitly.
    """
    api_key_scope = sa.Enum("runtime", "admin", name="api_key_scope")
    api_key_scope.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "api_keys",
        sa.Column(
            "scope",
            api_key_scope,
            nullable=False,
            server_default="admin",
        ),
    )


def downgrade() -> None:
    """Drop api_keys.scope."""
    op.drop_column("api_keys", "scope")
    sa.Enum(name="api_key_scope").drop(op.get_bind(), checkfirst=True)
