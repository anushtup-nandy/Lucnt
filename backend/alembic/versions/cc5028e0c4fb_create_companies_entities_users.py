"""create companies entities users

Revision ID: cc5028e0c4fb
Revises: 
Create Date: 2026-07-03 00:32:58.565490

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cc5028e0c4fb'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table('companies',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=255), nullable=False),
    sa.Column('plan', sa.String(length=50), nullable=False),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('entities',
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('code', sa.String(length=50), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('accounting_std', sa.String(length=50), nullable=False),
    sa.Column('erp', sa.String(length=50), nullable=True),
    sa.Column('tax_regime', sa.String(length=50), nullable=False),
    sa.Column('to_usd_rate', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.Column('timezone', sa.String(length=50), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('engineer', 'cfo', 'agent_owner', 'approver', 'auditor', name='user_role'), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('last_login', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )


def downgrade() -> None:
    """Revert this migration."""
    op.drop_table('users')
    op.drop_table('entities')
    op.drop_table('companies')
    # Postgres keeps ENUM types after DROP TABLE; drop them so re-upgrade works.
    sa.Enum(name='user_role').drop(op.get_bind(), checkfirst=True)
