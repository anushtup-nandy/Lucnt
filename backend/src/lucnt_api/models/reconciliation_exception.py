"""Reconciliation-exception ORM model: a flagged anomaly on a committed txn (M7).

Recovery reads the ledger and writes here; it never mutates `transactions`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class ReconExceptionType(enum.StrEnum):
    """Anomaly kind.

    `FAILED_DELIVERY` is agent-reported (not inferable from spend);
    `TAX_UNCLASSIFIED` is emitted at capture for a vendor lacking place-of-supply
    data, so tax was booked conservatively. The rest are ledger-detected.
    """

    DUPLICATE = "duplicate"
    OVERCHARGE = "overcharge"
    UNUSED_SUBSCRIPTION = "unused_subscription"
    FAILED_DELIVERY = "failed_delivery"
    TAX_UNCLASSIFIED = "tax_unclassified"


class ReconExceptionStatus(enum.StrEnum):
    """Lifecycle: open → recovering → resolved | dismissed (both terminal)."""

    OPEN = "open"
    RECOVERING = "recovering"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReconciliationException(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One flagged anomaly against a committed transaction (the recovery inbox)."""

    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (
        # One per (txn, type), ever: a ledger fact is immutable, so a re-scan must
        # not re-flag it even after resolution.
        UniqueConstraint("transaction_id", "exception_type", name="uq_recon_txn_type"),
        # Recovery inbox list + open-count header.
        Index("ix_reconciliation_exceptions_company_id_status", "company_id", "status"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    exception_type: Mapped[ReconExceptionType] = mapped_column(
        Enum(
            ReconExceptionType,
            name="recon_exception_type",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    status: Mapped[ReconExceptionStatus] = mapped_column(
        Enum(
            ReconExceptionStatus,
            name="recon_exception_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=ReconExceptionStatus.OPEN,
    )
    # Detector facts + drafted recovery plan (heterogeneous, hence `Any`).
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Recoverable amount in the transaction's paid currency.
    amount_at_stake: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0")
    )
    # The inbox aggregates per currency, never summing across them.
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # Recurring identity (type:agent:vendor:amount:currency) so a dismissed
    # recurring exception isn't re-flagged next month. NULL for per-txn types.
    dedup_key: Mapped[str | None] = mapped_column(Text, default=None)
    fix_notes: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
