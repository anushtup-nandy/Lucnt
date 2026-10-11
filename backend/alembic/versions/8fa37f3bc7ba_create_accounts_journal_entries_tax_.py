"""create accounts journal_entries tax_records

Revision ID: 8fa37f3bc7ba
Revises: 9e80e05e2608
Create Date: 2026-07-04 19:23:03.649028

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8fa37f3bc7ba'
down_revision: str | None = '9e80e05e2608'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table('accounts',
    sa.Column('company_id', sa.Uuid(), nullable=False),
    sa.Column('account_name', sa.String(length=255), nullable=False),
    sa.Column('account_code', sa.String(length=20), nullable=False),
    sa.Column('category', sa.Enum('asset', 'liability', 'equity', 'income', 'expense', name='account_category'), nullable=False),
    sa.Column('normal_balance', sa.String(length=6), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('company_id', 'account_code', name='uq_accounts_company_code')
    )
    op.create_table('journal_entries',
    sa.Column('transaction_id', sa.Uuid(), nullable=False),
    sa.Column('account_name', sa.String(length=255), nullable=False),
    sa.Column('account_code', sa.String(length=20), nullable=False),
    sa.Column('debit', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('credit', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('ledger', sa.String(length=50), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tax_records',
    sa.Column('transaction_id', sa.Uuid(), nullable=False),
    sa.Column('tax_type', sa.String(length=50), nullable=False),
    sa.Column('jurisdiction', sa.String(length=50), nullable=False),
    sa.Column('rate', sa.Numeric(precision=9, scale=6), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('is_reclaimable', sa.Boolean(), nullable=False),
    sa.Column('reclaim_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('legal_reference', sa.Text(), nullable=False),
    sa.Column('self_invoice_generated', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Revert this migration."""
    op.drop_table('tax_records')
    op.drop_table('journal_entries')
    op.drop_table('accounts')
    # Postgres keeps ENUM types after DROP TABLE; drop them so re-upgrade works.
    sa.Enum(name='account_category').drop(op.get_bind(), checkfirst=True)
