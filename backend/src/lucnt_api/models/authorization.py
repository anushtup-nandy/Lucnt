"""Authorization ORM model — a pending hold from `POST /transactions/authorize`.

Money has not moved yet when this row is created (PLAN.md §3). `status` mirrors
the SDK's `Verdict.status` (`sdk/src/lucnt/models.py`) exactly.
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
        # A hold amount is never zero/negative (T0-4) — enforced at the schema
        # too, but the DB is the last line of defence for any future writer.
        CheckConstraint("amount > 0", name="ck_authorizations_amount_positive"),
        # The functional-currency snapshot (T1-2) is positive when present;
        # NULL is allowed (a controls-blocked hold never reserves budget).
        CheckConstraint(
            "local_amount IS NULL OR local_amount > 0",
            name="ck_authorizations_local_amount_positive",
        ),
        CheckConstraint(
            "exchange_rate IS NULL OR exchange_rate > 0",
            name="ck_authorizations_exchange_rate_positive",
        ),
        # Idempotency (T0-6): at most one hold per (company, client key). Partial,
        # so the common no-key path is unconstrained (many NULLs allowed).
        Index(
            "uq_authorizations_company_idempotency_key",
            "company_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Hot-path indexes (T3-1). `reserved_spend` filters (agent_id, status);
        # `recent_activity_count` adds created_at — one composite serves both, so
        # authorize() stops sequentially scanning holds twice per call
        # (`services/spend_window.py:104,154`). The second index serves the
        # review/human_hold approval queue.
        Index(
            "ix_authorizations_agent_id_status_created_at",
            "agent_id",
            "status",
            "created_at",
        ),
        Index("ix_authorizations_company_id_status", "company_id", "status"),
        # T5-4: `reserved_spend` now also filters released_at/expires_at, so the
        # reservation scan stays index-only instead of falling back to a seq scan
        # on the two new predicates.
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
    # NUMERIC, never FLOAT (sdk/PLAN.md: Decimal not float).
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # FX snapshot at authorize (T1-2): the hold's value in the entity's functional
    # currency, so budget *reservation* is measured in one currency (never a raw
    # sum of mixed paid currencies). Nullable — a controls-blocked hold is never
    # approved and never reserves, so it carries no snapshot.
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    local_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    local_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    reason: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(255), default=None)
    project: Mapped[str | None] = mapped_column(String(255), default=None)
    # Caller-stated expense category (T1-19), carried to the transaction at
    # capture; drives journal classification + blocked-ITC (T1-14).
    category: Mapped[str | None] = mapped_column(String(50), default=None)
    # Tax slab + inclusive/exclusive (T1-10), carried to the transaction at capture.
    tax_category: Mapped[str | None] = mapped_column(String(20), default=None)
    tax_inclusive: Mapped[bool] = mapped_column(default=True)
    # Whether the supply is a service or goods (TE-4), carried to the transaction at
    # capture. A cross-border *goods* supply routes to customs, not services RCM.
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
    # Plain UUID, no FK yet -- the `policies` table doesn't exist until Phase 3
    # (root PLAN.md §4). The FK constraint lands with that migration.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    # The policy engine's own explanation for `status` (e.g. "exceeds monthly
    # budget"), distinct from `reason` (the caller-supplied justification).
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    # Machine-readable verdict code (VerdictCode), persisted so an idempotent
    # replay returns the exact original code (T0-6) — it cannot be re-derived.
    verdict_code: Mapped[str | None] = mapped_column(String(50), default=None)
    # Client-supplied idempotency key: a retried authorize with the same key
    # returns this hold instead of minting a new one (T0-6).
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)

    # --- T5-4: orphaned-hold release ----------------------------------------
    # An approved-but-uncaptured hold reserves budget forever (see
    # `services/spend_window.py::reserved_spend`), so a vendor error after
    # authorize — or a crash between authorize and capture — silently and
    # permanently shrinks the agent's budget. Two independent releases:
    #
    # `released_at` — an explicit `void()` from the caller: "this spend did not
    # happen." Stops reserving *and* makes a later capture a 409, because the
    # caller has stated the money never moved.
    #
    # `expires_at` — a TTL stamped at authorize. Stops reserving once passed, but
    # deliberately does **not** block a later capture: a slow-but-real spend
    # arriving after the TTL is a real spend, and refusing to book it would lose
    # money from the ledger, which is strictly worse than a stale reservation.
    # Reservation is a forecast; the ledger is the truth.
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    release_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
