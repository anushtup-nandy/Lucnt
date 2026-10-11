"""JournalEntry ORM model — one leg of a double-entry journal (Phase 2).

Produced by `services/journal_generator.py` after a transaction is captured, and
by the manual-journal / funding endpoints which have **no** transaction.
Each row is a single debit *or* credit against one account; the set of rows for a
journal always balances (Σ debit == Σ credit).

**Self-sufficient.** A line carries its own ``company_id``,
``entity_id`` and ``posting_date`` so the books read path scopes and periodises
off the line itself, not off a join to `transactions`. ``transaction_id`` is
nullable: manual journals, funding/opening-balance entries, and tax-settlement
journals post without one.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class JournalEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A single debit-or-credit line posted against one account for a transaction."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        # A line is one-sided: exactly one of debit/credit is non-zero, and
        # neither is negative. Guards any writer (card rail, manual
        # journals, backfills) from silently corrupting a balanced ledger.
        CheckConstraint(
            "debit >= 0 AND credit >= 0 AND (debit = 0) <> (credit = 0)",
            name="ck_journal_entries_one_sided",
        ),
        # Hot-path indexes: the transaction FK was unindexed (the reverse
        # path and `/books/journals` both look lines up by it), and every
        # statement filters company_id + optional entity_id + optional
        # posting_date range (`routers/books.py::_aggregates`).
        Index("ix_journal_entries_transaction_id", "transaction_id"),
        Index(
            "ix_journal_entries_company_id_entity_id_posting_date",
            "company_id",
            "entity_id",
            "posting_date",
        ),
    )

    # Which company/entity's book this line belongs to — the books read
    # path scopes off these, not a transaction join, so manual/funding/settlement
    # journals (no transaction) are first-class.
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    # Nullable: capture-booked lines reference their transaction; manual
    # journals, funding/opening-balance entries and tax settlements have none.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    # The accounting date of this posting — the books periodise off
    # this, so a statement is a true period statement independent of the txn join.
    posting_date: Mapped[date] = mapped_column(Date)
    # Denormalized name/code snapshot so a journal reads correctly even if the
    # Chart of Accounts is later renamed (standard ledger practice).
    account_name: Mapped[str] = mapped_column(String(255))
    account_code: Mapped[str] = mapped_column(String(20))
    # NUMERIC, never FLOAT (Decimal, not float). Exactly one of
    # debit/credit is non-zero on any given line.
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    # Which entity's book this line belongs to (entity code, e.g. "sg"/"in"/"us").
    ledger: Mapped[str] = mapped_column(String(50))
    # The functional (entity) currency this line is booked in. A single entity's
    # journal is single-currency; statements never sum across currencies.
    currency: Mapped[str] = mapped_column(String(3))
