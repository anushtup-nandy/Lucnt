"""JournalEntry ORM model — one leg of a double-entry journal (Phase 2).

Produced by `services/journal_generator.py` after a transaction is captured.
Each row is a single debit *or* credit against one account; the set of rows
for a transaction always balances (Σ debit == Σ credit).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class JournalEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A single debit-or-credit line posted against one account for a transaction."""

    __tablename__ = "journal_entries"

    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    # Denormalized name/code snapshot so a journal reads correctly even if the
    # Chart of Accounts is later renamed (standard ledger practice).
    account_name: Mapped[str] = mapped_column(String(255))
    account_code: Mapped[str] = mapped_column(String(20))
    # NUMERIC, never FLOAT (sdk/PLAN.md: Decimal not float). Exactly one of
    # debit/credit is non-zero on any given line.
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    # Which entity's book this line belongs to (entity code, e.g. "sg"/"in"/"us").
    ledger: Mapped[str] = mapped_column(String(50))
