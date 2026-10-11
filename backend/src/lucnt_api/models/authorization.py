"""Authorization ORM model: a pending hold, created before money moves."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class VerdictStatus(enum.StrEnum):
    """Policy verdict on a spend, before money moves. Mirrors `sdk.Verdict.status`."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    REVIEW = "review"
    HUMAN_HOLD = "human_hold"


class Authorization(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A pending spend hold awaiting (or having received) a policy verdict."""

    __tablename__ = "authorizations"
    __table_args__ = (
        # DB is the last line of defence for writers that bypass the schema.
        CheckConstraint("amount > 0", name="ck_authorizations_amount_positive"),
        # NULL allowed: a controls-blocked hold never reserves budget.
        CheckConstraint(
            "local_amount IS NULL OR local_amount > 0",
            name="ck_authorizations_local_amount_positive",
        ),
        CheckConstraint(
            "exchange_rate IS NULL OR exchange_rate > 0",
            name="ck_authorizations_exchange_rate_positive",
        ),
        # One hold per (company, idempotency key); partial so NULLs are free.
        Index(
            "uq_authorizations_company_idempotency_key",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Hot path: authorize()'s reservation + velocity scans; then the approval
        # queue.
        Index(
            "ix_authorizations_agent_id_status_created_at",
            "agent_id",
            "status",
            "created_at",
        ),
        Index("ix_authorizations_company_id_status", "company_id", "status"),
        # Keeps the reserved_spend scan index-only over released_at/expires_at.
        Index(
            "ix_authorizations_agent_id_status_released_at_expires_at",
            "agent_id",
            "status",
            "released_at",
            "expires_at",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"))
    vendor: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # FX snapshot in functional currency, so budget reservation is single-currency.
    # NULL for controls-blocked holds (they never reserve).
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    local_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    local_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    reason: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(255), default=None)
    project: Mapped[str | None] = mapped_column(String(255), default=None)
    # Caller-stated expense category; drives journal classification + blocked-ITC.
    category: Mapped[str | None] = mapped_column(String(50), default=None)
    tax_category: Mapped[str | None] = mapped_column(String(20), default=None)
    tax_inclusive: Mapped[bool] = mapped_column(default=True)
    # A cross-border *goods* supply routes to customs, not services RCM.
    supply_type: Mapped[str] = mapped_column(String(10), default="service")
    model_used: Mapped[str | None] = mapped_column(String(100), default=None)
    tokens_consumed: Mapped[int | None] = mapped_column(default=None)
    status: Mapped[VerdictStatus] = mapped_column(
        Enum(
            VerdictStatus,
            name="verdict_status",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # Engine's explanation for `status`; `reason` is the caller's justification.
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    # Persisted so an idempotent replay returns the original code (not derivable).
    verdict_code: Mapped[str | None] = mapped_column(String(50), default=None)
    # A retried authorize with the same key returns this hold.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)

    # Orphaned-hold release (else an uncaptured hold reserves budget forever).
    # `released_at`: explicit void(); stops reserving and makes capture a 409.
    # `expires_at`: TTL; stops reserving but still allows capture, since dropping
    # a real late spend from the ledger is worse than a stale reservation.
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    release_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
