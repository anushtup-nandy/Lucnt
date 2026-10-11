"""Transaction ORM model: committed spend, the ledger's source of truth.

Multi-surface: `source=sdk` rows carry `authorization_id`; card/invoice rows don't.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class TransactionSource(enum.StrEnum):
    """Which channel committed this spend to the ledger."""

    SDK = "sdk"
    CARD = "card"
    INVOICE = "invoice"


class TransactionStatus(enum.StrEnum):
    """Post-capture ledger status."""

    COMPLETED = "completed"


class Transaction(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A committed spend — the ledger's source of truth for one agent purchase."""

    __tablename__ = "transactions"
    __table_args__ = (
        # NULLs are distinct: blocks double-capture of one authorization without
        # constraining card/invoice rows.
        UniqueConstraint("authorization_id", name="uq_transactions_authorization_id"),
        # Strictly positive: refunds are reversals, never negative rows that would
        # silently free budget.
        CheckConstraint("paid_amount > 0", name="ck_transactions_paid_amount_positive"),
        CheckConstraint(
            "local_amount > 0", name="ck_transactions_local_amount_positive"
        ),
        CheckConstraint(
            "exchange_rate > 0", name="ck_transactions_exchange_rate_positive"
        ),
        # Card/invoice webhook dedup; partial so sdk rows (always NULL) are free.
        Index(
            "uq_transactions_company_transaction_hash",
            "company_id",
            "transaction_hash",
            unique=True,
            postgresql_where=text("transaction_hash IS NOT NULL"),
        ),
        # One reversal per original. Must be declared here or autogenerate drops
        # it.
        Index(
            "uq_transactions_reverses_transaction_id",
            "reverses_transaction_id",
            unique=True,
            postgresql_where=text("reverses_transaction_id IS NOT NULL"),
        ),
        # (agent_id, transaction_date) is scanned twice per authorize() (budget
        # window + velocity); the rest serve list/ledger/vendor/entity reads.
        Index(
            "ix_transactions_agent_id_transaction_date",
            "agent_id",
            "transaction_date",
        ),
        Index("ix_transactions_company_id_created_at", "company_id", "created_at"),
        Index(
            "ix_transactions_company_id_transaction_date",
            "company_id",
            "transaction_date",
        ),
        Index("ix_transactions_company_id_vendor", "company_id", "vendor"),
        Index("ix_transactions_entity_id", "entity_id"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    # Null unless source=sdk.
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("authorizations.id"), default=None
    )
    # A reversal is a positive txn pointing at the original, which is flagged
    # `is_reversed` and netted out of committed spend.
    reverses_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    # server_default mirrors the migration; omitting it is drift under
    # compare_server_default.
    is_reversed: Mapped[bool] = mapped_column(default=False, server_default=false())
    source: Mapped[TransactionSource] = mapped_column(
        Enum(
            TransactionSource,
            name="transaction_source",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    # Dedup key for card/invoice ingestion.
    transaction_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    vendor: Mapped[str] = mapped_column(String(255))
    item_description: Mapped[str | None] = mapped_column(Text, default=None)
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
    # Caller-stated expense category driving journal classification.
    category: Mapped[str | None] = mapped_column(String(50), default=None)
    # `tax_inclusive`: whether a domestic amount embeds GST or adds it on top.
    tax_category: Mapped[str | None] = mapped_column(String(20), default=None)
    tax_inclusive: Mapped[bool] = mapped_column(default=True)
    # Cross-border goods route to customs, never services reverse charge.
    supply_type: Mapped[str] = mapped_column(String(10), default="service")
    model_used: Mapped[str | None] = mapped_column(String(100), default=None)
    tokens_consumed: Mapped[int | None] = mapped_column(default=None)
    # Booked at the authorize-time estimate (SDK fallback) rather than lose the
    # spend. server_default: see `is_reversed`.
    estimated: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Length mirrors the schema's URL_MAX.
    invoice_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(
            TransactionStatus,
            name="transaction_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TransactionStatus.COMPLETED,
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    approval_method: Mapped[str | None] = mapped_column(String(50), default=None)
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    learned_text: Mapped[str | None] = mapped_column(Text, default=None)
    transaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
