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
        # Money integrity (T0-7): a committed spend and its FX rate are strictly
        # positive. Refunds/reversals get first-class modelling in Tier 1 (T1-16),
        # not a negative row that would silently free budget.
        CheckConstraint("paid_amount > 0", name="ck_transactions_paid_amount_positive"),
        CheckConstraint(
            "local_amount > 0", name="ck_transactions_local_amount_positive"
        ),
        CheckConstraint(
            "exchange_rate > 0", name="ck_transactions_exchange_rate_positive"
        ),
        # The card/invoice rails' dedup key (T3-6): a redelivered webhook must
        # not double-book a spend. Partial, so source=sdk rows (always null,
        # deduped by uq_transactions_authorization_id) stay unconstrained.
        Index(
            "uq_transactions_company_transaction_hash",
            "company_id",
            "transaction_hash",
            unique=True,
            postgresql_where=text("transaction_hash IS NOT NULL"),
        ),
        # At most one reversal per original transaction (T1-16, idempotent
        # reverse). Declared here, not only in migration f3a4b5c6d7e8: without
        # it `alembic revision --autogenerate` emits a `drop_index` for this
        # money-integrity guard (caught by `alembic check`).
        Index(
            "uq_transactions_reverses_transaction_id",
            "reverses_transaction_id",
            unique=True,
            postgresql_where=text("reverses_transaction_id IS NOT NULL"),
        ),
        # Hot-path indexes (T3-1). `(agent_id, transaction_date)` is the most
        # important index in the schema: `services/spend_window.py` scans it
        # twice on every authorize() to compute the budget window and the
        # velocity count. The rest serve the list endpoint, the company-wide
        # ledger reads (recon scan / intelligence recompute / tax reclass), the
        # vendor filter, and the per-entity statement filter.
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
    # Null unless source=sdk (backend/PLAN.md §3).
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("authorizations.id"), default=None
    )
    # Refunds/reversals (T1-16): a reversal is a real transaction (positive
    # amount, contra journal) pointing at the original; the original is flagged
    # ``is_reversed``. Netted out of committed spend so a refund frees budget.
    reverses_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    # `server_default` mirrors migration `f3a4b5c6d7e8`, which needed one to add a
    # NOT NULL column to a populated table. Declaring it here too is not cosmetic:
    # with T4-7's `compare_server_default` on, an undeclared DB default is drift,
    # and it means a row inserted outside the ORM would hit a NOT NULL violation
    # in the model's view of the world but succeed in the real database.
    is_reversed: Mapped[bool] = mapped_column(default=False, server_default=false())
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
    # Caller-stated expense category (T1-19) driving journal classification.
    category: Mapped[str | None] = mapped_column(String(50), default=None)
    # Tax slab + inclusive/exclusive (T1-10) — the rate slab resolved at capture
    # and whether a domestic amount embedded GST or had it added on top.
    tax_category: Mapped[str | None] = mapped_column(String(20), default=None)
    tax_inclusive: Mapped[bool] = mapped_column(default=True)
    # Service vs goods (TE-4): a cross-border goods supply routes to customs, never
    # the services reverse-charge path.
    supply_type: Mapped[str] = mapped_column(String(10), default="service")
    model_used: Mapped[str | None] = mapped_column(String(100), default=None)
    tokens_consumed: Mapped[int | None] = mapped_column(default=None)
    # F-8: True when the booked amount is the authorize-time estimate, not a
    # settled actual — the SDK's T5-1 fallback books this way rather than lose
    # the spend. `server_default` mirrors migration `a3b4c5d6e7f8` (same T4-7
    # drift reasoning as `is_reversed` above).
    estimated: Mapped[bool] = mapped_column(default=False, server_default=false())
    # F-12: the evidence link the capture contract accepts; length mirrors the
    # schema's URL_MAX. The M3 invoice hook is its eventual structured consumer.
    invoice_url: Mapped[str | None] = mapped_column(String(2048), default=None)
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
