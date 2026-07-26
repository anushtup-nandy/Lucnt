"""Reconciliation-exception ORM model — the recovery layer (Phase 4, M7).

A `reconciliation_exceptions` row is a flagged anomaly against a committed
`transactions` row: a duplicate charge, a statistical overcharge, a recurring
(possibly unused) subscription, or an agent-reported failed delivery. Recovery
**reads** the ledger and writes here; it never mutates `transactions`, keeping
the frozen P1 `authorize→capture` contract lane-agnostic (`CLAUDE.local.md`).

Detectors (`services/detectors.py`) create these via `POST /recon/scan`; the
`failed_delivery` type is agent-reported via the SDK `mark_failed_delivery()`
hook. A recovery plan drafted by `services/recovery_agent.py` is stored in
`evidence_json`; humans move an exception to a terminal state via
`POST /recon/{id}/resolve`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
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


class ReconExceptionType(str, enum.Enum):
    """The kind of anomaly a reconciliation exception records.

    `DUPLICATE`, `OVERCHARGE`, and `UNUSED_SUBSCRIPTION` are detected from the
    ledger by `services/detectors.py`; `FAILED_DELIVERY` is agent-reported (the
    SDK `mark_failed_delivery()` hook) because it cannot be inferred from spend
    data alone. `TAX_UNCLASSIFIED` is *capture-emitted* (TE-1b): a spend hit a
    vendor with no place-of-supply data, so tax was booked conservatively and the
    vendor needs enriching before the treatment can be finalised.
    """

    DUPLICATE = "duplicate"
    OVERCHARGE = "overcharge"
    UNUSED_SUBSCRIPTION = "unused_subscription"
    FAILED_DELIVERY = "failed_delivery"
    TAX_UNCLASSIFIED = "tax_unclassified"


class ReconExceptionStatus(str, enum.Enum):
    """Lifecycle of an exception: detected → in-recovery → terminal.

    `OPEN` is a freshly detected/reported exception; `RECOVERING` is one whose
    recovery action is in flight; `RESOLVED` (money recovered) and `DISMISSED`
    (a false positive or accepted loss) are the terminal states.
    """

    OPEN = "open"
    RECOVERING = "recovering"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReconciliationException(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One flagged anomaly against a committed transaction (the recovery inbox)."""

    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (
        # DB-level idempotency (mirrors uq_transactions_authorization_id): at most
        # one exception per (transaction, type), ever. A detected ledger fact (a
        # duplicate/overcharge row) is immutable, so a re-scan must not re-flag it
        # even once resolved — and a genuinely new occurrence is a new
        # transaction row, which gets its own exception.
        UniqueConstraint(
            "transaction_id", "exception_type", name="uq_recon_txn_type"
        ),
        # The recovery inbox list + the open-count header (T3-1). transaction_id
        # lookups already ride uq_recon_txn_type's leading column.
        Index(
            "ix_reconciliation_exceptions_company_id_status", "company_id", "status"
        ),
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
    # Detector facts + the drafted recovery plan. `Any` is justified (as in
    # Policy.config_json): genuinely heterogeneous JSON per exception type,
    # produced/consumed by the pure detector + recovery-agent services.
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Recoverable amount, in the transaction's paid currency. NUMERIC, never
    # FLOAT (sdk/PLAN.md: Decimal not float). Drives the inbox's "$ at stake".
    amount_at_stake: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), default=Decimal("0")
    )
    # The currency of `amount_at_stake` (the transaction's paid currency). The
    # inbox header aggregates "$ at stake" per currency, never summing across them
    # (PRODUCTION_PLAN T2-2). Nullable only for rows predating the column.
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # Stable recurring-identity key (type:agent:vendor:amount:currency) for the
    # ledger-detector types, so a dismissed recurring exception is not re-flagged on
    # next month's transaction row (T2-7). NULL for per-transaction types
    # (failed_delivery, tax_unclassified).
    dedup_key: Mapped[str | None] = mapped_column(Text, default=None)
    fix_notes: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
