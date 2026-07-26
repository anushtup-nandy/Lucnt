"""unique entity code and transaction hash

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-25 11:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: str | None = 'd0e1f2a3b4c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make the two lookup keys the ledger dedupes on actually unique.

    **T3-5 — ``entities (company_id, code)``.** ``authorize()`` and every
    ``/books/*`` statement resolve an entity by ``(company_id, code)`` with
    ``scalar_one_or_none()`` (`routers/transactions.py:120`,
    `routers/books.py:142,152`). A second entity sharing a code therefore makes
    every authorize for it raise ``MultipleResultsFound`` — a 500 that takes the
    whole spend path down for that entity, with no way to recover but a manual
    DELETE. Mirrors the existing ``uq_agents_company_slug`` invariant, and
    doubles as the index for that lookup.

    **T3-6 — ``transactions (company_id, transaction_hash)``.**
    ``transaction_hash`` is the card-rail dedup key (P6.5) but carries no
    uniqueness today, so a redelivered Stripe/Razorpay webhook would double-book
    the same spend: two transactions, two journals, two tax records, and budget
    consumed twice. Partial (``WHERE transaction_hash IS NOT NULL``) so the
    ``source=sdk`` rows — which always leave it null and dedupe via
    ``uq_transactions_authorization_id`` instead — stay unconstrained. Landing it
    before the card rail exists is the cheap moment to do it.

    **Why unique *indexes*, not ``UniqueConstraint``s** (a deviation from the
    older ``uq_agents_company_slug`` shape): a table constraint is added by
    ``ALTER TABLE``, which takes an ACCESS EXCLUSIVE lock and builds its index
    non-concurrently — on a populated ledger that stalls ``capture()`` for the
    duration. A unique index enforces uniqueness identically and can be built
    ``CONCURRENTLY`` (T4-10 house style). T3-6 has no choice regardless: a
    partial constraint cannot be expressed as a table constraint at all.
    """
    with op.get_context().autocommit_block():
        op.create_index(
            "uq_entities_company_code",
            "entities",
            ["company_id", "code"],
            unique=True,
            postgresql_concurrently=True,
        )
        op.create_index(
            "uq_transactions_company_transaction_hash",
            "transactions",
            ["company_id", "transaction_hash"],
            unique=True,
            postgresql_concurrently=True,
            postgresql_where=sa.text("transaction_hash IS NOT NULL"),
        )


def downgrade() -> None:
    """Drop both uniqueness guards (concurrently, to avoid a write lock)."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "uq_transactions_company_transaction_hash",
            table_name="transactions",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "uq_entities_company_code",
            table_name="entities",
            postgresql_concurrently=True,
        )
