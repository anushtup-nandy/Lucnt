"""JournalEntry ORM model: one leg of a balanced double-entry journal.

Carries its own company/entity/posting_date so the books never need a txn join.
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
        # One-sided, non-negative: guards any writer from corrupting the ledger.
        CheckConstraint(
            "debit >= 0 AND credit >= 0 AND (debit = 0) <> (credit = 0)",
            name="ck_journal_entries_one_sided",
        ),
        # Reverse path + /books/journals look up by txn; every statement filters
        # company + entity + posting_date.
        Index("ix_journal_entries_transaction_id", "transaction_id"),
        Index(
            "ix_journal_entries_company_id_entity_id_posting_date",
            "company_id",
            "entity_id",
            "posting_date",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    # Null for manual, funding/opening-balance and tax-settlement journals.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    posting_date: Mapped[date] = mapped_column(Date)
    # Snapshot so the journal reads correctly after a CoA rename.
    account_name: Mapped[str] = mapped_column(String(255))
    account_code: Mapped[str] = mapped_column(String(20))
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    # Entity code, e.g. "sg"/"in"/"us".
    ledger: Mapped[str] = mapped_column(String(50))
    # Functional currency; statements never sum across currencies.
    currency: Mapped[str] = mapped_column(String(3))
