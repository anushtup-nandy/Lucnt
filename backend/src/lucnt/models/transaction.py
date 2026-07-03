"""Transaction ORM model — committed spend, the ledger's source of truth.

Created by `POST /transactions/capture` (PLAN.md §3). Multi-surface: `source`
distinguishes the SDK path (`authorization_id` set) from the future card/invoice
rails (P6.5/M3, `authorization_id` null). Phase 1 only implements `source=sdk`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lucnt.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class TransactionSource(str, enum.Enum):
    """Which channel committed this spend to the ledger."""

    SDK = "sdk"
    CARD = "card"
    INVOICE = "invoice"


class TransactionStatus(str, enum.Enum):
    """Post-capture ledger status.

    Only `COMPLETED` is produced until Phase 4 (Recovery) adds reversal/
    dispute handling.
    """

    COMPLETED = "completed"


class Transaction(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A committed spend — the ledger's source of truth for one agent purchase."""

    __tablename__ = "transactions"
    __table_args__ = (
        # Postgres allows multiple NULLs under a unique constraint, so this only
        # blocks double-capture of one authorization on retry -- sdk/PLAN.md §7
        # failure mode #1 -- without constraining card/invoice rows (always null).
        UniqueConstraint(
            "authorization_id", name="uq_transactions_authorization_id"
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    # Null unless source=sdk (backend/PLAN.md §3).
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("authorizations.id"), default=None
    )
    source: Mapped[TransactionSource] = mapped_column(
        Enum(
            TransactionSource,
            name="transaction_source",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    # Dedup key for card/invoice ingestion (P6.5+); unused while source=sdk only.
    transaction_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    vendor: Mapped[str] = mapped_column(String(255))
    item_description: Mapped[str | None] = mapped_column(Text, default=None)
    # NUMERIC, never FLOAT (sdk/PLAN.md: Decimal not float).
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    paid_currency: Mapped[str] = mapped_column(String(3))
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("1.0")
    )
    local_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    local_currency: Mapped[str] = mapped_column(String(3))
    project: Mapped[str | None] = mapped_column(String(255), default=None)
    task_type: Mapped[str | None] = mapped_column(String(100), default=None)
    intent: Mapped[str | None] = mapped_column(String(255), default=None)
    model_used: Mapped[str | None] = mapped_column(String(100), default=None)
    tokens_consumed: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(
            TransactionStatus,
            name="transaction_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TransactionStatus.COMPLETED,
    )
    # Plain UUID, no FK yet -- mirrors Authorization.policy_id (Phase 3 adds the table).
    policy_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    approval_method: Mapped[str | None] = mapped_column(String(50), default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    # Phase 5 (Intelligence) insight text; always null until then.
    learned_text: Mapped[str | None] = mapped_column(Text, default=None)
    transaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
