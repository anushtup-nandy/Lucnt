"""unique constraint on transactions authorization_id

Revision ID: 9e80e05e2608
Revises: cafbda804fb1
Create Date: 2026-07-03 09:50:49.363768

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9e80e05e2608'
down_revision: str | None = 'cafbda804fb1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_unique_constraint('uq_transactions_authorization_id', 'transactions', ['authorization_id'])


def downgrade() -> None:
    """Revert this migration."""
    op.drop_constraint('uq_transactions_authorization_id', 'transactions', type_='unique')
